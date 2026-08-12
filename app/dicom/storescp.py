import logging
import os

from pynetdicom import AE, AllStoragePresentationContexts, evt

from app.core.config import resolve_project_path, settings
from app.core.database import session_scope
from app.core.models import StoreScpImage
from app.dicom.parser import build_dicom_file_path, get_text
from app.services import abort


logger = logging.getLogger(__name__)


def handle_store(event):
    dataset = event.dataset
    dataset.file_meta = event.file_meta

    # L4 止损闸门：影像落在止损范围内则拒收——不落盘、不写库，返回 Refused: Out of Resources。
    # 这是唯一能停住「C-MOVE 已发出、PACS 正在推」的一层（数据流不经过 downloader）。
    # 代价是磁盘上留半个 Study：已收的留着、后续的没有。这是止损的必然结果，
    # 靠计数如实交代少了多少张；checker 随后按 local < expected 判 fail，与事实一致。
    study_uid = get_text(dataset, "StudyInstanceUID")
    series_uid = get_text(dataset, "SeriesInstanceUID")
    if abort.is_image_aborted(study_uid, series_uid):
        rejected = abort.count_rejected(study_uid)
        logger.warning("止损拒收影像 study=%s series=%s sop=%s（累计拒收 %s 张）",
                       study_uid, series_uid, get_text(dataset, "SOPInstanceUID"), rejected)
        return 0xA700  # Refused: Out of Resources —— PACS 通常据此中止剩余 C-STORE

    file_path = build_dicom_file_path(resolve_project_path(settings.storage.fileserver_root), dataset)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        dataset.save_as(file_path, write_like_original=False)
        _upsert_storescp_image(dataset, file_path)
        logger.info("received dicom file: %s", file_path)
        return 0x0000
    except Exception as exc:
        logger.exception("failed to save dicom file: %s", exc)
        return 0xC001


def _upsert_storescp_image(dataset, file_path: str) -> None:
    study_uid = get_text(dataset, "StudyInstanceUID")
    series_uid = get_text(dataset, "SeriesInstanceUID")
    sop_uid = get_text(dataset, "SOPInstanceUID")
    if not series_uid or not sop_uid:
        return

    image_name = "%s.dcm" % sop_uid
    with session_scope() as db:
        image = db.query(StoreScpImage).filter(StoreScpImage.image_name == image_name).first()
        if not image:
            image = StoreScpImage(image_name=image_name)
            db.add(image)
        image.study_instance_uid = study_uid
        image.series_instance_uid = series_uid
        image.series_path = os.path.dirname(file_path)
        image.file_path = file_path


def start_storescp() -> None:
    logging.basicConfig(level=getattr(logging, settings.app.log_level.upper(), logging.INFO))
    ae = AE(ae_title=settings.storescp.ae_title)
    for context in AllStoragePresentationContexts:
        ae.add_supported_context(context.abstract_syntax)
    handlers = [(evt.EVT_C_STORE, handle_store)]
    ae.start_server((settings.storescp.host, settings.storescp.port), block=True, evt_handlers=handlers)


if __name__ == "__main__":
    start_storescp()
