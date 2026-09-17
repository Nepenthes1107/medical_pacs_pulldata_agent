"""Knowledge-store adapter.

The public search callable remains the single pipeline function so Agent tools,
MCP and tests cannot accidentally diverge. Chroma/BM25 and embedding details
stay below this infrastructure boundary.
"""

from src.infrastructure.rag_pipeline import search_knowledge


class KnowledgeStore:
    def search(self, query: str, *, limit: int | None = None, **kwargs):
        return search_knowledge(query, top_n=limit, **kwargs)


knowledge_store = KnowledgeStore()

__all__ = ["KnowledgeStore", "knowledge_store", "search_knowledge"]
