"""Canonical worker entry points.

Worker modules own queue/process boundaries and delegate domain work to
``src.application`` and ``src.infrastructure``.  The legacy ``app.workers``
paths remain import-compatible during the migration window.
"""

