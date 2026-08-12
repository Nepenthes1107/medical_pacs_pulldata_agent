"""经验记忆写入（需求 5：按故障签名去重/更新）。

每当一次诊断经人工审批确认，就把"现象—证据—根因"结构化为一条经验记忆写入知识库。
去重口径（plan-autonomous-v2 §5）：
- 故障签名 = LLM 归一化根因产出的规范 slug（同类故障命中同一签名 → 更新计数而非新增）；
- atom_id = "exp-<signature>"，ChromaDB 自身即去重载体，不引入独立签名表；
- LLM 不可用/归一化失败 → 跳过经验写入，不伪造粗签名污染检索库（降级不伪装：不可用就不记）。
"""
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

from app.agent.rag import store

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slugify(text: str) -> str:
    """清洗为签名 slug：小写、非字母数字转下划线、截断。"""
    s = (text or "").strip().lower().replace(" ", "_").replace("-", "_")
    s = _SLUG_RE.sub("_", s).strip("_")
    return s[:64] or "unknown"


def normalize_signature(diagnosis: Dict) -> Optional[str]:
    """把自由文本根因归一化为规范签名 slug。完全依赖 LLM 语义。

    LLM 可用且产出有效 slug → 返回 signature；
    LLM 不可用/调用失败/产出空 → 返回 None（无法归一化，调用侧据此跳过写入，不伪造粗签名）。
    """
    from app.agent.llm import llm_available

    if not llm_available():
        return None
    try:
        from app.agent.llm import get_structured_model
        from app.agent.prompts import SIGNATURE_SYSTEM_PROMPT
        from app.agent.tool_schemas import FaultSignature

        model = get_structured_model(FaultSignature)
        prompt = (
            SIGNATURE_SYSTEM_PROMPT
            + "\n\n## 根因\n" + (diagnosis.get("root_cause", "") or "")
            + "\n\n## 现象\n" + (diagnosis.get("summary", "") or "")
            + "\n\n请输出 FaultSignature。"
        )
        sig = _slugify(model.invoke(prompt).signature)
        if not sig or sig == "unknown":
            return None
        return sig
    except Exception as exc:  # noqa: BLE001
        logger.warning("签名归一化失败，跳过经验写入（不伪造粗签名）: %s", exc)
        return None


def build_experience_content(diagnosis: Dict) -> str:
    """把 GroundedDiagnosis 结构化为经验记忆正文（现象—证据—根因—补拉方案）。

    repull_plan 存在时追加第四行：只记根因不够，还要记"当时用什么方案解决的"，
    否则检索到经验也不知道该复用哪种补拉策略。
    """
    summary = diagnosis.get("summary", "")
    claims = diagnosis.get("claims", []) or []
    claim_texts = [c.get("claim", "") for c in claims if c.get("claim")]
    root_cause = diagnosis.get("root_cause", "")
    lines = [
        "现象：%s" % summary,
        "证据：%s" % "；".join(claim_texts[:5]),
        "根因：%s" % root_cause,
    ]
    plan = diagnosis.get("repull_plan")
    if plan:
        lines.append(
            "补拉方案：strategy=%s, level=%s, missing_instance_count=%s, target_count=%s, outcome=%s"
            % (plan.get("strategy"), plan.get("level"), plan.get("missing_instance_count"),
               plan.get("target_count"), plan.get("outcome"))
        )
    return "\n".join(lines)


def write_experience(run_id: str, diagnosis: Dict, now: Optional[datetime] = None) -> Optional[str]:
    """写入/合并一条经验记忆，返回其 atom_id；无法归一化签名则跳过并返回 None。

    命中同一签名 → hit_count+1、更新 last_seen_at/last_run_id（正文保留首次现象，追加最近一次）；
    不存在 → 新建 hit_count=1。时间戳由调用侧传入（写经验发生在 Checker 验收通过时点）。
    LLM 不可用/归一化失败 → 不写（不伪造粗签名污染检索库，需求 B）。
    """
    now = now or datetime.utcnow()
    ts = now.isoformat()
    signature = normalize_signature(diagnosis)
    if not signature:
        logger.warning("LLM 不可用或签名归一化失败，跳过经验写入 run_id=%s", run_id)
        return None
    atom_id = "exp-%s" % signature
    level = diagnosis.get("diagnostic_level", "study")
    integrity = diagnosis.get("integrity_result", "unknown")
    content = build_experience_content(diagnosis)

    collection = store.get_collection()
    existing = collection.get(ids=[atom_id])
    existing_ids = existing.get("ids", []) if existing else []

    if existing_ids:
        # 命中 → 更新计数与最近一次。
        prev_meta = (existing.get("metadatas") or [{}])[0] or {}
        prev_content = (existing.get("documents") or [""])[0] or ""
        hit_count = int(prev_meta.get("hit_count", 1)) + 1
        # 正文保留首次现象，追加最近一次现象（避免无限膨胀，只留最近一条追加）。
        merged_content = prev_content.split("\n\n[最近一次]")[0] + (
            "\n\n[最近一次] %s" % diagnosis.get("summary", "")[:120])
        metadata = dict(prev_meta)
        metadata.update({
            "hit_count": hit_count, "last_seen_at": ts, "last_run_id": run_id,
            "signature_source": "llm",
        })
        store.add_atoms([{"id": atom_id, "content": merged_content, "metadata": metadata}])
        logger.info("经验记忆已合并: %s (signature=%s, hit_count=%d)", atom_id, signature, hit_count)
        return atom_id

    metadata = {
        "category": "experience",
        "stage": integrity,
        "dicom_operation": "none",
        "symptom": diagnosis.get("summary", "")[:80],
        "source": "experience",
        "signature": signature,
        "signature_source": "llm",
        "hit_count": 1,
        "first_run_id": run_id,
        "last_run_id": run_id,
        "last_seen_at": ts,
        "diagnostic_level": level,
    }
    store.add_atoms([{"id": atom_id, "content": content, "metadata": metadata}])
    logger.info("经验记忆已写入: %s (signature=%s, source=llm)", atom_id, signature)
    return atom_id


def write_verified_repull_experience(run_id: str, proposed_action: Optional[Dict]) -> None:
    """现实验证通过后沉淀经验：Checker 判定 success 且原诊断 confidence=confirmed 才写。

    正文来自 proposed_action 里的原始诊断（root_cause + 补拉计划），不是 Checker 的验收结论——
    经验记的是「被现实验证过的原始根因分析」。由 Checker 在同一次收尾后触发（best-effort，
    异常只记日志，不影响已提交的 Task/Archive/AgentRun 状态）。
    """
    if not proposed_action:
        return
    # 护栏：原诊断置信度必须 confirmed，否则根因未达确信，不沉淀（即使补拉恰好补全）。
    if proposed_action.get("confidence") != "confirmed":
        logger.info("Checker 验收完整但原诊断非 confirmed，不写经验 run_id=%s", run_id)
        return
    try:
        from app.agent.rag.embeddings import embeddings_available

        if not embeddings_available():
            return
        from app.agent.tool_schemas import derive_repull_level

        scope = proposed_action.get("scope") or {}
        # 粒度现算（唯一真相是 missing_targets）——存字段会与范围分叉。
        targets = scope.get("missing_targets") or [] if isinstance(scope, dict) else []
        diagnosis = {
            "summary": proposed_action.get("rationale", "") or proposed_action.get("root_cause", ""),
            "root_cause": proposed_action.get("root_cause", ""),
            "confidence": "confirmed",
            "claims": [{"claim": r} if isinstance(r, str) else r
                       for r in proposed_action.get("evidence_refs", [])],
            "diagnostic_level": derive_repull_level(targets),
            "integrity_result": "complete",  # 现实已验证完整
            "repull_plan": {
                "strategy": proposed_action.get("strategy"),
                "level": derive_repull_level(targets),
                "missing_instance_count": scope.get("missing_instance_count"),
                "target_count": len(targets),
                "outcome": "complete",
            },
        }
        write_experience(run_id, diagnosis, now=datetime.utcnow())
    except Exception as exc:  # noqa: BLE001
        logger.warning("经验记忆写入失败（不影响 Checker 收尾）: %s", exc)


def list_experience() -> List[Dict]:
    """列出所有经验记忆条目（source=experience）。"""
    collection = store.get_collection()
    res = collection.get(where={"source": "experience"})
    ids = res.get("ids", [])
    docs = res.get("documents", [])
    metas = res.get("metadatas", [])
    return [{"id": ids[i], "content": docs[i], "metadata": metas[i]} for i in range(len(ids))]
