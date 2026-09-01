"""DashScope text-embedding-v4 的 Chroma embedding 装配。"""
from functools import lru_cache

from langchain_openai import OpenAIEmbeddings

from app.core.config import settings


@lru_cache()
def get_embeddings():
    cfg = settings.rag
    if not settings.llm.api_key:
        raise RuntimeError("Embedding api_key 未配置：请设置环境变量 DASHSCOPE_API_KEY")
    return OpenAIEmbeddings(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=settings.llm.api_key,
        check_embedding_ctx_length=False,
    )
