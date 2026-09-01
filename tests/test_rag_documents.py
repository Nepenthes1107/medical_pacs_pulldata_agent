import json

import pytest

from app.agent.rag import documents
from app.agent.rag import init_knowledge
from app.agent.rag import lexical
from app.agent.rag import store


def test_pdf_docx_html_and_json_parsers_keep_structure(tmp_path):
    fitz = pytest.importorskip("fitz")
    docx = pytest.importorskip("docx")
    pytest.importorskip("bs4")

    pdf_path = tmp_path / "sample.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "7.1 C-MOVE Status\nStatus 0xA801 means refused")
    page = pdf.new_page()
    page.insert_text((72, 72), "Continued on page two.\n7.2 C-FIND Status\nStatus 0x0000 means success")
    pdf.save(pdf_path)
    pdf.close()

    docx_path = tmp_path / "sample.docx"
    word = docx.Document()
    word.add_heading("Association", level=1)
    word.add_paragraph("An association negotiates presentation contexts.")
    word.add_heading("Presentation Context", level=2)
    word.add_paragraph("Each context contains an abstract syntax.")
    word.save(docx_path)

    html_path = tmp_path / "sample.html"
    html_path.write_text(
        "<html><body><main><h1>DICOM</h1><h2>Query/Retrieve</h2><p>C-FIND locates studies.</p>"
        "<table><tr><td>Status</td><td>0x0000</td></tr></table></main></body></html>",
        encoding="utf-8",
    )
    json_path = tmp_path / "sample.json"
    json_path.write_text(json.dumps([{
        "id": "atom-1",
        "content": "C-STORE transfers an instance.",
        "metadata": {"category": "fault_sop"},
    }]), encoding="utf-8")

    pdf_units = documents._pdf_units(str(pdf_path))
    docx_units = documents._docx_units(str(docx_path))
    html_units = documents._html_units(str(html_path))
    json_units = documents._json_units(str(json_path))

    assert pdf_units[0]["metadata"]["page_start"] == 1
    assert pdf_units[0]["metadata"]["page_end"] == 2
    assert "7.1 C-MOVE" in pdf_units[0]["metadata"]["section"]
    assert pdf_units[1]["metadata"]["page_start"] == 2
    assert "7.2 C-FIND" in pdf_units[1]["metadata"]["section"]
    assert docx_units[0]["metadata"]["section"] == "Association"
    assert docx_units[1]["metadata"]["section"] == "Association > Presentation Context"
    assert html_units[0]["metadata"]["section"] == "DICOM > Query/Retrieve"
    assert "0x0000" in html_units[0]["text"]
    assert json_units[0]["id"] == "atom-1"


def test_chunk_metadata_has_page_section_and_content_hash():
    chunks = documents._split_units(
        [{"text": "C-MOVE response status 0xA801", "metadata": {
            "page_start": 12, "page_end": 12, "section": "C.4.2 C-MOVE",
        }}],
        {"source_id": "ps3.4", "title": "DICOM PS3.4", "version": "2026c"},
    )
    assert len(chunks) == 1
    metadata = chunks[0]["metadata"]
    assert metadata["page_start"] == 12
    assert metadata["section"] == "C.4.2 C-MOVE"
    assert len(metadata["content_hash"]) == 64
    assert {
        "doc_id", "document_hash", "content_hash", "title", "section",
        "page_start", "page_end", "category", "source_url", "version",
        "unit_index", "chunk_index",
    } <= set(metadata)


def test_character_chunks_are_bounded_overlap_and_do_not_cross_sections():
    long_text = "".join(chr(0x4E00 + index % 1000) for index in range(1800))
    chunks = documents._split_units(
        [
            {"text": long_text, "metadata": {"section": "1 First"}},
            {"text": "second-section-only", "metadata": {"section": "2 Second"}},
        ],
        {"doc_id": "doc-1", "document_hash": "doc-hash", "title": "Test"},
    )

    first_section = [c for c in chunks if c["metadata"]["section"] == "1 First"]
    second_section = [c for c in chunks if c["metadata"]["section"] == "2 Second"]
    assert [len(c["content"]) for c in first_section] == [800, 800, 440]
    assert first_section[0]["content"][-120:] == first_section[1]["content"][:120]
    assert first_section[1]["content"][-120:] == first_section[2]["content"][:120]
    assert second_section[0]["content"] == "second-section-only"
    assert all(len(c["content"]) <= 800 for c in chunks)
    assert all(c["metadata"]["doc_id"] == "doc-1" for c in chunks)
    assert all(c["metadata"]["document_hash"] == "doc-hash" for c in chunks)


def test_full_rebuild_sends_identical_chunks_to_dense_and_bm25(monkeypatch):
    chunks = [{"id": "same-id", "content": "text", "metadata": {"category": "test"}}]
    observed = {}
    monkeypatch.setattr(init_knowledge, "build_chunks", lambda: chunks)
    monkeypatch.setattr(init_knowledge.store, "reset_collection", lambda: observed.setdefault("reset", True))
    monkeypatch.setattr(init_knowledge.store, "add_atoms", lambda value: observed.setdefault("dense", value) and len(value))
    monkeypatch.setattr(init_knowledge.store, "count", lambda: 1)
    monkeypatch.setattr(init_knowledge.lexical, "build_index", lambda value: observed.setdefault("bm25", value) and len(value))

    stats = init_knowledge.init_knowledge()

    assert observed["dense"] is observed["bm25"] is chunks
    assert stats["chunks"] == 1


def test_incremental_update_parses_changed_documents_and_deletes_stale_chunks(monkeypatch):
    atoms = [
        {"id": "old-a", "content": "old", "metadata": {
            "doc_id": "a", "document_hash": "old", "category": "test",
        }},
        {"id": "old-b", "content": "removed", "metadata": {
            "doc_id": "b", "document_hash": "same", "category": "test",
        }},
    ]
    observed = {}
    monkeypatch.setattr(init_knowledge, "document_manifest", lambda *_: [
        {"doc_id": "a", "hash": "new"},
        {"doc_id": "c", "hash": "new"},
    ])

    def build_chunks(**kwargs):
        observed["document_ids"] = kwargs["document_ids"]
        return [
            {"id": "new-a", "content": "changed", "metadata": {
                "doc_id": "a", "document_hash": "new", "category": "test",
            }},
            {"id": "new-c", "content": "added", "metadata": {
                "doc_id": "c", "document_hash": "new", "category": "test",
            }},
        ]

    def delete_ids(ids):
        observed["deleted"] = set(ids)
        atoms[:] = [atom for atom in atoms if atom["id"] not in ids]
        return len(ids)

    def add_atoms(chunks):
        observed["upserted"] = chunks
        atoms.extend(chunks)
        return len(chunks)

    monkeypatch.setattr(init_knowledge, "build_chunks", build_chunks)
    monkeypatch.setattr(init_knowledge.store, "list_atoms", lambda: list(atoms))
    monkeypatch.setattr(init_knowledge.store, "delete_ids", delete_ids)
    monkeypatch.setattr(init_knowledge.store, "add_atoms", add_atoms)
    monkeypatch.setattr(init_knowledge.store, "count", lambda: len(atoms))
    monkeypatch.setattr(init_knowledge.lexical, "build_index", lambda chunks: len(chunks))

    stats = init_knowledge.update_knowledge()

    assert observed["document_ids"] == {"a", "c"}
    assert observed["deleted"] == {"old-a", "old-b"}
    assert {atom["id"] for atom in atoms} == {"new-a", "new-c"}
    assert stats["changed_documents"] == 2
    assert stats["removed_documents"] == 1


def test_incremental_update_does_not_parse_or_embed_unchanged_documents(monkeypatch):
    atoms = [{"id": "a-1", "content": "same", "metadata": {
        "doc_id": "a", "document_hash": "same", "category": "test",
    }}]
    monkeypatch.setattr(init_knowledge, "document_manifest", lambda *_: [
        {"doc_id": "a", "hash": "same"},
    ])
    monkeypatch.setattr(init_knowledge.store, "list_atoms", lambda: list(atoms))
    monkeypatch.setattr(init_knowledge.store, "delete_ids", lambda ids: len(ids))
    monkeypatch.setattr(
        init_knowledge, "build_chunks", lambda **_: pytest.fail("不应重新解析未变化文档"),
    )
    monkeypatch.setattr(
        init_knowledge.store, "add_atoms", lambda _: pytest.fail("不应重新 Embedding 未变化文档"),
    )
    monkeypatch.setattr(init_knowledge.store, "count", lambda: len(atoms))
    monkeypatch.setattr(init_knowledge.lexical, "build_index", lambda chunks: len(chunks))

    stats = init_knowledge.update_knowledge()

    assert stats["changed_documents"] == 0
    assert stats["upserted_chunks"] == 0


def test_bm25_index_round_trip_and_category_filter(tmp_path, monkeypatch):
    chunks = [
        {"id": "move", "content": "C-MOVE failed with status 0xA801", "metadata": {
            "category": "dicom_standard",
        }},
        {"id": "store", "content": "C-STORE transfers DICOM instances", "metadata": {
            "category": "dicom_standard",
        }},
        {"id": "local", "content": "C-MOVE local troubleshooting 0xA801", "metadata": {
            "category": "local_doc",
        }},
    ]
    monkeypatch.setattr(lexical.settings.rag, "lexical_index_dir", str(tmp_path / "bm25"))
    lexical.load_index.cache_clear()

    assert lexical.build_index(chunks) == 3
    rows = lexical._query("C-MOVE 0xA801", k=5, category="dicom_standard")

    assert rows[0]["id"] == "move"
    assert all(row["metadata"]["category"] == "dicom_standard" for row in rows)
    assert isinstance(rows[0]["bm25_score"], float)


def test_chroma_collection_uses_explicit_hnsw_configuration(monkeypatch):
    observed = {}
    sentinel = object()

    class Client:
        def get_or_create_collection(self, **kwargs):
            observed.update(kwargs)
            return sentinel

    store.get_collection.cache_clear()
    monkeypatch.setattr(store, "_client", lambda: Client())
    try:
        assert store.get_collection() is sentinel
    finally:
        store.get_collection.cache_clear()

    assert observed["configuration"] == {"hnsw": {
        "space": "cosine",
        "ef_construction": 100,
        "ef_search": 100,
        "max_neighbors": 16,
    }}
