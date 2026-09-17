"""Legacy module alias for the canonical local DICOM scanner."""

import sys

from src.infrastructure.pacs import scanner as _canonical

sys.modules[__name__] = _canonical
