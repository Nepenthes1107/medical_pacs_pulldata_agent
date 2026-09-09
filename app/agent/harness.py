"""Harness 护栏层——校验器，不是决策器（plan §5.2）。

核心是护栏 2：引用接地强制校验（抗幻觉主力）。这里的校验器是**确定性代码**，
逐条核对 diagnose 输出的 evidence_refs 是否真的落在工具输出上——不是又一个 LLM，
也绝不在这里写「if fault_stage==X」这类根因判断规则（那是被删掉的规则引擎）。
"""
import logging
from typing import Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)


def _resolve_field(output: Dict, field: str):
    """按点/方括号路径从工具输出里取值，支持 series[C].instance_count、series.C 两种写法。

    路径解析失败时回退为「叶子字段名深度唯一匹配」：兼容 LLM 对嵌套字段（如
    tasks[0].expected_count）引用扁平名（expected_count）的情况；仅当该字段名在全输出中
    恰好出现一次才采用，避免歧义。取不到返回 (None, False)。序列按 series_instance_uid 匹配下标或键名。
    """
    cur = output
    # 归一化 a[b].c → a.b.c
    normalized = field.replace("[", ".").replace("]", "")
    parts = [p for p in normalized.split(".") if p != ""]
    for part in parts:
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            return _deep_unique_match(output, parts[-1])
        if isinstance(cur, list):
            # 数字下标
            if part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
                continue
            # 按 series_instance_uid 匹配
            match = next((it for it in cur
                          if isinstance(it, dict) and it.get("series_instance_uid") == part), None)
            if match is not None:
                cur = match
                continue
            return _deep_unique_match(output, parts[-1])
        return _deep_unique_match(output, parts[-1])
    return cur, True


def _deep_unique_match(output: Dict, leaf_name: str):
    """深度搜索字段名 == leaf_name 的叶子值；恰好一个才返回，否则 (None, False)。

    只收集标量叶子（dict/list 不视为叶子），避免把嵌套结构误当事实值。
    """
    matches = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == leaf_name and not isinstance(v, (dict, list)):
                    matches.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(output)
    if len(matches) == 1:
        return matches[0], True
    # 多个匹配但值全部一致（如 series[*].instance_count 都是 20）→ 仍可唯一确定，无歧义。
    if matches and all(m == matches[0] for m in matches):
        return matches[0], True
    return None, False


def _values_match(expected, actual) -> bool:
    """值匹配：引用必须显式给值；None 也只能匹配真实的 None。"""
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)
    if isinstance(expected, (list, dict)) or isinstance(actual, (list, dict)):
        return expected == actual
    return str(expected).strip() == str(actual).strip()


def resolve_evidence_refs(
    refs: Iterable[Dict],
    recent_evidence: List[Dict],
    thread_id: str,
) -> Dict[str, Dict]:
    """统一解析引用：优先近期 State，缺失部分按当前 thread_id 从 MySQL 回查。"""
    refs = list(refs)
    evidence_by_id = {
        item["tool_id"]: item
        for item in recent_evidence
        if item.get("tool_id") and item.get("thread_id") == thread_id
    }
    missing_ids = list({
        ref.get("tool_id") for ref in refs
        if ref.get("tool_id") and ref.get("tool_id") not in evidence_by_id
    })
    if missing_ids:
        from app.agent.audit import load_tool_evidence

        evidence_by_id.update(load_tool_evidence(missing_ids, thread_id))
    return evidence_by_id


def _citation_violations(claims: List[Dict], evidence_by_id: Dict[str, Dict]) -> List[str]:
    """在已解析的证据集上执行纯确定性校验。"""
    violations: List[str] = []
    for idx, claim in enumerate(claims):
        claim_refs = claim.get("evidence_refs", []) or []
        if not claim_refs:
            violations.append("claim[%d] 无 evidence_refs：'%s'" % (idx, claim.get("claim", "")[:40]))
            continue
        for ref in claim_refs:
            tool_id = ref.get("tool_id")
            field = ref.get("field", "")
            value = ref.get("value")
            if not tool_id:
                violations.append("claim[%d] evidence_ref 缺少 tool_id" % idx)
                continue
            evidence = evidence_by_id.get(tool_id)
            if evidence is None:
                violations.append("claim[%d] 引用不存在或不属于当前会话的 tool_id=%s"
                                  % (idx, tool_id))
                continue
            tool = evidence.get("tool", "unknown")
            if not evidence.get("success"):
                violations.append("claim[%d] 引用了失败调用 %s(tool_id=%s)"
                                  % (idx, tool, tool_id))
                continue
            output = evidence.get("output", {})
            actual, found = _resolve_field(output, field)
            if not found:
                violations.append("claim[%d] 字段 %s.%s 不存在于 tool_id=%s 的输出"
                                  % (idx, tool, field, tool_id))
                continue
            if not _values_match(value, actual):
                violations.append("claim[%d] %s.%s(tool_id=%s) 值不符：引用 %r 实际 %r"
                                  % (idx, tool, field, tool_id, value, actual))
    return violations


def verify_citations(
    diagnosis: Dict,
    recent_evidence: List[Dict],
    thread_id: str,
) -> Tuple[bool, List[str]]:
    """只校验引用真实性，不判断证据是否充分或诊断推理是否成立。

    返回 (all_grounded, violations)。violations 为人类可读的失败原因，供 diagnose 修正引用。
    无 claims / 无关键证据属于推理充分性问题，由 reflect 负责。
    """
    claims = diagnosis.get("claims", []) or []
    refs = [ref for claim in claims for ref in (claim.get("evidence_refs", []) or [])]
    evidence_by_id = resolve_evidence_refs(refs, recent_evidence, thread_id)
    violations = _citation_violations(claims, evidence_by_id)

    return (len(violations) == 0), violations


def filter_grounded_claims(
    diagnosis: Dict,
    recent_evidence: List[Dict],
    thread_id: str,
) -> List[Dict]:
    """只保留全部引用均真实的 claims，供 Verify 最终弃权时清理造假内容。"""
    claims = diagnosis.get("claims", []) or []
    refs = [ref for claim in claims for ref in (claim.get("evidence_refs", []) or [])]
    evidence_by_id = resolve_evidence_refs(refs, recent_evidence, thread_id)
    return [claim for claim in claims if not _citation_violations([claim], evidence_by_id)]


def build_citation_facts(
    diagnosis: Dict,
    recent_evidence: List[Dict],
    thread_id: str,
) -> List[Dict]:
    """为 reflect 构造全部已引用事实，包含已离开 State 窗口的 MySQL 证据。"""
    claims = diagnosis.get("claims", []) or []
    refs = [ref for claim in claims for ref in (claim.get("evidence_refs", []) or [])]
    evidence_by_id = resolve_evidence_refs(refs, recent_evidence, thread_id)
    facts: List[Dict] = []
    for claim in claims:
        for ref in claim.get("evidence_refs", []) or []:
            evidence = evidence_by_id.get(ref.get("tool_id"))
            if not evidence:
                continue
            actual, found = _resolve_field(evidence.get("output", {}), ref.get("field", ""))
            if not found:
                continue
            facts.append({
                "claim": claim.get("claim", ""),
                "tool_id": evidence["tool_id"],
                "tool": evidence.get("tool"),
                "args": evidence.get("args", {}),
                "field": ref.get("field", ""),
                "value": actual,
            })
    return facts
