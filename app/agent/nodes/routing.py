"""路由与目标解析节点（spec 6.3 / 8）。

route_request：显式标识/规则优先，歧义才调用 LLM（parse_with_llm）。
resolve_target：解析诊断层级，Series 补齐所属 Study，处理层级优先规则与归属冲突。
"""
import logging
import re
from typing import Dict, Optional

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


def parse_with_llm(message: str) -> Optional[Dict]:
    """规则抽不出标识时的 LLM 兜底：意图识别 + 实体抽取，产出结构化路由字段。

    返回 None 表示 LLM 不可用/调用失败，交由 route_request 落到规则澄清出口
    （与 plan_tools / retrieve_and_explain 同款 Fallback 纪律，绝不阻断图）。
    抽取的标识必须能在原文中找到（子串校验），否则视为幻觉丢弃。
    """
    from app.agent.llm import get_structured_model, llm_available

    if not llm_available():
        return None

    try:
        from app.agent.prompts import ROUTING_SYSTEM_PROMPT
        from app.agent.tool_schemas import RouteDecision

        model = get_structured_model(RouteDecision)
        decision = model.invoke(ROUTING_SYSTEM_PROMPT + "\n\n## 用户消息\n" + message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_with_llm LLM 调用失败，退回规则澄清: %s", exc)
        return None

    route = decision.route if decision.route in _VALID_ROUTES else "clarification"

    if route != "diagnosis":
        patch = {"route": route, "parse_source": "llm"}
        if route == "clarification":
            patch["clarification"] = (
                decision.clarification
                or "请提供 task_id 或 StudyInstanceUID / SeriesInstanceUID 以便定位诊断对象。"
            )
        return patch

    # diagnosis：逐个标识做原文子串校验，过滤模型编造的 UID / task_id。
    patch: Dict = {"route": "diagnosis", "parse_source": "llm"}
    for field in ("task_id", "study_instance_uid", "series_instance_uid"):
        value = getattr(decision, field, None)
        if value and value in message:
            patch[field] = value

    # 判为诊断却没抽到任何可定位标识 → 转澄清，避免下游空跑 DB。
    if not any(k in patch for k in ("task_id", "study_instance_uid", "series_instance_uid")):
        return {
            "route": "clarification",
            "parse_source": "llm",
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


def route_request(state: AgentState) -> Dict:
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
        llm_patch = parse_with_llm(message)
        if llm_patch is not None:
            return llm_patch

    # LLM 不可用/调用失败/无文本 → 信息不足，请求澄清（Fallback，不阻断）。
    return {
        "route": "clarification",
        "parse_source": "rule",
        "clarification": "请提供 task_id 或 StudyInstanceUID / SeriesInstanceUID 以便定位诊断对象。",
    }


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
    """知识问答出口：Agentic RAG 检索知识库回答（spec §8）。检索不可用时明确提示。"""
    message = state.get("message", "")
    retrieved = []
    try:
        from app.agent.rag import retriever
        from app.agent.rag.embeddings import embeddings_available

        if embeddings_available():
            retrieved = retriever.retrieve_texts(message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge_qa 检索失败: %s", exc)

    if retrieved:
        summary = "根据知识库检索到以下相关内容：\n" + "\n---\n".join(retrieved)
    else:
        summary = "知识库暂不可用或未检索到相关内容（需配置 embedding key 并初始化知识库）。"
    return {
        "status": "completed",
        "diagnosis": {"summary": summary, "route": "knowledge_qa", "retrieved_knowledge": retrieved},
        "retrieved_knowledge": retrieved,
    }
