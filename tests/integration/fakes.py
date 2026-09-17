"""集成测试共享的离网替身。

真实外部服务（Redis/MySQL/RabbitMQ/Orthanc/Chroma/BM25）保持不变；只有
依赖外网或密钥的段（dashscope embedding）用确定性实现替换，保证无 API key 也可跑。
"""
import hashlib
import re

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[㐀-鿿]")


class FakeEmbedding(EmbeddingFunction):
    """确定性、离线、固定维度的本地 embedding。

    用法：token 命中散列到固定维度累加，语义可预测——query 含某 token 时，
    含同一 token 的文档召回更高，便于断言 Chroma 真实召回/过滤行为。
    """

    def __init__(self, dimension: int = 32):
        self.dimension = dimension

    def _embed_one(self, text: str):
        vector = [0.0] * self.dimension
        for token in _TOKEN_RE.findall(str(text).lower()):
            bucket = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self.dimension
            vector[bucket] += 1.0
        return vector

    def __call__(self, input: Documents) -> Embeddings:
        return [self._embed_one(doc) for doc in input]

    def embed_query(self, input):  # noqa: A002
        texts = [input] if isinstance(input, str) else list(input)
        return [self._embed_one(text) for text in texts]

    @staticmethod
    def name() -> str:
        return "fake-embedding-offline"
