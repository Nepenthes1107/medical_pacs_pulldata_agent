"""版本化 Prompt 注册表与安全的 XML 上下文装配。"""
import html
import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.core.settings import BASE_DIR

PROMPT_FILE = Path(BASE_DIR) / "config" / "prompts.yml"
REQUIRED_PROMPTS = {
    "reason", "diagnosis", "routing", "reflect", "plan_repull",
    "query_rewrite", "rag_answer", "context_summary",
}


@lru_cache
def load_prompt_catalog(path: str = str(PROMPT_FILE)) -> dict[str, Any]:
    """加载受版本控制的 Prompt 文件；启动/首次使用时快速失败。"""
    try:
        with open(path, encoding="utf-8") as fp:
            catalog = yaml.safe_load(fp)
    except OSError as exc:
        raise RuntimeError("prompt catalog cannot be read: %s" % path) from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("version"), str) or not catalog["version"].strip():
        raise ValueError("prompt catalog requires a non-empty string version")
    prompts = catalog.get("prompts")
    if not isinstance(prompts, dict):
        raise ValueError("prompt catalog requires a prompts mapping")
    missing = REQUIRED_PROMPTS - set(prompts)
    if missing:
        raise ValueError("prompt catalog missing prompts: %s" % ", ".join(sorted(missing)))
    for name in REQUIRED_PROMPTS:
        item = prompts[name]
        if not isinstance(item, dict) or not isinstance(item.get("system"), str) or not item["system"].strip():
            raise ValueError("prompt %s requires a non-empty system template" % name)
    return catalog


def prompt_version() -> str:
    return load_prompt_catalog()["version"]


def get_system_prompt(name: str) -> str:
    """取得纯静态规则，绝不在这里插入用户、工具或检索数据。"""
    catalog = load_prompt_catalog()
    prompts = catalog["prompts"]
    if name not in prompts:
        raise KeyError("unknown prompt: %s" % name)
    return "<!-- prompt=%s version=%s -->\n%s" % (
        name, catalog["version"], prompts[name]["system"].strip(),
    )


def xml_block(name: str, value: Any, *, attributes: Mapping[str, Any] | None = None) -> str:
    """将不可信动态数据隔离到 XML 块；文本转义避免伪造标签。"""
    if not name.replace("_", "").isalnum() or not name:
        raise ValueError("invalid XML block name: %s" % name)
    attrs = "".join(
        ' %s="%s"' % (key, html.escape(str(raw), quote=True))
        for key, raw in (attributes or {}).items()
    )
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "<%s%s>\n%s\n</%s>" % (name, attrs, html.escape(payload, quote=False), name)


def xml_blocks(items: Iterable[tuple[str, Any]]) -> str:
    return "\n\n".join(xml_block(name, value) for name, value in items)


def diagnosis_few_shot() -> str:
    """固定的一条通用示例，只教引用与弃权格式，不提供业务事实。"""
    return xml_block("diagnosis_few_shot", {
        "input": "工具证据不足以确认单一根因",
        "expected_output_shape": {
            "confidence": "uncertain",
            "claims": [],
            "missing_evidence": ["需要一次成功的任务状态或完整性查询"],
            "rule": "仅引用实际 ToolMessage/evidence 中存在的 tool_id、field、value。",
        },
    })


