"""Compatibility module alias for the canonical audit adapter."""
import sys

from src.infrastructure.db import audit as _canonical

sys.modules[__name__] = _canonical
