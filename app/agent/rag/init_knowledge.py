"""知识库初始化脚本（spec 11.3/11.4）。

从 app/agent/rag/knowledge/*.json 读取知识原子，长文档用 RecursiveCharacterTextSplitter
切分，短 SOP 作独立原子，写入 ChromaDB。

用法：python -m app.agent.rag.init_knowledge [--reset]
"""
import argparse
import json
import logging
import os
from typing import Dict, List

from app.agent.rag import store

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
_LONG_DOC_THRESHOLD = 800  # 超过该字数的文档才切分（spec 11.3：短 SOP 作独立原子）


def load_knowledge_files(knowledge_dir: str = KNOWLEDGE_DIR) -> List[Dict]:
    """加载 knowledge/ 下所有 JSON（每个文件是一个原子数组）。"""
    atoms: List[Dict] = []
    if not os.path.isdir(knowledge_dir):
        logger.warning("知识目录不存在: %s", knowledge_dir)
        return atoms
    for name in sorted(os.listdir(knowledge_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(knowledge_dir, name), "r", encoding="utf-8") as fp:
            data = json.load(fp)
        atoms.extend(data if isinstance(data, list) else [data])
    return atoms


def _split_long_atoms(atoms: List[Dict]) -> List[Dict]:
    """仅对超长文档切分；短原子原样保留。"""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n## ", "\n### ", "\n#### ", "\n", "。", ".", " "],
    )
    out: List[Dict] = []
    for atom in atoms:
        content = atom["content"]
        if len(content) <= _LONG_DOC_THRESHOLD:
            out.append(atom)
            continue
        for i, chunk in enumerate(splitter.split_text(content)):
            out.append({
                "id": "%s-chunk%d" % (atom["id"], i),
                "content": chunk,
                "metadata": dict(atom.get("metadata", {})),
            })
    return out


def init_knowledge(reset: bool = True) -> Dict:
    """初始化知识库，返回统计信息。"""
    atoms = load_knowledge_files()
    atoms = _split_long_atoms(atoms)
    if reset:
        store.reset_collection()
    written = store.add_atoms(atoms)
    by_category: Dict[str, int] = {}
    for a in atoms:
        cat = a.get("metadata", {}).get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
    stats = {"written": written, "total_in_collection": store.count(), "by_category": by_category}
    logger.info("知识库初始化完成: %s", stats)
    return stats


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="重建 collection 后再写入")
    args = parser.parse_args()
    stats = init_knowledge(reset=args.reset or True)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
