"""可观测性（spec 12.4）：LangSmith tracing 开关 + 本地 Run 指标收集。

LangSmith 仅作可观测层，不在关键路径：不可用不影响主流程。
本地 RunMetrics 提供不依赖 LangSmith 的轻量基线（节点耗时、LLM 调用次数、token 估算），
便于在没有 LangSmith key 的环境也能读出优化收益。
"""
import logging
from dataclasses import dataclass, field

from src.core.settings import settings

logger = logging.getLogger(__name__)


def langsmith_enabled() -> bool:
    return bool(settings.langsmith.tracing_enabled)


@dataclass
class RunMetrics:
    """单次 Run 的本地指标基线。"""

    run_id: str = "adhoc"
    llm_calls: int = 0
    total_tokens: int = 0
    node_latency_ms: dict[str, float] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)

    def record_node(self, name: str, latency_ms: float) -> None:
        self.node_latency_ms[name] = round(latency_ms, 2)

    def record_llm(self, tokens: int = 0) -> None:
        self.llm_calls += 1
        self.total_tokens += max(tokens, 0)

    def record_tool(self, name: str) -> None:
        self.tool_calls.append(name)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "llm_calls": self.llm_calls,
            "total_tokens": self.total_tokens,
            "tool_calls": len(self.tool_calls),
            "node_latency_ms": self.node_latency_ms,
            "total_latency_ms": round(sum(self.node_latency_ms.values()), 2),
        }
