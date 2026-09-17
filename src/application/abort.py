"""紧急止损标记（误批大范围补拉的止损通道）。

C-MOVE 的数据流不经过发起方：downloader 发请求，PACS 直接 C-STORE 推给 storescp。
所以止损不是一个开关，而是四道递减确定性的闸门，靠 Redis 标记跨进程共享：

    L1 队列前   downloader._begin_downloading  → 还在排队的任务，100% 拦住
    L2 目标间   downloader._run_move           → 多目标任务的剩余目标，大范围补拉主要收益
    L3 DICOM 级 pacs_client._move              → 当前 C-MOVE 的剩余子操作（看 PACS 是否遵从）
    L4 接收端   storescp.handle_store          → 正在推过来的影像，唯一能停接收的一层

标记存 Redis 而非 DB：storescp 的 handle_store 是每张影像都要过的热路径，
Redis GET 比 DB 查询轻得多；且三个进程本来就共享同一个 Redis（幂等锁已在用）。

Redis 不可用时 is_aborted 返回 False（放行接收）——止损是例外流程，
不能让 Redis 故障把正常补拉全部拒收。API 侧会如实告知止损可能不生效，不伪装。
"""
import logging

from src.core.settings import settings

logger = logging.getLogger(__name__)

# task 级标记：downloader 的 L1/L2/L3 按 task_id 判断是否中止。
_TASK_KEY = "abort:task:%s"
# study 级标记：storescp 只认 DICOM 里的 UID，不知道 task_id，故 L4 按 study 判断。
_STUDY_KEY = "abort:study:%s"
# series 级标记：定向补拉只止损特定 Series 时用，避免整 Study 拒收。
_SERIES_KEY = "abort:series:%s"
# 被拒收计数：止损后有多少张影像被挡在门外，供事后交代磁盘上少了什么。
_REJECTED_KEY = "abort:rejected:%s"


def _client():
    import redis

    return redis.Redis.from_url(settings.redis.url)


def mark_aborted(
    task_id: str,
    study_instance_uid: str | None = None,
    series_instance_uids: list[str] | None = None,
) -> bool:
    """打止损标记。返回 True 表示标记已写入（四层闸门生效）。

    series_instance_uids 非空 → 只止损这些 Series（定向补拉场景，不波及整 Study 其他数据）；
    为空 → 按 study 止损（整 Study 补拉场景）。
    写失败返回 False，调用方必须据此告知「止损可能不生效」，不得伪装成功。
    """
    ttl = settings.agent.abort_flag_ttl_seconds
    try:
        client = _client()
        pipe = client.pipeline()
        pipe.set(_TASK_KEY % task_id, "1", ex=ttl)
        # 定向止损只标 Series；否则标整个 Study。两者互斥，避免误伤同 Study 的正常补拉。
        if series_instance_uids:
            for uid in series_instance_uids:
                pipe.set(_SERIES_KEY % uid, "1", ex=ttl)
        elif study_instance_uid:
            pipe.set(_STUDY_KEY % study_instance_uid, "1", ex=ttl)
        pipe.execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("止损标记写入失败，止损可能不生效 task_id=%s: %s", task_id, exc)
        return False


def is_task_aborted(task_id: str | None) -> bool:
    """L1/L2/L3 用：该补拉任务是否已被止损。Redis 不可用时返回 False（放行）。"""
    if not task_id:
        return False
    try:
        return bool(_client().exists(_TASK_KEY % task_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("止损标记读取失败，按未止损处理 task_id=%s: %s", task_id, exc)
        return False


def is_image_aborted(study_instance_uid: str | None, series_instance_uid: str | None) -> bool:
    """L4 用：这张影像是否落在止损范围内（storescp 每张影像都会调，需轻量）。

    Series 标记优先于 Study：定向止损时只拒该 Series，同 Study 其他 Series 照常接收。
    """
    if not study_instance_uid and not series_instance_uid:
        return False
    try:
        client = _client()
        if series_instance_uid and client.exists(_SERIES_KEY % series_instance_uid):
            return True
        return bool(study_instance_uid and client.exists(_STUDY_KEY % study_instance_uid))
    except Exception as exc:  # noqa: BLE001
        logger.warning("止损标记读取失败，按未止损接收影像: %s", exc)
        return False


def count_rejected(study_instance_uid: str | None) -> int:
    """记一张被拒收的影像，返回累计数。失败返回 0（不阻断拒收本身）。"""
    if not study_instance_uid:
        return 0
    try:
        client = _client()
        key = _REJECTED_KEY % study_instance_uid
        count = client.incr(key)
        client.expire(key, settings.agent.abort_flag_ttl_seconds)
        return int(count)
    except Exception:  # noqa: BLE001
        return 0


def rejected_count(study_instance_uid: str | None) -> int:
    """读被拒收张数（供接口如实交代磁盘上少了多少）。"""
    if not study_instance_uid:
        return 0
    try:
        value = _client().get(_REJECTED_KEY % study_instance_uid)
        return int(value) if value else 0
    except Exception:  # noqa: BLE001
        return 0


def clear_abort(
    task_id: str,
    study_instance_uid: str | None = None,
    series_instance_uids: list[str] | None = None,
) -> None:
    """清除止损标记（止损是临时状态，重新补拉同一目标前必须清，否则新任务也被拒收）。"""
    keys = []
    if task_id:
        keys.append(_TASK_KEY % task_id)
    if study_instance_uid:
        keys.append(_STUDY_KEY % study_instance_uid)
    for uid in series_instance_uids or []:
        keys.append(_SERIES_KEY % uid)
    if not keys:
        return
    try:
        _client().delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("止损标记清除失败 task_id=%s: %s", task_id, exc)


