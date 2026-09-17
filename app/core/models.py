"""Legacy module alias for canonical infrastructure ORM models."""

import sys

from src.infrastructure.db import models as _canonical

sys.modules[__name__] = _canonical
