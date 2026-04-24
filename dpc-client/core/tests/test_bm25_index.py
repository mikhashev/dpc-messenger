"""Tests for BM25 keyword index (ADR-010, MEM-3.5)."""

from dpc_client_core.dpc_agent.bm25_index import BM25Index, tokenize, _detect_script


def test_whitespace_tokenizer():
    tokens = tokenize("Hello world test")
    assert "hello" in tokens
    assert "world" in tokens


def test_bigram_tokenizer_cjk():
    tokens = tokenize("你好世界测试内容很长的文本需要大量中文字符来触发双字母模式")
    assert len(tokens) > 0
    assert all(len(t) == 2 for t in tokens if not t[0].isspace())


def test_detect_script_latin():
    assert _detect_script("Hello world this is English text") == "whitespace"


def test_detect_script_cjk():
    assert _detect_script("你好世界测试内容很长的文本需要中文") == "bigram"


def test_build_and_search():
    idx = BM25Index()
    texts = ["Python programming language", "JavaScript web development", "Rust systems programming"]
    metas = [{"file": "py.md"}, {"file": "js.md"}, {"file": "rust.md"}]
    idx.build(texts, metas)
    results = idx.search("Python programming", top_k=2)
    assert len(results) >= 1
    assert results[0][0]["file"] == "py.md"


def test_search_empty():
    idx = BM25Index()
    assert idx.search("query") == []


def test_save_and_load(tmp_path):
    idx = BM25Index(tmp_path)
    texts = ["alpha beta", "gamma delta"]
    idx.build(texts, [{"file": "a.md"}, {"file": "b.md"}])
    idx.save()

    idx2 = BM25Index(tmp_path)
    assert idx2.load() is True
    assert idx2.total_documents == 2
    results = idx2.search("alpha")
    assert len(results) >= 1
