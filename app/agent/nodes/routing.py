"""Compatibility facade for PACS routing nodes."""
from src.agents.pacs.nodes import routing as _routing
from src.agents.pacs.nodes.routing import *
from src.infrastructure.rag_pipeline import search_knowledge as _canonical_search_knowledge

search_knowledge = _canonical_search_knowledge


def retrieve_and_answer(state):
    _routing.search_knowledge = search_knowledge
    return _routing.retrieve_and_answer(state)
