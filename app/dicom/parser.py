"""Legacy module alias for canonical PACS parsing helpers."""

import sys

from src.infrastructure.pacs import parser as _canonical

sys.modules[__name__] = _canonical
