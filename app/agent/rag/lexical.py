"""与 Chroma 共用 chunk corpus 的本地 BM25 索引。"""
import os
import re
import shutil
import unicodedata
from functools import lru_cache
from typing import Dict, List, Optional

import bm25s
import jieba

from app.core.config import resolve_project_path, settings

_TOKEN_PATTERN = re.compile(
    r"\([0-9A-Fa-f]{4},[0-9A-Fa-f]{4}\)|0x[0-9A-Fa-f]+|"
    r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*|[\u3400-\u9fff]+"
)


def tokenize_text(text: str) -> List[str]:
    """中英混合分词，同时把 DICOM 精确标识折叠为不会再被拆开的 token。"""
    text = unicodedata.normalize("NFKC", text)
    tokens = []
    for match in _TOKEN_PATTERN.finditer(text):
        value = match.group(0)
        if re.fullmatch(r"[\u3400-\u9fff]+", value):
            tokens.extend(part.strip() for part in jieba.lcut(value) if part.strip())
        elif value.startswith("("):
            tokens.append("tag_" + value[1:-1].replace(",", "_").lower())
        else:
            tokens.append(re.sub(r"[.\-]", "_", value.lower()))
    return tokens


def _lexical_text(text: str) -> str:
    return " ".join(tokenize_text(text))


def build_index(chunks: List[Dict]) -> int:
    index_dir = resolve_project_path(settings.rag.lexical_index_dir)
    if os.path.isdir(index_dir):
        shutil.rmtree(index_dir)
    os.makedirs(index_dir, exist_ok=True)
    corpus_tokens = bm25s.tokenize([_lexical_text(c["content"]) for c in chunks])
    retriever = bm25s.BM25(corpus=chunks)
    retriever.index(corpus_tokens)
    retriever.save(index_dir, corpus=chunks)
    load_index.cache_clear()
    return len(chunks)


@lru_cache()
def load_index():
    index_dir = resolve_project_path(settings.rag.lexical_index_dir)
    if not os.path.isdir(index_dir):
        raise FileNotFoundError("BM25 index not initialized: %s" % index_dir)
    return bm25s.BM25.load(index_dir, load_corpus=True)


def _query(text: str, k: int, category: Optional[str] = None) -> List[Dict]:
    retriever = load_index()
    corpus_size = len(retriever.corpus)
    if corpus_size == 0:
        return []
    query_tokens = bm25s.tokenize([_lexical_text(text)])
    # 为保证 metadata 过滤准确，先对当前小型本地语料取完整排名，再过滤。
    documents, scores = retriever.retrieve(query_tokens, k=corpus_size)
    items = []
    for document, score in zip(documents[0], scores[0]):
        item = document
        if category and (item.get("metadata") or {}).get("category") != category:
            continue
        items.append({**item, "bm25_score": float(score)})
        if len(items) >= k:
            break
    return items
