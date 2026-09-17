"""本地 Chroma + BM25 集成测试（spec §9：RAG store）。

conftest 把 persist/lexical 目录指向 tmp_path，并用确定性 FakeEmbedding 替换
dashscope embedding 段——Chroma/BM25 均真实落盘，但全程离线、无 API key。
"""
import pytest

from src.infrastructure import rag_lexical, rag_store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.rag,
]


# ---------------------------------------------------------------- Chroma store

def test_chroma_add_query_where_delete(rag_dirs):
    """真实 PersistentClient：upsert → 语义 query → where 过滤 → delete 全链路。"""
    rag_store.reset_collection()
    assert rag_store.count() == 0

    atoms = [
        {"id": "it-a1", "content": "胸部CT平扫 显示右肺上叶小结节 建议短期随访复查",
         "metadata": {"category": "diagnosis", "study_uid": "9.9.9.s1"}},
        {"id": "it-a2", "content": "腰椎MRI 提示 L4/L5 椎间盘突出 伴椎管狭窄",
         "metadata": {"category": "diagnosis", "study_uid": "9.9.9.s2"}},
        {"id": "it-a3", "content": "CT 增强显示肝右叶占位 考虑血管瘤可能",
         "metadata": {"category": "report", "study_uid": "9.9.9.s3"}},
    ]
    assert rag_store.add_atoms(atoms) == 3
    assert rag_store.count() == 3

    # query 召回含 "结节" 的最相关文档
    hits = rag_store.query("肺部小结节 随访", k=2)
    assert hits, "期望非空召回"
    assert hits[0]["id"] == "it-a1"
    assert "content" in hits[0] and "distance" in hits[0]

    # where 过滤只留 diagnosis 类
    filtered = rag_store.query("占位", k=3, where={"category": "diagnosis"})
    assert {item["id"] for item in filtered} <= {"it-a1", "it-a2"}

    # delete 后 count 递减
    assert rag_store.delete_ids(["it-a2"]) == 1
    assert rag_store.count() == 2
    listed = rag_store.list_atoms()
    assert {item["id"] for item in listed} == {"it-a1", "it-a3"}


# ---------------------------------------------------------------- BM25 lexical

def test_bm25_build_load_query(rag_dirs):
    """真实 bm25s：build_index（rmtree 重建）→ load_index → 中文 query 召回。"""
    chunks = [
        {"id": "it-b1", "content": "胸部 CT 平扫，右肺上叶见小结节影，建议 6 个月后复查",
         "metadata": {"category": "diagnosis"}},
        {"id": "it-b2", "content": "冠状动脉 CTA 显示前降支中段钙化斑块，管腔轻度狭窄",
         "metadata": {"category": "report"}},
        {"id": "it-b3", "content": "胸部影像未见明确异常，双肺纹理清晰",
         "metadata": {"category": "report"}},
    ]
    assert rag_lexical.build_index(chunks) == 3

    hits = rag_lexical.query("肺结节 复查", k=2)
    assert hits, "期望非空 BM25 召回"
    assert hits[0]["id"] == "it-b1"
    assert "bm25_score" in hits[0] and hits[0]["bm25_score"] > 0

    # category 过滤生效
    cat_hits = rag_lexical.query("胸部", k=3, category="report")
    assert all((item.get("metadata") or {}).get("category") == "report" for item in cat_hits)
