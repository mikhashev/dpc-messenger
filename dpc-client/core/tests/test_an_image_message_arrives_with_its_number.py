"""The number a message already has must reach the screen with it.

An image sent to a group was stored with a msg_index like every other message
and announced to the interface without one, so it drew with no number until
the page was reloaded and the history came back off disk. The text path next
door had solved this already — store, read the index, then announce — and this
holds the two paths to the same order.
"""

from __future__ import annotations

import inspect
import re

from dpc_client_core import service as service_module


def _body(name: str) -> str:
    src = inspect.getsource(service_module.CoreService)
    start = src.index(f"async def {name}")
    nxt = src.find("\n    async def ", start + 10)
    return src[start: nxt if nxt != -1 else len(src)]


def test_the_group_image_event_carries_the_index():
    body = _body("send_group_image")
    assert '"msg_index": msg_index' in body, "the interface is told without the number"


def test_the_index_is_read_before_the_event_is_sent():
    """Order is the whole mechanism: the index does not exist until the message
    is stored, so announcing first can only ever send None."""
    body = _body("send_group_image")
    stored = body.index("get_last_msg_index()")
    announced = body.index('broadcast_event("group_file_received"')
    assert stored < announced, "the event goes out before the index exists"


def test_the_voice_path_was_not_left_behind():
    body = _body("send_group_voice_message")
    assert "get_last_msg_index()" in body and '"msg_index": msg_index' in body


def test_every_group_file_event_carries_the_field():
    """A second sender of the same event without the field would put the defect
    straight back, and it would look exactly like the first time."""
    src = inspect.getsource(service_module)
    events = re.findall(
        r'broadcast_event\(\s*"group_file_received",\s*\{(.*?)\}\s*\)', src, re.S
    )
    assert events, "no group_file_received broadcasts found — has the name changed?"
    missing = [e for e in events if "msg_index" not in e]
    assert not missing, f"{len(missing)} of {len(events)} broadcasts omit msg_index"
