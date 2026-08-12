"""Embedding 装配（spec 11.1）：DashScope text-embedding-v4，OpenAI 兼容。

check_embedding_ctx_length=False：DashScope 兼容端点不支持 tiktoken 长度预校验。
key 缺失时不阻断导入，仅在实际编码时报错（与 chat model 一致）。
"""
import logging
from functools import lru_cache

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache()
def get_embeddings():
    from langchain_openai import OpenAIEmbeddings

    cfg = settings.rag
    if not settings.llm.api_key:
        raise RuntimeError("Embedding api_key 未配置：请设置环境变量 DASHSCOPE_API_KEY")
    return OpenAIEmbeddings(
        model=cfg.embedding_model,
        base_url=cfg.embedding_base_url,
        api_key=settings.llm.api_key,
        check_embedding_ctx_length=False,
    )


def embeddings_available() -> bool:
    return bool(settings.llm.api_key)
