"""Legacy module alias for the canonical database composition root."""

import sys

from src.infrastructure.db import database as _canonical

sys.modules[__name__] = _canonical
