"""路由与目标解析节点（spec 6.3 / 8）。

route_request：显式标识/规则优先，歧义才调用 LLM（parse_with_llm）。
resolve_target：解析诊断层级，Series 补齐所属 Study，处理层级优先规则与归属冲突。
"""
import logging
import re
from typing import Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.rag.pipeline import search_knowledge
from app.agent.state import AgentState
from app.core.database import session_scope
from app.core.enums import DataLevel
from app.core.models import DownloadTask, SeriesModel

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UID_RE = re.compile(r"\b(?:\d+\.){3,}\d+\b")
# 知识问答的意图关键词（规则优先，命中即走 knowledge_qa）。
_QA_HINTS = ("什么是", "如何", "怎么", "为什么", "原理", "介绍", "解释一下", "what is", "how to", "why")
# 诊断意图词（须先于首拉词判定：「没拉全」含「拉」，会误命中首拉）。
_DIAGNOSIS_HINTS = ("没拉全", "拉全", "不全", "缺失", "排查", "诊断", "为什么没", "查一下", "检查",
                    "missing", "diagnose", "incomplete", "not complete")
# 首次拉取意图词。
_FIRST_PULL_HINTS = ("拉取", "下载", "拉一下", "拉个", "获取", "pull", "download", "fetch", "retrieve")


def _extract(pattern, text: str) -> Optional[str]:
    m = pattern.search(text or "")
    return m.group(0) if m else None


_VALID_ROUTES = {"diagnosis", "knowledge_qa", "clarification"}


def parse_with_llm(message: str, context: str = "") -> Optional[Dict]:
    """规则抽不出标识时的 LLM 兜底：意图识别 + 实体抽取，产出结构化路由字段。

    返回 None 表示 LLM 不可用/调用失败，交由 route_request 落到规则澄清出口
    （与 plan_tools / retrieve_and_explain 同款 Fallback 纪律，绝不阻断图）。
    抽取的标识必须能在原文中找到（子串校验），否则视为幻觉丢弃。
    """
    from app.agent.llm import get_structured_model, llm_available, stable_prompt

    if not llm_available():
        return None

    try:
        from app.agent.prompts import get_system_prompt, xml_blocks
        from app.agent.tool_schemas import RouteDecision

        model = get_structured_model(RouteDecision, include_raw=True)
        response = model.invoke(stable_prompt(
            get_system_prompt("routing"),
            xml_blocks([
                ("conversation_summary", context),
                ("user_message", message),
            ]),
        ))
        if isinstance(response, dict) and "parsed" in response:
            if response.get("parsing_error"):
                raise response["parsing_error"]
            decision = response.get("parsed")
            if decision is None:
                raise ValueError("routing structured response has no parsed value")
            raw_messages = [response["raw"]] if isinstance(response.get("raw"), AIMessage) else []
        else:
            decision, raw_messages = response, []
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_with_llm LLM 调用失败，退回规则澄清: %s", exc)
        return None

    route = decision.route if decision.route in _VALID_ROUTES else "clarification"

    if route != "diagnosis":
        patch = {"route": route, "parse_source": "llm", "messages": raw_messages}
        if route == "clarification":
            patch["clarification"] = (
                decision.clarification
                or "请提供 task_id 或 StudyInstanceUID / SeriesInstanceUID 以便定位诊断对象。"
            )
        return patch

    # diagnosis：逐个标识做原文子串校验，过滤模型编造的 UID / task_id。
    patch: Dict = {"route": "diagnosis", "parse_source": "llm", "messages": raw_messages}
    for field in ("task_id", "study_instance_uid", "series_instance_uid"):
        value = getattr(decision, field, None)
        if value and value in message:
            patch[field] = value

    # 判为诊断却没抽到任何可定位标识 → 转澄清，避免下游空跑 DB。
    if not any(k in patch for k in ("task_id", "study_instance_uid", "series_instance_uid")):
        return {
            "route": "clarification",
            "parse_source": "llm",
            "messages": raw_messages,
            "clarification": decision.clarification
            or "请提供 task_id 或 StudyInstanceUID / SeriesInstanceUID 以便定位诊断对象。",
        }
    return patch


def _pull_vs_diagnosis(message: str) -> Optional[str]:
    """有定位标识时，靠语义区分 first_pull（尚未拉取）vs diagnosis（已存在、疑似没拉全）。

    诊断意图词优先（「没拉全」含「拉」，必须先于首拉词判定），命中返回 diagnosis；
    否则命中首拉词返回 first_pull；都不命中返回 None（由调用方默认 diagnosis，向后兼容）。
    """
    low = message.lower()
    if any(h in message or h in low for h in _DIAGNOSIS_HINTS):
        return "diagnosis"
    if any(h in message or h in low for h in _FIRST_PULL_HINTS):
        return "first_pull"
    return None


def _classify_request(state: AgentState) -> Dict:
    """分流 first_pull / diagnosis / knowledge_qa / clarification。

    三级纪律：显式意图字段 > 规则 > LLM 兜底（plan-autonomous-v2 §1.2）。
    """
    message = state.get("message", "") or ""
    intent = (state.get("intent") or "").strip().lower()
    explicit = state.get("task_id") or state.get("study_instance_uid") or state.get("series_instance_uid")

    # 1) 显式意图字段最高优先。first_pull 要求有拉取目标（study/series）。
    if intent == "first_pull":
        if state.get("study_instance_uid") or state.get("series_instance_uid"):
            return {"route": "first_pull", "parse_source": "explicit"}
        return {"route": "clarification", "parse_source": "explicit",
                "clarification": "首次拉取需要 StudyInstanceUID / SeriesInstanceUID。"}
    if intent in _VALID_ROUTES:
        return {"route": intent, "parse_source": "explicit"}

    # 2) 显式结构化标识 + 语义判 first_pull / diagnosis（默认 diagnosis，向后兼容）。
    if explicit:
        sem = _pull_vs_diagnosis(message)
        route = sem or "diagnosis"
        return {"route": route, "parse_source": "explicit"}

    # 规则解析：文本里能抽到 task_id(UUID) 或 UID。
    task_id = _extract(_UUID_RE, message)
    study_uid = None
    uid = _extract(_UID_RE, message)
    if uid:
        study_uid = uid  # 无法仅凭正则区分 study/series，交给 resolve_target/首拉节点结合 DB 判定
    if task_id or uid:
        # task_id 意味着任务已存在 → 必然是诊断；仅 study/series UID 时靠语义判首拉。
        route = "diagnosis" if task_id else (_pull_vs_diagnosis(message) or "diagnosis")
        patch = {"route": route, "parse_source": "rule"}
        if task_id:
            patch["task_id"] = task_id
        if study_uid:
            patch["study_instance_uid"] = study_uid
        return patch

    # 知识问答意图（规则命中）。
    low = message.lower()
    if any(h in message or h in low for h in _QA_HINTS):
        return {"route": "knowledge_qa", "parse_source": "rule"}

    # 3) 规则全部落空且有文本 → LLM 意图识别 + 实体抽取（模糊自然语言兜底）。
    if message.strip():
        llm_patch = parse_with_llm(message, state.get("conversation_context", ""))
        if llm_patch is not None:
            return llm_patch

    # LLM 不可用/调用失败/无文本 → 信息不足，请求澄清（Fallback，不阻断）。
    return {
        "route": "clarification",
        "parse_source": "rule",
        "clarification": "请提供 task_id 或 StudyInstanceUID / SeriesInstanceUID 以便定位诊断对象。",
    }


def route_request(state: AgentState) -> Dict:
    """分类请求并显式决定是否进入 RAG。

    只有 knowledge_qa 意图启用 RAG；诊断、首次拉取和澄清请求不会把每条用户消息
    自动送入知识库。search_knowledge 仍可作为统一工具暴露给外部调用者。
    """
    patch = _classify_request(state)
    patch["use_rag"] = patch.get("route") == "knowledge_qa"
    return patch


def resolve_target(state: AgentState) -> Dict:
    """解析诊断层级并对齐权威 UID（spec 6.3 层级优先规则）。"""
    errors = list(state.get("errors", []))
    task_id = state.get("task_id")
    study_uid = state.get("study_instance_uid")
    series_uid = state.get("series_instance_uid")

    with session_scope() as db:
        # 规则：提供 task_id → 以任务记录为准（level/study/series）。
        if task_id:
            task = db.query(DownloadTask).filter(DownloadTask.task_id == task_id).first()
            if not task:
                return {"route": "clarification",
                        "clarification": "未找到 task_id=%s 对应的任务。" % task_id,
                        "errors": errors}
            if (study_uid and study_uid != task.study_instance_uid) or (
                series_uid and series_uid != task.series_instance_uid
            ):
                errors.append("用户提供的 Study/Series UID 与 task 记录不一致，以 task 为准")
            level = "series" if task.level == DataLevel.SERIES.value else "study"
            return {
                "diagnostic_level": level,
                "study_instance_uid": task.study_instance_uid,
                "series_instance_uid": task.series_instance_uid if level == "series" else None,
                "errors": errors,
            }

        # 无 task_id：先判断文本抽到的 UID 究竟是 study 还是 series。
        if study_uid and not series_uid:
            # 若该 UID 实际是某 Series，则补齐所属 Study，按 series 级诊断。
            series = db.query(SeriesModel).filter(SeriesModel.series_instance_uid == study_uid).first()
            if series:
                return {
                    "diagnostic_level": "series",
                    "study_instance_uid": series.study_instance_uid,
                    "series_instance_uid": series.series_instance_uid,
                    "errors": errors,
                }

        if series_uid:
            # 仅提供 Series：解析所属 Study。
            resolved_study = study_uid
            series = db.query(SeriesModel).filter(SeriesModel.series_instance_uid == series_uid).first()
            if series:
                if study_uid and study_uid != series.study_instance_uid:
                    # 同时提供 Study 与 Series 且归属不一致 → 参数冲突，停止诊断。
                    return {"route": "clarification",
                            "clarification": "Study 与 Series 归属不一致，请确认参数。",
                            "errors": errors}
                resolved_study = series.study_instance_uid
            return {
                "diagnostic_level": "series",
                "study_instance_uid": resolved_study,
                "series_instance_uid": series_uid,
                "errors": errors,
            }

    if study_uid:
        return {"diagnostic_level": "study", "study_instance_uid": study_uid, "errors": errors}

    return {"route": "clarification",
            "clarification": "缺少可定位的 Study/Series 标识。",
            "errors": errors}


def present_clarification(state: AgentState) -> Dict:
    """澄清出口：把澄清语句落到 status。"""
    return {"status": "completed",
            "diagnosis": {"summary": state.get("clarification", "信息不足，无法诊断。"),
                          "route": "clarification"}}


def retrieve_and_answer(state: AgentState) -> Dict:
    """知识问答出口：只消费统一检索结果并生成回答，不实现检索策略。"""
    if state.get("use_rag") is not True:
        raise RuntimeError("RAG is only available for knowledge_qa intent")
    message = state["message"]
    # Rewrite 只取用户可见、可控的最小上下文；State 标识不做 DB/PACS 验证。
    recent_user_messages = [
        item.content for item in state.get("messages", [])
        if isinstance(item, HumanMessage) and isinstance(item.content, str)
    ][-3:]
    result = search_knowledge(
        message,
        context=(state.get("context_summary") or "")[:1000],
        recent_user_messages=recent_user_messages,
        task_id=state.get("task_id"),
        study_instance_uid=state.get("study_instance_uid"),
        series_instance_uid=state.get("series_instance_uid"),
    )
    retrieved = [hit.model_dump() for hit in result.hits]
    sources = []
    for index, hit in enumerate(result.hits, start=1):
        sources.append({
            "id": "S%d" % index,
            "chunk_id": hit.chunk_id,
            "title": hit.title,
            "section": hit.section,
            "page": hit.page,
            "url": hit.url,
        })

    if not result.hits:
        summary = "知识库中没有检索到足够依据，暂时无法回答这个问题。"
    else:
        documents = []
        for index, hit in enumerate(result.hits, start=1):
            location = "；".join(filter(None, [
                hit.title,
                hit.section,
                "第%d页" % hit.page if hit.page is not None else None,
            ]))
            documents.append(("document", hit.content, {
                "source_id": "S%d" % index,
                "location": location,
            }))
        from app.agent.llm import get_chat_model, stable_prompt
        from app.agent.prompts import get_system_prompt, xml_block

        retrieved_context = "\n".join(
            xml_block(name, content, attributes=attributes)
            for name, content, attributes in documents
        )
        prompt = xml_block("user_query", message) + "\n\n<retrieved_context>\n" + retrieved_context + "\n</retrieved_context>"
        response = get_chat_model().invoke(stable_prompt(get_system_prompt("rag_answer"), prompt))
        if not isinstance(response.content, str) or not response.content.strip():
            raise ValueError("knowledge answer response must be non-empty text")
        summary = response.content.strip()
    return {
        "status": "completed",
        "diagnosis": {
            "summary": summary,
            "route": "knowledge_qa",
            "retrieved_knowledge": retrieved,
            "sources": sources,
            "rag": result.model_dump(),
        },
        "retrieved_knowledge": retrieved,
    }
