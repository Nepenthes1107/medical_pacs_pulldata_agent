"""知识库索引初始化与按 doc_id/hash 增量更新。"""
import argparse
import json
import logging

from src.infrastructure import rag_lexical as lexical
from src.infrastructure import rag_store as store
from src.infrastructure.rag_documents import build_chunks, document_manifest
from src.infrastructure.rag_sources import MANIFEST_PATH, download_sources

logger = logging.getLogger(__name__)


def init_knowledge(incremental: bool = False, manifest_path: str | None = None) -> dict:
    """使用同一份 chunks 全量重建两套索引。"""
    if incremental:
        return update_knowledge(manifest_path)
    chunks = build_chunks(manifest_path=manifest_path) if manifest_path else build_chunks()
    if not chunks:
        raise RuntimeError("没有可索引的知识 chunks")
    store.reset_collection()
    dense_written = store.add_atoms(chunks)
    bm25_written = lexical.build_index(chunks)
    dense_total = store.count()
    if dense_written != len(chunks) or bm25_written != len(chunks) or dense_total != len(chunks):
        raise RuntimeError("Dense and BM25 indexes were not built from the complete chunk set")
    by_category: dict[str, int] = {}
    for chunk in chunks:
        cat = chunk["metadata"].get("category", "uncategorized")
        by_category[cat] = by_category.get(cat, 0) + 1
    stats = {
        "chunks": len(chunks),
        "dense_written": dense_written,
        "bm25_written": bm25_written,
        "total_in_collection": dense_total,
        "by_category": by_category,
    }
    logger.info("知识库初始化完成: %s", stats)
    return stats


def update_knowledge(manifest_path: str | None = None) -> dict:
    """只解析哈希变化的文档，并对 Chroma 执行 upsert/delete。

    bm25s 没有原地 upsert API，因此变更后仅用 Chroma 中的当前 corpus 重建 BM25；
    未变化文档不会重新解析或重新 Embedding。
    """
    manifest = document_manifest(manifest_path) if manifest_path else document_manifest()
    current = {item["doc_id"]: item for item in manifest}
    existing = store.list_atoms()
    existing_by_doc: dict[str, list[dict]] = {}
    for atom in existing:
        doc_id = (atom.get("metadata") or {}).get("doc_id")
        if not doc_id:
            raise RuntimeError("existing chunk missing doc_id; run full init_knowledge once")
        existing_by_doc.setdefault(str(doc_id), []).append(atom)

    changed = {
        doc_id for doc_id, item in current.items()
        if not existing_by_doc.get(doc_id)
        or any((a.get("metadata") or {}).get("document_hash") != item["hash"]
               for a in existing_by_doc[doc_id])
    }
    removed = set(existing_by_doc) - set(current)
    stale_ids = [
        atom["id"]
        for doc_id in changed | removed
        for atom in existing_by_doc.get(doc_id, [])
    ]
    deleted = store.delete_ids(stale_ids)
    changed_chunks = build_chunks(
        manifest_path=manifest_path or MANIFEST_PATH,
        document_ids=changed,
    ) if changed else []
    upserted = store.add_atoms(changed_chunks) if changed_chunks else 0
    all_chunks = store.list_atoms()
    if not all_chunks:
        raise RuntimeError("没有可索引的知识 chunks")
    bm25_written = lexical.build_index(all_chunks)
    if bm25_written != len(all_chunks) or store.count() != len(all_chunks):
        raise RuntimeError("增量更新后的 Dense/BM25 corpus 不一致")
    by_category: dict[str, int] = {}
    for chunk in all_chunks:
        category = (chunk.get("metadata") or {}).get("category", "uncategorized")
        by_category[category] = by_category.get(category, 0) + 1
    stats = {
        "documents": len(current),
        "changed_documents": len(changed),
        "removed_documents": len(removed),
        "upserted_chunks": upserted,
        "deleted_chunks": deleted,
        "chunks": len(all_chunks),
        "bm25_written": bm25_written,
        "by_category": by_category,
    }
    logger.info("知识库增量更新完成: %s", stats)
    return stats


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="先按来源清单下载官方文档")
    parser.add_argument("--force-download", action="store_true", help="强制重新下载官方文档")
    parser.add_argument("--incremental", action="store_true", help="按 doc_id/hash 增量更新索引")
    args = parser.parse_args()
    if args.download or args.force_download:
        download_sources(force=args.force_download)
    stats = init_knowledge(incremental=args.incremental)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

