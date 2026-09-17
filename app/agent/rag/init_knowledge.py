"""Compatibility exports for infrastructure-owned knowledge indexing."""
from src.infrastructure import rag_index as _canonical
from src.infrastructure.rag_index import *

store = _canonical.store
lexical = _canonical.lexical
build_chunks = _canonical.build_chunks
document_manifest = _canonical.document_manifest


def _sync():
    _canonical.build_chunks = build_chunks
    _canonical.document_manifest = document_manifest
    _canonical.store = store
    _canonical.lexical = lexical


def init_knowledge(*args, **kwargs):
    _sync()
    return _canonical.init_knowledge(*args, **kwargs)


def update_knowledge(*args, **kwargs):
    _sync()
    return _canonical.update_knowledge(*args, **kwargs)

main = _canonical.main
