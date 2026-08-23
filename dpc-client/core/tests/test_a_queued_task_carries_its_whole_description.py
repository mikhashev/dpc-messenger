"""«В scheduled tasks нельзя развернуть описание задачи» — Mike, 2026-08-16,
and again on 2026-08-24 looking at a REMINDER cut at «популярность/д…».

A queued task is the one human audit point before the work runs, and the board
drew it as a single `white-space: nowrap` line clipped by the pixel width of the
panel — half a sentence, with no control to open it. The completed rows had an
expand button, but it is gated on `has_full_result`, which the backend sets only
for tasks that already have a result file.

So the row had to start carrying its own text. These cover the reading of it,
because the field it lives in depends on who scheduled the task: the UI form
writes `message`, an agent deferring work for itself writes `text`, and two
older shapes are still on disk.
"""

import pytest

from dpc_client_core.agent_service import AgentService


def _text(data):
    return AgentService._queued_text({"data": data})


def test_the_form_field_is_read_first():
    assert _text({"message": "from the form"}) == "from the form"


def test_the_field_an_agent_defers_with_is_read_too():
    """`text` is what `agent.py:_execute_task` actually reads, so a task an
    agent scheduled for itself used to show nothing at all — the board went
    blank on exactly the tasks it exists to show."""
    assert _text({"text": "deferred by the agent"}) == "deferred by the agent"


def test_the_older_shapes_still_on_disk_are_read():
    assert _text({"task": "older shape"}) == "older shape"
    assert _text({"prompt": "older still"}) == "older still"


def test_the_form_field_wins_when_several_are_present():
    assert _text({"message": "form", "text": "agent"}) == "form"


def test_a_task_with_nothing_to_say_yields_an_empty_string():
    """Not None: the board renders the value, and `None` would print as one."""
    assert _text({}) == ""
    assert AgentService._queued_text({}) == ""
    assert AgentService._queued_text({"data": None}) == ""


def test_the_whole_description_survives_the_two_hundred_character_preview():
    """The preview is a preview. The point of the fix is that the row also
    carries the rest — a 200-character slice is what left the reader with half
    a sentence in the first place."""
    long = "Обсудить в рабочем чате продвижение DPC " * 20
    assert len(long) > 200
    assert AgentService._queued_text({"data": {"message": long}}) == long
