"""真实 PACS（Orthanc 作 fake server）集成测试（spec §9：PACS fake server）。

前置：mock DICOM 已导入 Orthanc（scripts/import_dicom_to_orthanc.py），
含 study 9.9.9.20250101000000.1（5 个 series）。本测试经 DICOM 协议
（非 REST）直连 Orthanc 4242，验证 C-ECHO / C-FIND（STUDY/SERIES/IMAGE）。
C-MOVE 需要 C-STORE 接收端（pull-data-storescp 容器），不可用则 skip 并如实记录。
"""
import subprocess

import pytest

from src.infrastructure.pacs.client import PacsClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.pacs,
]

STUDY_UID = "9.9.9.20250101000000.1"
STUDY_UID_2 = "9.9.9.20260101000000.1"


def test_pacs_echo(pacs_settings):
    """C-ECHO 握手成功 = PACS 真实可达。"""
    result = PacsClient().echo(pacs_settings)
    assert result.ok is True, result.message
    assert result.status_code == 0x0000
    assert result.latency_ms >= 0


def test_find_study_matches_imported_mock(pacs_settings):
    """C-FIND STUDY 能按 StudyInstanceUID 命中已导入语料。"""
    results = PacsClient().find_study(pacs_settings, study_instance_uid=STUDY_UID)
    assert len(results) == 1
    assert results[0].study_instance_uid == STUDY_UID
    assert results[0].number_of_study_related_instances is None or results[0].number_of_study_related_instances > 0


def test_find_series_and_instances_consistent(pacs_settings):
    """C-FIND SERIES 返回 5 个 series，IMAGE 计数与 SERIES 元数据一致。"""
    client = PacsClient()
    series_list = client.find_series(pacs_settings, STUDY_UID)
    assert len(series_list) == 5

    total = 0
    for series in series_list:
        instances = client.find_instances(pacs_settings, STUDY_UID, series.series_instance_uid)
        assert len(instances) >= 1
        expected = series.number_of_series_related_instances
        if expected is not None:
            assert len(instances) == expected
        total += len(instances)
    assert total <= 140  # 不超过 mock 语料总数


def _storescp_container() -> str:
    """返回运行中的 storescp 容器名；找不到返回空串。"""
    try:
        names = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.splitlines()
    except (subprocess.CalledProcessError, OSError):
        return ""
    for name in names:
        if "storescp" in name.lower():
            return name
    return ""


def _store_request_count(container: str) -> int:
    """统计 storescp 容器日志里 C-STORE 请求行数（本次与历史累积，取差值用）。"""
    try:
        logs = subprocess.run(
            ["docker", "logs", container],
            check=False, capture_output=True, text=True, timeout=30,
        )
        return (logs.stdout + logs.stderr).count("Received Store Request")
    except OSError:
        return 0


def test_c_move_received_by_storescp(pacs_settings, storescp_available):
    """C-MOVE E2E：仅当 C-STORE 接收端（11112）在跑时验证；否则如实 skip。

    storescp 容器收到实例即落盘（data/fileserver/<study>/<series>）并写 MySQL。
    以「容器日志中 C-STORE 请求增量 == C-MOVE completed」为同步证据——
    不依赖宿主 bind-mount 目录缓存（Docker Desktop FUSE 存在陈旧视图问题）。
    """
    if not storescp_available:
        pytest.skip("pull-data-storescp 未运行（11112 无监听），跳过 C-MOVE 端到端断言")
    container = _storescp_container()
    if not container:
        pytest.skip("未发现运行中的 storescp 容器，跳过 C-MOVE 端到端断言")

    client = PacsClient()
    series_list = client.find_series(pacs_settings, STUDY_UID_2)
    assert series_list, "前置：应先能 C-FIND 到 series"

    before = _store_request_count(container)
    target = series_list[0]
    result = client.move_series(pacs_settings, STUDY_UID_2, target.series_instance_uid)
    assert result.ok is True, result.message
    assert result.failed == 0
    expected = target.number_of_series_related_instances or 0
    assert result.completed == expected

    delta = _store_request_count(container) - before
    assert delta == expected, "storescp 本次收到 C-STORE 数应与 C-MOVE completed 一致（实际 %s 期望 %s）" % (delta, expected)
