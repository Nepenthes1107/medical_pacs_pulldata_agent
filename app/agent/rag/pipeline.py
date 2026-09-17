"""Compatibility module alias for the canonical RAG pipeline."""
import sys

from src.infrastructure import rag_pipeline as _canonical

sys.modules[__name__] = _canonical
