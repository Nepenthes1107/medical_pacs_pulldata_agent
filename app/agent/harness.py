"""Harness 护栏层——校验器，不是决策器（plan §5.2）。

核心是护栏 2：引用接地强制校验（抗幻觉主力）。这里的校验器是**确定性代码**，
逐条核对 diagnose 输出的 evidence_refs 是否真的落在工具输出上——不是又一个 LLM，
也绝不在这里写「if fault_stage==X」这类根因判断规则（那是被删掉的规则引擎）。
"""
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _resolve_field(output: Dict, field: str):
    """按点/方括号路径从工具输出里取值，支持 series[C].instance_count、series.C 两种写法。

    取不到返回 (_MISSING, False)。序列按 series_instance_uid 匹配下标或键名。
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
            return None, False
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
            return None, False
        return None, False
    return cur, True


def _values_match(expected, actual) -> bool:
    """值匹配：数值按相等比较，字符串按 strip 比较，None 视为通配（只校验字段存在）。"""
    if expected is None:
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)
    return str(expected).strip() == str(actual).strip()


def verify_citations(diagnosis: Dict, tool_results: Dict) -> Tuple[bool, List[str]]:
    """逐条核对 claims[].evidence_refs：工具调过吗？字段在吗？值对得上吗？

    返回 (all_grounded, violations)。violations 为人类可读的失败原因，供 reflect 修正。
    无 claims 视为未接地（不能凭空下确定结论）——但 uncertain 诊断允许无 claims（见调用方）。
    """
    violations: List[str] = []
    claims = diagnosis.get("claims", []) or []

    for idx, claim in enumerate(claims):
        refs = claim.get("evidence_refs", []) or []
        if not refs:
            violations.append("claim[%d] 无 evidence_refs：'%s'" % (idx, claim.get("claim", "")[:40]))
            continue
        for ref in refs:
            tool = ref.get("tool")
            field = ref.get("field", "")
            value = ref.get("value")
            output = tool_results.get(tool)
            if output is None:
                violations.append("claim[%d] 引用未调用的工具 %s" % (idx, tool))
                continue
            if not output.get("success"):
                violations.append("claim[%d] 引用了失败工具 %s" % (idx, tool))
                continue
            actual, found = _resolve_field(output, field)
            if not found:
                violations.append("claim[%d] 字段 %s.%s 不存在于工具输出" % (idx, tool, field))
                continue
            if not _values_match(value, actual):
                violations.append("claim[%d] %s.%s 值不符：引用 %r 实际 %r"
                                   % (idx, tool, field, value, actual))

    return (len(violations) == 0), violations


def key_evidence_present(tool_results: Dict) -> bool:
    """护栏 6 前置：是否具备下结论的关键证据（至少 PACS 目标查询成功）。"""
    pacs = tool_results.get("query_pacs_target", {})
    return bool(pacs.get("success"))
