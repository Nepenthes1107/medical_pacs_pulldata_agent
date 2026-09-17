"""Single LLM factory for the platform and migrated domain agents."""
import os
from functools import lru_cache

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from src.core.settings import settings


def stable_prompt(system_prompt: str, dynamic_content: str):
    return [SystemMessage(content=system_prompt), HumanMessage(content=dynamic_content)]


def contextual_prompt(system_prompt: str, state, dynamic_content: str):
    from src.agents.pacs.prompts import xml_block
    summary = state.get("context_summary", "") or ""
    parts = [xml_block("conversation_summary", summary)] if summary else []
    parts.append(dynamic_content)
    return stable_prompt(system_prompt, "\n\n".join(parts))


def reasoning_prompt(system_prompt: str, state, dynamic_content: str):
    history: list[BaseMessage] = []
    for message in state.get("messages", []):
        if isinstance(message, (HumanMessage, ToolMessage)):
            history.append(message)
        elif isinstance(message, AIMessage) and message.tool_calls:
            history.append(message)
    return [SystemMessage(content=system_prompt), *history, HumanMessage(content=dynamic_content)]


def configure_langsmith() -> None:
    ls = settings.langsmith
    if ls.tracing_enabled:
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", ls.project)
        os.environ.setdefault("LANGCHAIN_PROJECT", ls.project)
    else:
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"


@lru_cache
def get_chat_model():
    from langchain_openai import ChatOpenAI
    cfg = settings.llm
    if cfg.provider not in {"openai-compatible", "dashscope", "openai"}:
        raise RuntimeError(f"unsupported LLM provider: {cfg.provider}")
    if not cfg.api_key:
        raise RuntimeError("LLM api_key 未配置：请设置环境变量 LLM_API_KEY 或 DASHSCOPE_API_KEY")
    configure_langsmith()
    return ChatOpenAI(
        model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key,
        temperature=cfg.temperature, timeout=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
    )


@lru_cache
def get_reasoning_model():
    from src.agents.pacs.tools import READ_ONLY_TOOLS
    return get_chat_model().bind_tools(READ_ONLY_TOOLS)


def get_structured_model(schema, *, include_raw: bool = False):
    return get_chat_model().with_structured_output(schema, include_raw=include_raw)


def llm_available() -> bool:
    return bool(settings.llm.api_key)


__all__ = [
    "configure_langsmith", "contextual_prompt", "get_chat_model", "get_reasoning_model",
    "get_structured_model", "llm_available", "reasoning_prompt", "stable_prompt",
]
