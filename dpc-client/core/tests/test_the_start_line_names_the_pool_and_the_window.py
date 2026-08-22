"""Two numbers decide how much room there is, and neither was on the start line.

`n_ctx` is the KV pool the child allocates for all of its slots together;
`context_window` is what one conversation may occupy. Nothing derives one from
the other, so they can disagree — and one direction is silent: a window larger
than the pool lets an agent fill to a limit the engine never allocated.
"""

from __future__ import annotations

import logging

import pytest

from dpc_client_core.managers.llama_server_supervisor import (
    LlamaServerSupervisor,
    window_outgrows_pool,
)


def _sup(**config):
    config.setdefault("gguf_path", "C:/models/m.gguf")
    s = LlamaServerSupervisor("test-alias", config)
    s.port = 8080
    return s


def _line(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


# --- the predicate ----------------------------------------------------------


def test_only_a_window_larger_than_the_pool_counts():
    assert window_outgrows_pool(262145, 262144) is True
    assert window_outgrows_pool(262144, 262144) is False, "equal is the ordinary case"
    assert window_outgrows_pool(131072, 262144) is False, "smaller wastes cells, quietly"


def test_an_unset_window_is_not_a_disagreement():
    for absent in (None, 0, ""):
        assert window_outgrows_pool(absent, 262144) is False, absent


# --- the start line ---------------------------------------------------------


def test_the_start_line_names_both_numbers(caplog):
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=343600, context_window=110000).log_start("q4_0", {})

    line = "\n".join(_line(caplog, logging.INFO))
    assert "n_ctx=343600" in line
    assert "context_window=110000" in line


def test_a_window_nobody_set_reads_as_silence_not_as_zero(caplog):
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=262144).log_start(None, {})

    assert "context_window=build default" in "\n".join(_line(caplog, logging.INFO))


def test_the_pool_is_named_even_when_it_is_the_default(caplog):
    """It is DEFAULTS that supplies 262144, and the line must still say so —
    the number was previously visible nowhere: not in the line, not in a logged
    command, only inside the child's own arguments."""
    with caplog.at_level(logging.INFO):
        _sup().log_start(None, {})

    assert "n_ctx=262144" in "\n".join(_line(caplog, logging.INFO))


# --- the warning ------------------------------------------------------------


def test_a_window_larger_than_the_pool_is_said_out_loud(caplog):
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=262144, context_window=524288).log_start(None, {})

    warnings = _line(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "524288" in warnings[0] and "262144" in warnings[0]


def test_the_ordinary_configuration_says_nothing(caplog):
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=343600, context_window=110000).log_start(None, {})
    assert _line(caplog, logging.WARNING) == []

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=262144, context_window=262144).log_start(None, {})
    assert _line(caplog, logging.WARNING) == []


def test_the_cache_size_still_reaches_the_line_from_the_environment(caplog):
    """The reason this line exists at all — do not lose it while adding to it."""
    with caplog.at_level(logging.INFO):
        _sup().log_start(None, {"LLAMA_ARG_CACHE_RAM": "24576"})

    assert "cache_ram=24576 (from environment)" in "\n".join(_line(caplog, logging.INFO))


# --- which executable wrote the line ----------------------------------------


def test_the_start_line_names_the_binary_it_is_starting(caplog):
    """One file per alias, no line naming the executable — so two builds' starts
    are indistinguishable in the child log, and on 2026-08-22 a reviewer who
    read the whole file (the thing the brief asked for) drew the opposite
    conclusion from the right lines: the failures were the pin and the
    successes a build from an open PR. The instrument failed, not the reader.

    Ours is the cheaper half to fix — the start line is our own file — and it
    matters more since `binary_path` reached the provider form, which makes two
    builds side by side an ordinary state.
    """
    from pathlib import Path

    with caplog.at_level(logging.INFO):
        _sup(n_ctx=262144).log_start(None, {}, Path("D:/build/llama.cpp-pr27342/llama-server.exe"))

    line = "\n".join(_line(caplog, logging.INFO))
    assert "llama.cpp-pr27342" in line
    assert "binary=" in line


def test_a_start_line_with_no_binary_says_so_rather_than_implying_the_pin(caplog):
    with caplog.at_level(logging.INFO):
        _sup(n_ctx=262144).log_start(None, {})

    line = "\n".join(_line(caplog, logging.INFO))
    assert "binary=unknown" in line
