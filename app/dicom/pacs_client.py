"""Legacy module alias for the canonical PACS client adapter."""

import sys

from src.infrastructure.pacs import client as _canonical

sys.modules[__name__] = _canonical
