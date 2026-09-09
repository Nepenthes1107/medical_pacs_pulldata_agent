"""补拉/首拉建任务服务（plan-autonomous-v2 §1.2）。

把「建 DownloadTask/Study/Series/Archive + 投 download_queue」的核心逻辑抽为单一函数，
`/tasks/pull` 端点与 Agent 的 first_pull 直达节点共用一份，不复制业务、保持单一真相。

错误约定：以领域异常 PullError 表达业务失败（source 不存在 / Study 未找到 / PACS 失败等），
调用方（API / 节点）各自翻译为 HTTP 状态或结构化 state，服务层不耦合 Web 语义。
"""
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import ArchiveStatus, DataLevel, DownloadStatus
from app.core.messaging import publish_download_task
from app.core.models import ArchiveModel, DownloadTask, SeriesModel, StudyModel
from app.core.schemas import PullTaskRequest, SeriesResult, StudyResult
from app.dicom.pacs_client import PacsClient


class PullError(Exception):
    """建任务领域异常。status_code 供 API 层翻译为 HTTP 状态。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def create_pull_task(request: PullTaskRequest, db: Session) -> DownloadTask:
    """建任务 + 投队列的单一真相函数。成功返回已提交的 DownloadTask；失败抛 PullError。"""
    _ensure_source(request.source_id)

    study_result = None
    pacs_series: List[SeriesResult] = []
    series_list = _normalize_series_list(request)
    series_instance_uid = request.series_instance_uid or _first_series_uid(series_list)

    if request.level == DataLevel.SERIES and not series_instance_uid:
        raise PullError("series_instance_uid is required for series level task", 400)
    # sop 级：必须至少有一条目标带非空 SOP 列表，否则无从定向（不许伪装 sop 粒度）。
    if request.level == DataLevel.SOP and not any(
        item.get("sop_instance_uid_list") for item in series_list
    ):
        raise PullError("sop level task requires series_list with sop_instance_uid_list", 400)

    if not request.skip_pacs_find:
        study_result, pacs_series = _load_pacs_metadata(request.source_id, request.study_instance_uid)
        if request.level == DataLevel.SERIES and not _series_exists(pacs_series, series_instance_uid):
            raise PullError("Series not found in PACS", 404)
        if request.level == DataLevel.SOP:
            # sop 级：逐个目标 Series 必须在 PACS 侧真实存在，否则定向补拉必然落空。
            for item in series_list:
                if not _series_exists(pacs_series, item.get("series_instance_uid")):
                    raise PullError("Series not found in PACS: %s" % item.get("series_instance_uid"), 404)
        series_list = _series_task_items(series_list, pacs_series, request.level.value, series_instance_uid)
        series_instance_uid = request.series_instance_uid or _first_series_uid(series_list)

    expected_count = _expected_count(request, series_list, study_result, pacs_series, series_instance_uid)
    task_id = str(uuid4())
    task_body = {
        "series_list": series_list,
    }

    if study_result:
        _upsert_study_from_pacs(db, request.source_id, study_result)
    else:
        _upsert_study(db, request, expected_count)

    if pacs_series:
        _upsert_series_from_pacs(db, request.source_id, pacs_series)
    else:
        _upsert_series(db, request, series_list)

    task = DownloadTask(
        task_id=task_id,
        study_instance_uid=request.study_instance_uid,
        series_instance_uid=series_instance_uid,
        modality=request.modality or _task_modality(pacs_series, series_instance_uid),
        expected_image_number=expected_count,
        level=request.level.value,
        status=DownloadStatus.IN_QUEUE.value,
        queued_at=datetime.utcnow(),
        priority=request.priority,
        source_id=request.source_id,
        task_body=task_body,
    )
    db.add(task)
    db.flush()
    _upsert_archive(db, request, task_id, expected_count, series_instance_uid, series_list)
    db.commit()

    # 清除该范围的止损标记：止损是一次性的临时状态，新建任务即表示人工已决定重新拉取。
    # 不清的话 storescp 会静默拒收新任务的影像（L4 按 study/series UID 判断，不认 task_id）。
    _clear_abort_flags(request, series_list, series_instance_uid)

    try:
        publish_download_task(task_message(task))
    except Exception as exc:
        task.status = DownloadStatus.FAIL.value
        task.last_error = "RabbitMQ publish failed: %s" % exc
        db.commit()
        raise PullError(task.last_error, 503)

    return task


def _clear_abort_flags(request: PullTaskRequest, series_list: List[Dict],
                       series_instance_uid: Optional[str]) -> None:
    """清掉本次拉取范围上的止损标记（失败不阻断建任务，最坏情况是影像被拒收后人工排查）。"""
    from app.services import abort

    uids = [i.get("series_instance_uid") for i in series_list if i.get("series_instance_uid")]
    if not uids and series_instance_uid:
        uids = [series_instance_uid]
    # task_id 传空串：新任务的 task 级标记本就不存在，这里只需清 study/series 级。
    abort.clear_abort("", request.study_instance_uid, uids)


def task_message(task: DownloadTask) -> Dict:
    # 消息体只带 task_id：downloader 消费时按 task_id 从 DB 重查全部参数，
    # 避免 source_id/level/priority/task_body 在消息与 DB 间双写不一致。
    return {"task_id": task.task_id}


# ---------- 内部 helper（原 routes_task 建任务专用逻辑，随核心一并迁入）----------

def _ensure_source(source_id: str) -> None:
    try:
        settings.get_pacs_source(source_id)
    except KeyError as exc:
        raise PullError(str(exc), 404)


def _load_pacs_metadata(source_id: str, study_uid: str) -> Tuple[StudyResult, List[SeriesResult]]:
    try:
        client = PacsClient()
        studies = client.find_study(source_id, study_instance_uid=study_uid)
        if not studies:
            raise PullError("Study not found in PACS", 404)
        return studies[0], client.find_series(source_id, study_uid)
    except PullError:
        raise
    except Exception as exc:
        raise PullError("PACS C-FIND failed: %s" % exc, 503)


def _normalize_series_list(request: PullTaskRequest) -> List[Dict]:
    if request.series_list:
        return [item.model_dump() for item in request.series_list]
    if request.series_instance_uid:
        return [{"series_instance_uid": request.series_instance_uid, "sop_instance_uid_list": []}]
    return []


def _series_task_items(request_items, pacs_series, level, series_instance_uid):
    request_by_uid = {i.get("series_instance_uid"): i for i in request_items if i.get("series_instance_uid")}
    # series 级任务的拉取范围：以 request.series_list 显式点名集合为准，一次可定向多个 Series
    #（多 Series 定向补拉）；未点名时才退回单值 series_instance_uid（兼容单 series 定向语义）。
    wanted_series = set(request_by_uid) if request_by_uid else ({series_instance_uid} if series_instance_uid else set())
    items = []
    for series in pacs_series:
        if level == DataLevel.SERIES.value and series.series_instance_uid not in wanted_series:
            continue
        # sop 级只保留请求里点名的 Series——不能把整个 Study 的 Series 都拉进定向任务。
        if level == DataLevel.SOP.value and series.series_instance_uid not in request_by_uid:
            continue
        item = dict(request_by_uid.get(series.series_instance_uid, {}))
        item["series_instance_uid"] = series.series_instance_uid
        item.setdefault("sop_instance_uid_list", [])
        # 期望数：定向到具体 SOP 时以 SOP 列表长度为准，否则用 PACS 的整 Series 数。
        if item["sop_instance_uid_list"]:
            item["expected_image_number"] = len(item["sop_instance_uid_list"])
        elif series.number_of_series_related_instances is not None:
            item["expected_image_number"] = series.number_of_series_related_instances
        items.append(item)
    return items or request_items


def _expected_count(request, series_list, study_result, pacs_series, series_instance_uid):
    if request.expected_image_number is not None:
        return request.expected_image_number
    # sop 级：期望数就是被点名的 SOP 总数（定向补拉只对这些张负责）。
    if request.level == DataLevel.SOP:
        counts = [len(item.get("sop_instance_uid_list") or []) for item in series_list]
        return sum(counts) if any(counts) else None
    if request.level == DataLevel.SERIES:
        # series 级任务的期望数 = 各被点名 Series 期望数之和：一次定向多个 Series 时
        # expected 代表任务覆盖的总量，checker 按此判定完整（与 _recompute_expected 一致）。
        counts = [item.get("expected_image_number") for item in series_list
                  if item.get("expected_image_number") is not None]
        if counts:
            return sum(counts)
        if series_instance_uid:
            for series in pacs_series:
                if series.series_instance_uid == series_instance_uid:
                    return series.number_of_series_related_instances
        return None
    if study_result and study_result.number_of_study_related_instances is not None:
        return study_result.number_of_study_related_instances
    pacs_counts = [i.number_of_series_related_instances for i in pacs_series]
    if pacs_counts and all(c is not None for c in pacs_counts):
        return sum(pacs_counts)
    request_counts = [i.get("expected_image_number") for i in series_list]
    if request_counts and all(c is not None for c in request_counts):
        return sum(request_counts)
    return None


def _first_series_uid(series_list: List[Dict]) -> Optional[str]:
    return series_list[0].get("series_instance_uid") if series_list else None


def _series_exists(series_list: List[SeriesResult], series_uid: Optional[str]) -> bool:
    return bool(series_uid) and any(i.series_instance_uid == series_uid for i in series_list)


def _task_modality(series_list: List[SeriesResult], series_uid: Optional[str]) -> Optional[str]:
    if series_uid:
        for item in series_list:
            if item.series_instance_uid == series_uid:
                return item.modality
    for item in series_list:
        if item.modality:
            return item.modality
    return None


def _parse_dicom_date(value: Optional[str]) -> Optional[date]:
    if not value or len(value) != 8:
        return None
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def _upsert_study_from_pacs(db: Session, source_id: str, result: StudyResult) -> None:
    study = db.query(StudyModel).filter(StudyModel.study_instance_uid == result.study_instance_uid).first()
    if not study:
        study = StudyModel(study_instance_uid=result.study_instance_uid)
        db.add(study)
    study.source_id = source_id
    study.study_date = _parse_dicom_date(result.study_date)
    study.modalities_in_study = result.modality
    study.number_of_study_related_instances = result.number_of_study_related_instances
    study.find_source = {"source_id": source_id, "raw": result.raw}


def _upsert_series_from_pacs(db: Session, source_id: str, series_results: List[SeriesResult]) -> None:
    for result in series_results:
        series = db.query(SeriesModel).filter(
            SeriesModel.study_instance_uid == result.study_instance_uid,
            SeriesModel.series_instance_uid == result.series_instance_uid,
        ).first()
        if not series:
            series = SeriesModel(study_instance_uid=result.study_instance_uid,
                                 series_instance_uid=result.series_instance_uid)
            db.add(series)
        series.source_id = source_id
        series.modality = result.modality
        series.number_of_series_related_instances = result.number_of_series_related_instances
        series.find_source = {"source_id": source_id, "raw": result.raw}


def _upsert_study(db: Session, request: PullTaskRequest, expected_count: Optional[int]) -> None:
    study = db.query(StudyModel).filter(StudyModel.study_instance_uid == request.study_instance_uid).first()
    if not study:
        study = StudyModel(study_instance_uid=request.study_instance_uid)
        db.add(study)
    study.source_id = request.source_id
    # 只有整 Study 级任务的 expected 才代表 Study 总数；series/sop 级 expected 是局部数量，
    # 写进来会污染 Study 权威期望数（Checker 拿它做完整性比对）。
    if expected_count is not None and request.level == DataLevel.STUDY:
        study.number_of_study_related_instances = expected_count
    study.find_source = study.find_source or {}
    study.move_source = {"source_id": request.source_id, "level": request.level.value}


def _upsert_series(db: Session, request: PullTaskRequest, series_list: List[Dict]) -> None:
    for item in series_list:
        series_uid = item.get("series_instance_uid")
        if not series_uid:
            continue
        series = db.query(SeriesModel).filter(
            SeriesModel.study_instance_uid == request.study_instance_uid,
            SeriesModel.series_instance_uid == series_uid,
        ).first()
        if not series:
            series = SeriesModel(study_instance_uid=request.study_instance_uid, series_instance_uid=series_uid)
            db.add(series)
        series.source_id = request.source_id
        series.modality = request.modality
        # sop 级的 expected 只是被点名的若干张，不是该 Series 的总数，不能覆盖权威期望数。
        if item.get("expected_image_number") is not None and request.level != DataLevel.SOP:
            series.number_of_series_related_instances = item.get("expected_image_number")


def _upsert_archive(db, request, task_id, expected_count, series_instance_uid, series_list=None) -> None:
    data_id = _build_data_id(request.level.value, request.study_instance_uid, series_instance_uid,
                             task_id, series_list)
    archive = db.query(ArchiveModel).filter(ArchiveModel.data_id == data_id).first()
    if not archive:
        archive = ArchiveModel(data_id=data_id, study_instance_uid=request.study_instance_uid,
                               series_instance_uid=series_instance_uid, level=request.level.value)
        db.add(archive)
    archive.status = ArchiveStatus.ARCHIVING.value
    archive.task_id = task_id
    archive.expected_image_count = expected_count
    archive.checked = False
    archive.last_error = None


def _build_data_id(level: str, study_uid: str, series_uid: Optional[str],
                   task_id: Optional[str] = None, series_list: Optional[List[Dict]] = None) -> str:
    """归档标识。study/单 series 沿用稳定 data_id（可跨任务复用同一归档行）。

    sop 级与多 Series 定向任务用 task_id 作后缀：这类任务的范围是「本次缺哪些」，
    每次都不同，若共用稳定 data_id，第二次定向补拉会覆盖上一次的期望数与计数。
    """
    multi_series = series_list is not None and len(series_list) > 1
    if level == DataLevel.SOP.value or multi_series:
        return "repull:%s" % task_id
    if level == DataLevel.SERIES.value and series_uid:
        return "series:%s:%s" % (study_uid, series_uid)
    return "study:%s" % study_uid


def refresh_task_expected(task: DownloadTask, db: Session) -> Optional[int]:
    """重新 C-FIND 刷新任务的 expected 与 Study/Series 元数据（retry_task 用）。

    保持同一 task_id、retry_times 连续累加（不新建任务）。成功返回新 expected；
    PACS 不可达 / Study 查不到等查询失败抛 PullError，调用方据此拒绝重试。
    """
    study_result, pacs_series = _load_pacs_metadata(task.source_id, task.study_instance_uid)
    if study_result:
        _upsert_study_from_pacs(db, task.source_id, study_result)
    if pacs_series:
        _upsert_series_from_pacs(db, task.source_id, pacs_series)
    expected = _recompute_expected(task, study_result, pacs_series)
    task.expected_image_number = expected
    return expected


def _recompute_expected(task: DownloadTask, study_result: Optional[StudyResult],
                        pacs_series: List[SeriesResult]) -> Optional[int]:
    """按任务自身 level/范围，用最新 C-FIND 结果重算 expected（不动 request 结构）。"""
    level = DataLevel(task.level)
    if level == DataLevel.SOP:
        # sop 级期望数只由点名 SOP 数量决定，不随 C-FIND 变。
        counts = [len(i.get("sop_instance_uid_list") or [])
                  for i in ((task.task_body or {}).get("series_list") or [])]
        return sum(counts) or None
    if level == DataLevel.SERIES:
        targets = (task.task_body or {}).get("series_list") or []
        if not targets and task.series_instance_uid:
            targets = [{"series_instance_uid": task.series_instance_uid}]
        total = 0
        for item in targets:
            series_uid = item.get("series_instance_uid")
            match = next((s for s in pacs_series if s.series_instance_uid == series_uid), None)
            if not match or match.number_of_series_related_instances is None:
                return None
            total += match.number_of_series_related_instances
        return total or None
    # study 级：Study 总数。
    return study_result.number_of_study_related_instances if study_result else None
