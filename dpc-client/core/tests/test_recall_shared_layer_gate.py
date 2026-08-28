"""Revoking access to the shared layer has to reach the hint, not only the read.

`read_file` asks the knowledge gate on every call, so revocation blocks reads at once.
The index is not asked and does not change: the L6 rows stay, each carrying the first
500 characters of its document. So the hint went on printing an address `read_file` had
already begun refusing — and under it, 200 characters of the content the gate was
closed to withhold.
"""

from __future__ import annotations

from dpc_client_core.dpc_agent.active_recall import (
    _has_something_to_offer,
    get_recall_block,
    hint_address,
    render_recall_hints,
)
from dpc_client_core.dpc_agent.hybrid_search import SearchResult


def _shared(text: str = "the shared document body") -> dict:
    return {
        "source_file": "L6/commit-note.md",
        "source_path": r"C:\Users\mikha\.dpc\knowledge\commit-note.md",
        "source_layer": "L6",
        "heading": "Commit Note",
        "text": text,
    }


def _result(meta: dict, score: float = 1.0) -> SearchResult:
    return SearchResult(chunk_meta=meta, score=score, source="hybrid")


def test_the_shared_layer_offers_an_address_while_the_gate_is_open():
    assert hint_address(_shared(), shared_knowledge_enabled=True) == _shared()["source_path"]


def test_the_shared_layer_offers_no_address_once_the_gate_is_shut():
    assert hint_address(_shared(), shared_knowledge_enabled=False) is None


def test_the_gate_is_asked_now_not_at_indexing_time():
    """The row proves the gate was open when it was written, and says nothing about
    today. This is the whole defect: presence in the index was read as permission."""
    indexed_while_allowed = _shared()

    assert hint_address(indexed_while_allowed, shared_knowledge_enabled=False) is None


def test_a_shut_gate_drops_the_document_rather_than_quoting_it():
    """An excerpt normally redeems a missing address. Not here — the excerpt is the
    thing being withheld."""
    assert not _has_something_to_offer(_result(_shared()), True, shared_knowledge_enabled=False)


def test_an_external_file_still_earns_its_slot_with_an_excerpt():
    """The dead-end courtesy for extended paths is untouched: there the toggle governs
    reaching the file, not seeing that it exists."""
    external = {"source_file": "EXT/project/README.md", "source_path": r"C:\project\README.md",
                "source_layer": "EXT", "text": "readme body"}

    assert _has_something_to_offer(_result(external), extended_read_enabled=False)


def test_no_part_of_the_document_reaches_the_block_once_the_gate_is_shut():
    body = "SECRET-BODY-MARKER"
    block = get_recall_block([_result(_shared(body))], agent_root=None,
                             shared_knowledge_enabled=False)

    assert body not in block.text
    assert "commit-note" not in block.text
    assert block.injected == []


def test_the_block_carries_the_document_while_the_gate_is_open():
    body = "SECRET-BODY-MARKER"
    block = get_recall_block([_result(_shared(body))], agent_root=None,
                             shared_knowledge_enabled=True)

    assert body in block.text
    assert "commit-note" in block.text


def test_the_dead_end_names_the_gate_that_is_actually_shut():
    """Two layers reach that line. Telling an agent to check extended paths when the
    knowledge gate is the one refusing sends it to the wrong switch."""
    no_path = {"source_file": "L6/commit-note.md", "source_layer": "L6",
               "heading": "Commit Note", "text": "body"}

    rendered = render_recall_hints([_result(no_path)])

    assert "shared knowledge access is off" in rendered.text
    assert "extended path" not in rendered.text


def test_hints_only_mode_also_stops_naming_a_gated_document():
    """The compact mode prints no excerpts, but a filename is still a disclosure, and
    the filter runs before the mode is chosen."""
    block = get_recall_block([_result(_shared())], context_usage_ratio=0.6,
                             agent_root=None, shared_knowledge_enabled=False)

    assert block.mode == "hints"
    assert "commit-note" not in block.text
