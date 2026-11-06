"""A doubled `<tool_call>` opener does not hide written calls from the guard.

2026-11-06 14:57, DPC Project #168: Johnny's model wrote two `search_in_file` calls as
text with `</tool_call>\\n<tool_call>\\n<tool_call>` between them. The detector stopped at
the extra opener, read the rest as text after a quote, recognized neither call, and the
raw block was posted to the room instead of being asked again with the reason.
"""
from dpc_client_core.dpc_agent.loop import _written_tool_calls, without_tag_calls

_BLOCK = (
    "<tool_call>\n<function=search_in_file=pattern>\n{q}\n</parameter>\n<parameter=pattern>\n{q}\n"
    "</parameter>\n<parameter=file_path>\n{p}\n</parameter>\n<parameter=context_lines>\n{n}\n"
    "</parameter>\n</function>\n</tool_call>"
)
_GUARDS = "C:\\Users\\mikha\\Documents\\dpc-messenger\\dpc-client\\core\\dpc_client_core\\agent\\guards.py"
_SETTINGS = "C:\\Users\\mikha\\Documents\\dpc-messenger\\dpc-client\\core\\dpc_client_core\\settings.py"

# The reply as it was stored (661 characters): the second block has a second opener in front.
REPLY_168 = (
    _BLOCK.format(q="compaction could not reduce", p=_GUARDS, n=6)
    + "\n<tool_call>\n"
    + _BLOCK.format(q="compaction_enabled", p=_SETTINGS, n=4)
)


def test_the_stored_reply_is_the_one_that_was_posted():
    assert len(REPLY_168) == 661
    assert "</tool_call>\n<tool_call>\n<tool_call>\n<function=" in REPLY_168


def test_both_written_calls_are_recognised_and_nothing_is_left_as_an_answer():
    names, prose = _written_tool_calls(REPLY_168)

    assert names == ["search_in_file"]
    assert prose == ""


def test_a_doubled_opener_on_the_first_block_is_the_same_block():
    names, prose = _written_tool_calls("<tool_call>\n" + _BLOCK.format(q="x", p=_GUARDS, n=1))

    assert names == ["search_in_file"] and prose == ""


def test_the_raw_text_without_its_calls_is_empty():
    assert without_tag_calls(REPLY_168) == ""


def test_text_after_the_blocks_still_makes_them_a_quote():
    """The rule this does not change: a block with an answer after it is quoted, not called."""
    quoted = REPLY_168 + "\n\nThat is the format the model used; the search found two places."

    names, prose = _written_tool_calls(quoted)

    assert names == [] and prose == quoted
