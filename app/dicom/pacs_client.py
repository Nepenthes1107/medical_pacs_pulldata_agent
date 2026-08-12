import time
from typing import Any, Dict, List, Optional

from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

from app.core.config import settings
from app.core.schemas import InstanceResult, MoveResult, PacsEchoResult, SeriesResult, StudyResult


def _status_code(status: Any) -> Optional[int]:
    if not status:
        return None
    return getattr(status, "Status", None)


def _dataset_text(dataset: Dataset, key: str) -> Optional[str]:
    value = getattr(dataset, key, None)
    if value in (None, ""):
        return None
    return str(value)


def _dataset_int(dataset: Dataset, key: str) -> Optional[int]:
    value = getattr(dataset, key, None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class PacsClient(object):
    """基于 pynetdicom 的最小 PACS 客户端。"""

    def _associate(self, source_id: str, contexts: List[Any]):
        source = settings.get_pacs_source(source_id)
        ae = AE(ae_title=source.client_ae_title)
        for context in contexts:
            ae.add_requested_context(context)
        assoc = ae.associate(source.host, source.port, ae_title=source.ae_title)
        return source, assoc

    def echo(self, source_id: str) -> PacsEchoResult:
        start = time.time()
        try:
            _, assoc = self._associate(source_id, [Verification])
            if not assoc.is_established:
                return PacsEchoResult(ok=False, source_id=source_id, message="Association failed")
            status = assoc.send_c_echo()
            assoc.release()
            code = _status_code(status)
            return PacsEchoResult(
                ok=code == 0x0000,
                source_id=source_id,
                status_code=code,
                latency_ms=int((time.time() - start) * 1000),
                message="PACS reachable" if code == 0x0000 else "C-ECHO failed",
            )
        except Exception as exc:
            return PacsEchoResult(
                ok=False,
                source_id=source_id,
                latency_ms=int((time.time() - start) * 1000),
                message=str(exc),
            )

    def find_study(
        self,
        source_id: str,
        study_instance_uid: Optional[str] = None,
        study_date: Optional[str] = None,
    ) -> List[StudyResult]:
        _, assoc = self._associate(source_id, [StudyRootQueryRetrieveInformationModelFind])
        if not assoc.is_established:
            raise RuntimeError("PACS association failed")

        query = Dataset()
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = study_instance_uid or ""
        query.StudyDate = study_date or ""
        query.ModalitiesInStudy = ""
        query.NumberOfStudyRelatedInstances = ""

        results = []
        for status, identifier in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            code = _status_code(status)
            if code in (0xFF00, 0xFF01) and identifier:
                results.append(
                    StudyResult(
                        study_instance_uid=_dataset_text(identifier, "StudyInstanceUID") or "",
                        study_date=_dataset_text(identifier, "StudyDate"),
                        modality=_dataset_text(identifier, "ModalitiesInStudy"),
                        number_of_study_related_instances=_dataset_int(identifier, "NumberOfStudyRelatedInstances"),
                        raw=identifier.to_json_dict(),
                    )
                )
        assoc.release()
        return [item for item in results if item.study_instance_uid]

    def find_series(self, source_id: str, study_instance_uid: str) -> List[SeriesResult]:
        _, assoc = self._associate(source_id, [StudyRootQueryRetrieveInformationModelFind])
        if not assoc.is_established:
            raise RuntimeError("PACS association failed")

        query = Dataset()
        query.QueryRetrieveLevel = "SERIES"
        query.StudyInstanceUID = study_instance_uid
        query.SeriesInstanceUID = ""
        query.Modality = ""
        query.NumberOfSeriesRelatedInstances = ""

        results = []
        for status, identifier in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            code = _status_code(status)
            if code in (0xFF00, 0xFF01) and identifier:
                results.append(
                    SeriesResult(
                        study_instance_uid=_dataset_text(identifier, "StudyInstanceUID") or study_instance_uid,
                        series_instance_uid=_dataset_text(identifier, "SeriesInstanceUID") or "",
                        modality=_dataset_text(identifier, "Modality"),
                        number_of_series_related_instances=_dataset_int(identifier, "NumberOfSeriesRelatedInstances"),
                        raw=identifier.to_json_dict(),
                    )
                )
        assoc.release()
        return [item for item in results if item.series_instance_uid]

    def find_instances(
        self, source_id: str, study_instance_uid: str, series_instance_uid: str
    ) -> List[InstanceResult]:
        """IMAGE 级 C-FIND：列出某 Series 下 PACS 侧全部 SOPInstanceUID。

        这是「缺哪几张」的权威来源——与本地扫描做差集即得 sop 粒度补拉目标。
        """
        _, assoc = self._associate(source_id, [StudyRootQueryRetrieveInformationModelFind])
        if not assoc.is_established:
            raise RuntimeError("PACS association failed")

        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.StudyInstanceUID = study_instance_uid
        query.SeriesInstanceUID = series_instance_uid
        query.SOPInstanceUID = ""
        query.InstanceNumber = ""

        results = []
        for status, identifier in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind):
            code = _status_code(status)
            if code in (0xFF00, 0xFF01) and identifier:
                results.append(
                    InstanceResult(
                        study_instance_uid=_dataset_text(identifier, "StudyInstanceUID") or study_instance_uid,
                        series_instance_uid=_dataset_text(identifier, "SeriesInstanceUID") or series_instance_uid,
                        sop_instance_uid=_dataset_text(identifier, "SOPInstanceUID") or "",
                        instance_number=_dataset_int(identifier, "InstanceNumber"),
                        raw=identifier.to_json_dict(),
                    )
                )
        assoc.release()
        return [item for item in results if item.sop_instance_uid]

    def move_study(self, source_id: str, study_instance_uid: str, should_abort=None) -> MoveResult:
        return self._move(source_id, "STUDY", study_instance_uid, should_abort=should_abort)

    def move_series(self, source_id: str, study_instance_uid: str, series_instance_uid: str,
                    should_abort=None) -> MoveResult:
        return self._move(source_id, "SERIES", study_instance_uid, series_instance_uid,
                          should_abort=should_abort)

    def move_instance(
        self, source_id: str, study_instance_uid: str, series_instance_uid: str, sop_instance_uid: str,
        should_abort=None,
    ) -> MoveResult:
        """IMAGE 级 C-MOVE：只取单张 SOP（真 instance 粒度定向补拉）。

        一次 move 一个 SOP，不做多值 UID 匹配——各家 PACS 对多值 UID 支持不一，
        逐张发起虽多几次关联，但行为确定、失败可精确定位到具体 SOP。
        """
        return self._move(source_id, "IMAGE", study_instance_uid, series_instance_uid, sop_instance_uid,
                          should_abort=should_abort)

    def _move(
        self,
        source_id: str,
        level: str,
        study_instance_uid: str,
        series_instance_uid: Optional[str] = None,
        sop_instance_uid: Optional[str] = None,
        should_abort=None,
    ) -> MoveResult:
        start = time.time()
        source, assoc = self._associate(source_id, [StudyRootQueryRetrieveInformationModelMove])
        if not assoc.is_established:
            return MoveResult(
                ok=False,
                source_id=source_id,
                level=level.lower(),
                study_instance_uid=study_instance_uid,
                series_instance_uid=series_instance_uid,
                sop_instance_uid=sop_instance_uid,
                message="PACS association failed",
            )

        query = Dataset()
        query.QueryRetrieveLevel = level
        query.StudyInstanceUID = study_instance_uid
        if series_instance_uid:
            query.SeriesInstanceUID = series_instance_uid
        if sop_instance_uid:
            query.SOPInstanceUID = sop_instance_uid

        # C-MOVE 会先回若干 Pending（0xFF00/0xFF01）进度状态，最后回一个最终状态。
        # 只有最终状态才能判定成败：last_final_code 记录“最后一个非 Pending 状态”，
        # Pending 绝不能作为最终成功（原实现的 bug）。
        last_code = None
        last_final_code = None
        saw_pending = False
        aborted = False
        completed = failed = warning = 0
        msg_id = 1
        for status, _ in assoc.send_c_move(
            query,
            source.move_destination_ae_title,
            StudyRootQueryRetrieveInformationModelMove,
            msg_id=msg_id,
        ):
            code = _status_code(status)
            last_code = code
            if code in (0xFF00, 0xFF01):
                saw_pending = True
                # L3 止损闸门：每收到一个 Pending 就问一次是否已被止损，
                # 是则发 C-CANCEL 请 PACS 停掉剩余子操作。PACS 是否遵从由其实现决定，
                # 故只标记 aborted、不谎报已停干净——真正兜底在 storescp 拒收（L4）。
                if should_abort is not None and should_abort():
                    try:
                        assoc.send_c_cancel(msg_id, query_model=StudyRootQueryRetrieveInformationModelMove)
                        aborted = True
                    except Exception:  # noqa: BLE001
                        aborted = True  # 发 cancel 失败也照常跳出，不再等剩余 Pending
                    break
            else:
                last_final_code = code
            completed = getattr(status, "NumberOfCompletedSuboperations", completed) or completed
            failed = getattr(status, "NumberOfFailedSuboperations", failed) or failed
            warning = getattr(status, "NumberOfWarningSuboperations", warning) or warning
        assoc.release()

        # 止损中止：不算成功也不算普通失败，如实标注已中止（区别于 PACS 侧真失败）。
        if aborted:
            return MoveResult(
                ok=False, source_id=source_id, level=level.lower(),
                study_instance_uid=study_instance_uid, series_instance_uid=series_instance_uid,
                sop_instance_uid=sop_instance_uid, status_code=last_code,
                completed=completed, failed=failed, warning=warning,
                latency_ms=int((time.time() - start) * 1000),
                message="C-MOVE aborted by operator (C-CANCEL sent); already-sent images may still arrive",
            )

        # 判定口径：
        # - 0x0000 且无 failed 子操作 → 最终成功；
        # - 0xB000（部分子操作完成的 Warning）且无 failed 子操作 → 可接受 Warning；
        # - 只收到 Pending 未收到最终状态 → 不算成功（连接中断/超时）；
        # - 其余（含 failed>0）→ 失败。
        final_code = last_final_code
        if final_code == 0x0000 and failed == 0:
            ok, message = True, "C-MOVE completed"
        elif final_code == 0xB000 and failed == 0:
            ok, message = True, "C-MOVE completed with warning"
        elif final_code is None and saw_pending:
            ok, message = False, "C-MOVE returned only Pending, no final status (aborted/timeout)"
        else:
            ok, message = False, "C-MOVE failed: status=%s failed=%s" % (
                hex(final_code) if final_code is not None else hex(last_code) if last_code is not None else None,
                failed,
            )
        return MoveResult(
            ok=ok,
            source_id=source_id,
            level=level.lower(),
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            sop_instance_uid=sop_instance_uid,
            status_code=final_code if final_code is not None else last_code,
            completed=completed,
            failed=failed,
            warning=warning,
            latency_ms=int((time.time() - start) * 1000),
            message=message,
        )
