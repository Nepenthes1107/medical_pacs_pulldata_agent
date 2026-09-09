"""生成 mock DICOM 数据集，用于补拉诊断实验（不依赖真实影像）。

每个实例是一个最小合法的 Secondary Capture Image：
- 只含关键元数据标签（Study/Series/SOPInstanceUID、InstanceNumber、Modality 等）
- 1x1 灰度像素占位，单文件约 2~3 KB
- SOPClassUID 用 Secondary Capture Image Storage（Orthanc/pynetdicom 均支持）

用法：
  python scripts/generate_mock_dicom.py \
    --study-uid 9.9.9.20250101000000.1 \
    --num-series 5 \
    --instances-per-series 20 \
    --output-dir data/dicom_mock
"""
import argparse
import os
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

# Secondary Capture Image Storage：所有 PACS 均接受的通用存储类，最适合 mock。
SOP_CLASS = SecondaryCaptureImageStorage


def build_instance(study_uid: str, series_uid: str, sop_uid: str, instance_number: int) -> FileDataset:
    """构造一个最小合法的 DICOM 实例。"""
    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = SOP_CLASS
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = pydicom.uid.PYDICOM_IMPLEMENTATION_UID

    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = SOP_CLASS
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = "OT"
    ds.StudyDate = "20250101"
    ds.SeriesDate = "20250101"
    ds.ContentDate = "20250101"
    ds.InstanceNumber = instance_number
    # 1x1 灰度像素占位
    ds.Rows = 1
    ds.Columns = 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = b"\x00"
    return ds


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate mock DICOM study for repull-diagnosis experiments.")
    parser.add_argument("--study-uid", default="9.9.9.20250101000000.1")
    parser.add_argument("--num-series", type=int, default=5)
    parser.add_argument("--instances-per-series", type=int, default=20)
    parser.add_argument("--output-dir", default="data/dicom_mock")
    args = parser.parse_args()

    root = Path(args.output_dir) / args.study_uid
    total = 0
    for s in range(1, args.num_series + 1):
        series_uid = "%s.%d" % (args.study_uid, s)
        series_dir = root / series_uid
        series_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, args.instances_per_series + 1):
            sop_uid = "%s.%d.%d" % (series_uid, i, 1)
            ds = build_instance(args.study_uid, series_uid, sop_uid, i)
            out_path = series_dir / ("%s.dcm" % sop_uid)
            ds.save_as(str(out_path), write_like_original=False)
            total += 1
    print("生成完成: study=%s series=%d instances=%d -> %s"
          % (args.study_uid, args.num_series, total, root))


if __name__ == "__main__":
    main()
