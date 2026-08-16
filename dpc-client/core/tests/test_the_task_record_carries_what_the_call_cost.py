"""The task result must carry what the task cost, not an empty dict.

`tokens: {}` in 56 of 56 task results is what a field added and never read
back looks like a year later: `agent.py` asks `usage.get("tokens", {})` while
the loop keeps its counters flat and creates no `tokens` key at all. The data
was there the whole time and was asked for by the wrong name.

The provider-shaped fields (cache split, reasoning, the effort word) are
optional on purpose: no other provider has them, and present-and-empty is the
misreading this entry exists to correct.
"""

def test_the_durable_record_stops_being_empty():
    """`tokens: {}` in 56 of 56 task results is what a field added and never
    read back looks like a year later: the loop keeps its counters flat and
    the writer asked for a nested key nobody creates."""
    from dpc_client_core.dpc_agent.agent import tokens_block

    block = tokens_block({"prompt_tokens": 1000, "completion_tokens": 900, "total_tokens": 1900})
    assert block["prompt_tokens"] == 1000
    assert block["completion_tokens"] == 900
    assert block["total_tokens"] == 1900


def test_fields_no_provider_reported_are_absent_rather_than_zero():
    """Present-and-empty reads as "we looked and there was nothing", which is
    the misreading this whole entry exists to correct."""
    from dpc_client_core.dpc_agent.agent import tokens_block

    block = tokens_block({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert "reasoning_tokens" not in block
    assert "prompt_cache_hit_tokens" not in block


def test_the_split_and_the_effort_survive_into_the_record():
    from dpc_client_core.dpc_agent.agent import tokens_block

    block = tokens_block({
        "prompt_tokens": 1000, "completion_tokens": 900, "total_tokens": 1900,
        "prompt_cache_hit_tokens": 320, "prompt_cache_miss_tokens": 680,
        "reasoning_tokens": 850, "reasoning_effort": "high",
    })
    assert block["prompt_cache_hit_tokens"] == 320
    assert block["prompt_cache_miss_tokens"] == 680
    assert block["reasoning_tokens"] == 850
    assert block["reasoning_effort"] == "high"


def test_a_multi_round_task_adds_the_rounds_up():
    """One task is several calls; the record is for the task."""
    from dpc_client_core.dpc_agent.loop import merge_optional_usage

    acc = {}
    merge_optional_usage(acc, {"prompt_cache_hit_tokens": 100, "reasoning_tokens": 40})
    merge_optional_usage(acc, {"prompt_cache_hit_tokens": 220, "reasoning_tokens": 60})
    assert acc["prompt_cache_hit_tokens"] == 320
    assert acc["reasoning_tokens"] == 100
    assert "prompt_cache_miss_tokens" not in acc


def test_a_round_from_a_provider_that_reports_nothing_adds_nothing():
    from dpc_client_core.dpc_agent.loop import merge_optional_usage

    acc = {}
    merge_optional_usage(acc, {"prompt_tokens": 10, "completion_tokens": 5})
    assert acc == {}
