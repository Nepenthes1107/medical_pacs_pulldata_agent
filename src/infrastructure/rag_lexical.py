"""BM25 lexical index adapter sharing the Chroma chunk corpus."""
import os
import re
import shutil
import unicodedata
from functools import lru_cache

import bm25s
import jieba

from src.core.settings import resolve_project_path, settings

_TOKEN_PATTERN = re.compile(
    r"\([0-9A-Fa-f]{4},[0-9A-Fa-f]{4}\)|0x[0-9A-Fa-f]+|"
    r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*|[\u3400-\u9fff]+"
)


def tokenize_text(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
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


def build_index(chunks: list[dict]) -> int:
    index_dir = resolve_project_path(settings.rag.lexical_index_dir)
    if os.path.isdir(index_dir):
        shutil.rmtree(index_dir)
    os.makedirs(index_dir, exist_ok=True)
    corpus_tokens = bm25s.tokenize([_lexical_text(item["content"]) for item in chunks])
    retriever = bm25s.BM25(corpus=chunks)
    retriever.index(corpus_tokens)
    retriever.save(index_dir, corpus=chunks)
    load_index.cache_clear()
    return len(chunks)


@lru_cache
def load_index():
    index_dir = resolve_project_path(settings.rag.lexical_index_dir)
    if not os.path.isdir(index_dir):
        raise FileNotFoundError("BM25 index not initialized: %s" % index_dir)
    return bm25s.BM25.load(index_dir, load_corpus=True)


def query(text: str, k: int, category: str | None = None) -> list[dict]:
    retriever = load_index()
    corpus_size = len(retriever.corpus)
    if not corpus_size:
        return []
    query_tokens = bm25s.tokenize([_lexical_text(text)])
    documents, scores = retriever.retrieve(query_tokens, k=corpus_size)
    items = []
    for document, score in zip(documents[0], scores[0]):
        if category and (document.get("metadata") or {}).get("category") != category:
            continue
        items.append({**document, "bm25_score": float(score)})
        if len(items) >= k:
            break
    return items


# Compatibility name retained for callers that used the old private helper.
_query = query


# Compatibility name used by the existing pipeline tests and migration scripts.
_query = query
