"""MCP 薄层（spec 9.7，可选加分）。

把 9 个只读工具按 MCP 标准暴露，复用同一份工具实现，不改任何业务逻辑。
写工具 execute_repull_plan 绝不暴露——它必须走 human_approval 审批节点，脱离审批的
任何调用通道都违反安全边界（护栏 4）。

输入映射工具的 Pydantic Schema，输出序列化 BaseToolOutput。
运行：python -m app.agent.mcp_server （stdio transport）
"""
import logging

from src.agents.pacs.tools import (
    READ_ONLY_TOOL_NAMES,
    compute_integrity,
    query_missing_instances,
    query_pacs_hierarchy,
    query_pacs_target,
    query_receive_status,
    query_task_context,
    query_task_history,
    query_worker_health,
)
from src.infrastructure.rag_pipeline import search_knowledge

logger = logging.getLogger(__name__)

# 安全边界断言：写工具绝不在暴露列表中，暴露范围与只读白名单严格一致。
_EXPOSED = {
    "query_pacs_target", "query_pacs_hierarchy", "query_task_context", "query_task_history",
    "query_worker_health", "query_receive_status", "query_missing_instances",
    "compute_integrity", "search_knowledge",
}
assert "execute_repull_plan" not in _EXPOSED, "写工具不得经 MCP 暴露"
assert _EXPOSED == READ_ONLY_TOOL_NAMES, "MCP 暴露范围必须与只读工具白名单一致"


def build_mcp_server():
    """构造 FastMCP server，注册 9 个只读工具。返回 FastMCP 实例。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pacs-pulldata-agent")

    @mcp.tool()
    def mcp_query_pacs_target(
        source_id: str,
        study_instance_uid: str | None = None,
        series_instance_uid: str | None = None,
    ) -> dict:
        """检查 PACS 连通性并查询目标 Study/Series 元数据（C-ECHO + C-FIND）。"""
        return query_pacs_target(source_id, study_instance_uid, series_instance_uid).model_dump()

    @mcp.tool()
    def mcp_query_pacs_hierarchy(source_id: str, study_instance_uid: str) -> dict:
        """C-FIND SERIES 级下钻：列出 Study 下各 Series 及其期望 Instance 数。"""
        return query_pacs_hierarchy(source_id, study_instance_uid).model_dump()

    @mcp.tool()
    def mcp_query_task_context(
        task_id: str | None = None,
        study_instance_uid: str | None = None,
        series_instance_uid: str | None = None,
    ) -> dict:
        """查询补拉任务当前状态与阶段时间线。"""
        return query_task_context(task_id, study_instance_uid, series_instance_uid).model_dump()

    @mcp.tool()
    def mcp_query_task_history(
        task_id: str | None = None,
        study_instance_uid: str | None = None,
        series_instance_uid: str | None = None,
    ) -> dict:
        """查询目标历史累计重试次数与错误演变。"""
        return query_task_history(task_id, study_instance_uid, series_instance_uid).model_dump()

    @mcp.tool()
    def mcp_query_worker_health(queue_name: str | None = None) -> dict:
        """查询下载队列消费者数与积压，判断 Worker 存活/队列堵塞。"""
        return query_worker_health(queue_name).model_dump()

    @mcp.tool()
    def mcp_query_receive_status(
        study_instance_uid: str,
        series_instance_uid: str | None = None,
    ) -> dict:
        """查询 storescp 接收登记数与本地扫描结果（唯一 SOP 以本地为权威）。"""
        return query_receive_status(study_instance_uid, series_instance_uid).model_dump()

    @mcp.tool()
    def mcp_query_missing_instances(
        source_id: str,
        study_instance_uid: str,
        series_instance_uid: str,
    ) -> dict:
        """IMAGE 级差集：列出某 Series 下 PACS 有、本地缺的具体 SOPInstanceUID。"""
        return query_missing_instances(source_id, study_instance_uid, series_instance_uid).model_dump()

    @mcp.tool()
    def mcp_compute_integrity(
        expected: int | None = None,
        local_unique_sop: int = 0,
        level: str = "study",
    ) -> dict:
        """完整性算术：给定 expected 与 local_unique_sop 返回缺口。"""
        return compute_integrity(expected, local_unique_sop, level).model_dump()

    # RAG 不设 MCP 转发函数：MCP 与 StructuredTool 绑定同一个函数对象和输出模型。
    mcp.tool()(search_knowledge)

    return mcp


def main():
    logging.basicConfig(level=logging.INFO)
    server = build_mcp_server()
    server.run()  # 默认 stdio transport


if __name__ == "__main__":
    main()
