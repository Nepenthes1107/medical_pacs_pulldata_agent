"""Chat model 装配（DashScope OpenAI 兼容）与 bind_tools（spec §4.3）。

- 只把 8 个只读工具绑定给模型；写工具 execute_repull_plan 绝不绑定（必经审批节点，护栏 4）。
- 模型在 key 缺失时不阻断导入：get_chat_model() 实际调用时才校验 key。
- LangSmith tracing 由配置开关控制，仅作可观测层，不在关键路径（护栏 7 旁路）。
"""
import logging
import os
from functools import lru_cache
from typing import Optional

from app.agent.tools import READ_ONLY_TOOLS
from app.core.config import settings

logger = logging.getLogger(__name__)


def configure_langsmith() -> None:
    """按配置把 LangSmith tracing 开关写入环境变量（SDK 直接读环境变量）。"""
    ls = settings.langsmith
    if ls.tracing_enabled:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", ls.project)
        os.environ.setdefault("LANGCHAIN_PROJECT", ls.project)
    else:
        # 显式关闭，避免误启用产生外呼。
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"


@lru_cache()
def get_chat_model():
    """构造 DashScope 兼容的 ChatOpenAI；key 缺失时报错（仅在实际调用时触发）。"""
    from langchain_openai import ChatOpenAI

    cfg = settings.llm
    if not cfg.api_key:
        raise RuntimeError(
            "LLM api_key 未配置：请设置环境变量 DASHSCOPE_API_KEY（key 不写入 yaml）"
        )
    configure_langsmith()
    return ChatOpenAI(
        model=cfg.model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        timeout=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
    )


@lru_cache()
def get_reasoning_model():
    """绑定 8 个只读工具的模型，用于 reason 节点自主决策下一步 tool_call。"""
    return get_chat_model().bind_tools(READ_ONLY_TOOLS)


def get_structured_model(schema):
    """结构化输出模型（diagnose / reflect / plan_repull 用）。schema 为 Pydantic 模型类。"""
    return get_chat_model().with_structured_output(schema)


def llm_available() -> bool:
    """LLM 是否可用（key 是否就位）——供 Fallback 判定，不触发外呼。"""
    return bool(settings.llm.api_key)
