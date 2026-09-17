"""Compatibility module alias for the canonical LLM factory."""
import sys

from src.core import llm as _canonical

sys.modules[__name__] = _canonical
