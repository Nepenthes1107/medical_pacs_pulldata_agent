from app.api.main import app


def _registered_paths(router) -> set[str]:
    """收集最终注册的 route path。

    FastAPI 0.141+ 的 include_router 生成惰性 ``_IncludedRouter``（无 .path），
    真实 APIRoute 需沿 ``original_router.routes`` 递归展开。
    """
    paths: set[str] = set()
    for route in getattr(router, "routes", []):
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        else:
            paths |= _registered_paths(getattr(route, "original_router", None))
    return paths


def test_platform_service_routes_are_registered():
    paths = _registered_paths(app)
    assert "/agents/info" in paths
    assert "/agents/{agent_id}/invoke" in paths
    assert "/agents/{agent_id}/stream" in paths
    assert "/agents/{agent_id}/history" in paths
    assert "/agents/{agent_id}/threads" in paths
    assert "/pacs/runs" in paths
    assert "/pacs/runs/{run_id}/approval" in paths


def test_legacy_stream_is_marked_for_migration():
    from app.api.routes_agent import stream_run

    assert stream_run.__doc__ and "SSE" in stream_run.__doc__


def test_legacy_agent_routes_are_disabled_by_default():
    paths = _registered_paths(app)
    assert "/agent/chat" not in paths
    assert "/agent/runs/{run_id}/stream" not in paths


def test_unknown_agent_is_rejected_before_streaming():
    from fastapi import HTTPException

    from src.service.agent_routes import _require_agent

    try:
        _require_agent("does-not-exist")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("unknown agents must return 404")


def test_pacs_domain_routes_are_the_default_business_boundary():
    paths = _registered_paths(app)
    assert "/pacs/runs" in paths
    assert "/pacs/runs/{run_id}/approval" in paths
    assert "/agent/chat" not in paths
