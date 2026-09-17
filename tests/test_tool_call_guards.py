"""工具调用幻觉最小整改的回归测试。

覆盖：注册表白名单、执行前参数校验、空结果与失败的区分、异常收敛为安全摘要、
不可重试错误码不进入程序化重试、非法工具输出不进正常成功证据。
"""
import pytest

from app.agent.nodes import loop
from app.agent.tool_schemas import BaseToolOutput, ComputeIntegrityOutput
from app.agent.tools import TOOL_REGISTRY

# --- 注册表白名单 ---------------------------------------------------------

def test_unknown_tool_is_rejected_without_execution():
    out = loop._run_tool_call({"name": "query_everything", "args": {"x": 1}})

    assert out["success"] is False
    assert out["error_code"] == "tool_not_allowed"
    assert out["retryable"] is False
    assert out["correction"]


def test_write_tool_not_in_registry():
    assert "execute_repull_plan" not in TOOL_REGISTRY
    assert loop._run_tool_call({"name": "execute_repull_plan", "args": {}})["error_code"] == "tool_not_allowed"


def test_registry_matches_bound_tools():
    from app.agent.tools import READ_ONLY_TOOL_NAMES

    # 模型可见工具与工程允许执行的工具必须是同一份，避免分叉给幻觉留门。
    assert set(TOOL_REGISTRY) == READ_ONLY_TOOL_NAMES


# --- 执行前参数校验（不触达外部依赖）--------------------------------------

def test_extra_argument_is_rejected():
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 5, "local_unique_sop": 2, "level": "study", "bogus": 1},
    })

    assert out["error_code"] == "invalid_arguments"
    assert "bogus" in out["error"]
    assert out["retryable"] is False


def test_illegal_level_enum_is_rejected():
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 5, "local_unique_sop": 2, "level": "image"},
    })

    assert out["error_code"] == "invalid_arguments"
    assert "level" in out["error"]


def test_negative_count_is_rejected():
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": -1, "local_unique_sop": 0, "level": "study"},
    })

    assert out["error_code"] == "invalid_arguments"


@pytest.mark.parametrize("top_n", [0, 11])
def test_top_n_out_of_range_is_rejected(top_n):
    out = loop._run_tool_call({"name": "search_knowledge", "args": {"query": "abc", "top_n": top_n}})

    assert out["error_code"] == "invalid_arguments"


def test_missing_required_argument_is_rejected():
    out = loop._run_tool_call({"name": "query_pacs_hierarchy", "args": {"source_id": "orthanc-local"}})

    assert out["error_code"] == "invalid_arguments"
    assert "study_instance_uid" in out["error"]


def test_empty_string_uid_is_rejected():
    out = loop._run_tool_call({
        "name": "query_receive_status", "args": {"study_instance_uid": "   "},
    })

    assert out["error_code"] == "invalid_arguments"


def test_task_query_requires_at_least_one_locator():
    out = loop._run_tool_call({"name": "query_task_context", "args": {}})

    assert out["error_code"] == "invalid_arguments"
    assert out["correction"]


def test_series_pacs_query_requires_parent_study():
    out = loop._run_tool_call({
        "name": "query_pacs_target",
        "args": {"source_id": "orthanc-local", "series_instance_uid": "1.2.3.4.5"},
    })

    assert out["error_code"] == "invalid_arguments"


def test_arg_violations_do_not_echo_input_values():
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 5, "local_unique_sop": 2, "level": "SECRET-VALUE"},
    })

    assert "SECRET-VALUE" not in out["error"]


def test_valid_call_still_works():
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 5, "local_unique_sop": 2, "level": "series"},
    })

    assert out["success"] is True
    assert out["result_status"] == "ok"
    assert out["missing"] == 3
    assert out["error_code"] is None


# --- 空结果 vs 查询失败 ---------------------------------------------------

def test_empty_result_is_success_not_failure(monkeypatch):
    from app.agent import tools

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def all(self):
            return []

    class _FakeSession:
        def query(self, *a, **k):
            return _FakeQuery()

    import contextlib

    @contextlib.contextmanager
    def fake_scope():
        yield _FakeSession()

    monkeypatch.setattr(tools, "session_scope", fake_scope)
    out = tools.query_task_context(task_id="T-1")

    assert out.success is True
    assert out.result_status == "empty"
    assert out.tasks == []
    assert out.error_code is None


def test_dependency_error_returns_safe_summary(monkeypatch):
    from app.agent import tools

    def boom():
        raise RuntimeError("mysql://root:p@ssw0rd@10.0.0.9:3306/pacs connection refused")

    monkeypatch.setattr(tools, "session_scope", boom)
    out = tools.query_task_context(task_id="T-1")
    dumped = out.model_dump()

    assert out.success is False
    assert out.result_status == "unavailable"
    assert out.error_code == "dependency_unavailable"
    assert out.retryable is True
    # 安全收敛：连接串/凭据/主机不得出现在返回值任何字段里。
    blob = str(dumped)
    assert "p@ssw0rd" not in blob and "10.0.0.9" not in blob and "mysql://" not in blob


def test_non_transient_error_is_query_failed(monkeypatch):
    from app.agent import tools

    def boom():
        raise ValueError("bad column")

    monkeypatch.setattr(tools, "session_scope", boom)
    out = tools.query_task_history(task_id="T-1")

    assert out.result_status == "error"
    assert out.error_code == "query_failed"
    assert out.retryable is False


# --- 输出契约 -------------------------------------------------------------

def test_result_status_derived_from_success():
    assert ComputeIntegrityOutput(success=True).result_status == "ok"
    assert ComputeIntegrityOutput(success=False).result_status == "error"
    # 自相矛盾的组合被拦下：失败不得声称 ok/empty。
    assert ComputeIntegrityOutput(success=False, result_status="empty").result_status == "error"


def test_invalid_tool_output_is_converted(monkeypatch):
    tool = TOOL_REGISTRY["compute_integrity"]
    monkeypatch.setattr(tool, "func", lambda **kw: {"success": True, "missing": 0})

    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 1, "local_unique_sop": 0, "level": "study"},
    })

    assert out["success"] is False
    assert out["error_code"] == "invalid_tool_output"
    assert out["retryable"] is False


def test_unexpected_tool_exception_is_converted(monkeypatch):
    tool = TOOL_REGISTRY["compute_integrity"]

    def boom(**kw):
        raise RuntimeError("unexpected 10.0.0.9 secret")

    monkeypatch.setattr(tool, "func", boom)
    out = loop._run_tool_call({
        "name": "compute_integrity",
        "args": {"expected": 1, "local_unique_sop": 0, "level": "study"},
    })

    assert out["error_code"] == "tool_execution_failed"
    assert "secret" not in out["error"] and "10.0.0.9" not in out["error"]


def test_every_registered_tool_forbids_unknown_arguments():
    from app.agent.tool_schemas import BaseToolInput

    for name, tool in TOOL_REGISTRY.items():
        assert issubclass(tool.args_schema, BaseToolInput), name
        assert tool.args_schema.model_config.get("extra") == "forbid", name
    assert issubclass(ComputeIntegrityOutput, BaseToolOutput)


def test_tool_schema_is_generated_from_pydantic_metadata():
    schema = TOOL_REGISTRY["compute_integrity"].args_schema.model_json_schema()

    assert set(schema["required"]) == {"local_unique_sop"}
    assert schema["properties"]["expected"]["description"]
    assert schema["properties"]["level"]["enum"] == ["study", "series", "sop"]
