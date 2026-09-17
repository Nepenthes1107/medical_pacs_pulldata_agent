"""Legacy module alias for the canonical worker implementation."""
import sys

from src.workers import agent as _canonical

sys.modules[__name__] = _canonical
