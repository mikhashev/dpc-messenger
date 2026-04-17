"""Tests for extraction triggers (ADR-010, MEM-4.1)."""

from dpc_client_core.dpc_agent.extraction_triggers import check_triggers


def test_tool_call_trigger():
    r = check_triggers("short response", has_tool_calls=True)
    assert r.should_extract
    assert "tool_call_in_response" in r.reasons


def test_long_response_trigger():
    r = check_triggers("word " * 200)
    assert r.should_extract
    assert "long_response" in r.reasons


def test_decision_verb_ru():
    r = check_triggers("Мы решили использовать FAISS")
    assert r.should_extract
    assert "decision_verb" in r.reasons


def test_decision_verb_en():
    r = check_triggers("We decided to use hybrid search")
    assert r.should_extract
    assert "decision_verb" in r.reasons


def test_knowledge_claim():
    r = check_triggers("The conclusion is that RRF works best")
    assert r.should_extract
    assert "knowledge_claim" in r.reasons


def test_no_triggers():
    r = check_triggers("ok")
    assert not r.should_extract
    assert r.reasons == []


def test_multiple_triggers():
    r = check_triggers("We decided this because " + "x " * 200, has_tool_calls=True)
    assert len(r.reasons) >= 3
