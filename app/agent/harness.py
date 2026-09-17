"""Compatibility module alias for the canonical evidence harness."""
import sys

from src.agents.pacs import harness as _canonical

sys.modules[__name__] = _canonical
