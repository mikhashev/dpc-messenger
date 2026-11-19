"""The rules' file_transfer.max_size_mb, as the 1:1 agent-chat paperclip reads it.

`file_transfer_cap_mb` falls back to AGENT_CHAT_FILE_DEFAULT_MAX_MB for a cap
that is a bool, not a number, NaN, an infinity, or 0 or less: json.loads accepts
NaN/Infinity, and none of them may mean "no cap" (Zcode, review of 3f2c6600).

A sender-side cap on group and one-to-one files was written and held back on
2026-11-20: at the 100 MB default it would stop the owner sending a file larger
than that, against ADR-047's multi-gigabyte goal, while every receiver already
holds an offer to its own entry's limit. Card
A-GROUP-FILE-SEND-HAS-NO-SIZE-CAP-ON-THE-SENDER keeps the decision.
"""

import pytest

from dpc_client_core import service as service_mod

MB = 1024 * 1024
INVALID_CAPS = [0, -5, "big", True, None, float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("cap", INVALID_CAPS)
def test_a_cap_that_is_not_a_finite_positive_number_is_the_default(cap):
    assert service_mod.file_transfer_cap_mb({"file_transfer": {"max_size_mb": cap}}) \
        == service_mod.AGENT_CHAT_FILE_DEFAULT_MAX_MB


@pytest.mark.parametrize("rules", [{}, {"file_transfer": {}}, {"file_transfer": None}, None])
def test_rules_without_a_cap_have_the_default(rules):
    assert service_mod.file_transfer_cap_mb(rules) == service_mod.AGENT_CHAT_FILE_DEFAULT_MAX_MB


@pytest.mark.parametrize("cap", [1, 2.5, 1000])
def test_a_positive_cap_is_kept(cap):
    assert service_mod.file_transfer_cap_mb({"file_transfer": {"max_size_mb": cap}}) == cap


def test_the_refusal_reads_as_the_paperclips():
    assert service_mod.file_transfer_cap_refusal(MB + 1, 1) == \
        "File too large (1.00 MB > 1 MB limit, file_transfer.max_size_mb)"
    assert service_mod.file_transfer_cap_refusal(MB, 1) is None
