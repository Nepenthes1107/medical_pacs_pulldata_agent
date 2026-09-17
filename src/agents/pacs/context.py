"""Token Budget + Summary + Recent Raw Messages 上下文管理。"""
from collections.abc import Iterable

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage

from src.agents.pacs.state import AgentState
from src.core.settings import settings


def _text(message: AnyMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else str(content)


def _encoding():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(parts: Iterable[str]) -> int:
    encoding = _encoding()
    return sum(len(encoding.encode(part or "")) for part in parts)


def _render(summary: str, messages: list[AnyMessage]) -> str:
    recent = "\n".join("%s: %s" % (m.type, _text(m)) for m in messages)
    blocks = []
    if summary:
        blocks.append("## Earlier conversation summary\n" + summary)
    if recent:
        blocks.append("## Recent raw messages\n" + recent)
    return "\n\n".join(blocks)


def _summarize(previous: str, older: list[AnyMessage]) -> str:
    transcript = "\n".join("%s: %s" % (m.type, _text(m)) for m in older)
    from src.agents.pacs.prompts import get_system_prompt, xml_blocks
    from src.core.llm import get_chat_model, llm_available, stable_prompt

    if llm_available():
        response = get_chat_model().invoke(stable_prompt(
            get_system_prompt("context_summary"),
            xml_blocks([
                ("existing_summary", previous or "（无）"),
                ("messages_to_summarize", transcript),
            ]),
        ))
        summary = _text(response)
    else:
        summary = (previous + "\n" + transcript).strip()

    budget = settings.agent.context_summary_budget
    encoding = _encoding()
    tokens = encoding.encode(summary)
    return encoding.decode(tokens[-budget:]) if len(tokens) > budget else summary


def _recent_start(messages: list[AnyMessage], count: int) -> int:
    """扩展窗口起点，确保 ToolMessage 不会脱离其工具请求 AIMessage。"""
    start = max(0, len(messages) - count)
    if start < len(messages):
        candidate = messages[start]
        if isinstance(candidate, ToolMessage):
            target = candidate.tool_call_id
        else:
            return start
        for index in range(start - 1, -1, -1):
            candidate = messages[index]
            if isinstance(candidate, AIMessage) and any(
                call.get("id") == target for call in candidate.tool_calls
            ):
                return index
    return start


def _message_ids(messages: list[AnyMessage]) -> list[str]:
    """收集存在 message id 的移除目标；langchain 的 RemoveMessage id 不允许 None。"""
    ids: list[str] = []
    for message in messages:
        message_id = getattr(message, "id", None)
        if message_id is not None:
            ids.append(message_id)
    return ids


def _leading_group(messages: list[AnyMessage]) -> list[AnyMessage]:
    """移除窗口首条工具请求时，同时移除它的所有配对 ToolMessage。"""
    if not messages:
        return []
    first = messages[0]
    group = [first]
    if isinstance(first, AIMessage) and first.tool_calls:
        ids = {call.get("id") for call in first.tool_calls}
        for message in messages[1:]:
            if isinstance(message, ToolMessage) and message.tool_call_id in ids:
                group.append(message)
                continue
            break
    return group


def manage_context(state: AgentState) -> dict:
    """优先摘要较早消息；仍超预算时由最近消息窗口兜底。"""
    messages = list(state.get("messages", []))
    summary = state.get("context_summary", "") or ""
    budget = settings.agent.context_token_budget
    recent_count = max(2, settings.agent.recent_message_count)

    if count_tokens([summary, *(_text(m) for m in messages)]) > budget and len(messages) > recent_count:
        start = _recent_start(messages, recent_count)
        older, messages = messages[:start], messages[start:]
        summary = _summarize(summary, older)
        removals = [RemoveMessage(id=message_id) for message_id in _message_ids(older)]
    else:
        removals = []

    # Sliding window 仅是最终兜底：从最早的 raw message 开始移除，保留至少最近两条。
    while len(messages) > 2 and count_tokens([summary, *(_text(m) for m in messages)]) > budget:
        removed_group = _leading_group(messages)
        del messages[:len(removed_group)]
        removals.extend(RemoveMessage(id=message_id) for message_id in _message_ids(removed_group))

    context = _render(summary, messages)
    return {
        "messages": removals,
        "context_summary": summary,
        "conversation_context": context,
        "context_token_count": count_tokens([context]),
    }


def capture_response(state: AgentState) -> dict:
    """把本轮可见结果作为原始 assistant message 写回短期记忆。"""
    if state.get("action_result"):
        content = "审批执行结果：%s" % state["action_result"]
    elif state.get("clarification"):
        content = state["clarification"] or ""
    else:
        diagnosis = state.get("diagnosis") or {}
        content = diagnosis.get("summary") or "本轮任务已处理。"
    return {"messages": [AIMessage(content=content)]}


def new_user_message(content: str) -> HumanMessage:
    return HumanMessage(content=content or "（结构化任务请求）")
