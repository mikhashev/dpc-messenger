"""What goes on the wire must equal what the author stored.

Both fields below are inside the signing preimage (`specs/dptp_v1.md` §4.1),
which turns two cosmetic defects into one fatal one the moment signing starts:

  - `sender_name` was the literal "User", so a human's name never left the
    machine — and would shortly be a cryptographically attested wrong name;
  - `agent_owner` was a node_id in the monitor and a display name on the wire,
    so an author would sign one value and store the other, and its own
    `export_history` would ship a history that fails against its own signature.

The tests bind the real methods to a stub service: the collaborators are fake,
the code under test is not.
"""

import pytest

from dpc_client_core.service import CoreService


NODE = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
DISPLAY = "Mike Windows PC"
GROUP = "group-1234"


class _Group:
    group_id = GROUP
    members = [NODE]
    name = "1234"


class _P2P:
    node_id = NODE
    peers = {}

    def get_display_name(self):
        return DISPLAY


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Monitor:
    def __init__(self):
        self.seen = []
        self._history = []

    async def on_message(self, message):
        self.seen.append(message)
        self._history.append({
            "id": message.message_id,
            "content": message.text,
            "msg_index": len(self._history) + 1,
            "sender_name": message.sender_name,
            "agent_owner": getattr(message, "agent_owner", None),
        })

    def save_history(self):
        pass

    def get_message_history(self):
        return self._history

    def get_last_msg_index(self):
        return self._history[-1]["msg_index"] if self._history else 0

    def set_token_count(self, _):
        pass

    def get_token_usage(self):
        return {"token_limit": 128000}


class _Service:
    """Only the collaborators are stubbed; the methods under test are real."""

    def __init__(self):
        self.group_manager = _GroupManager()
        self.p2p_manager = _P2P()
        self.local_api = _Api()
        self.monitor = _Monitor()
        self.broadcast = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    async def _handle_group_agent_mentions(self, *a, **kw):
        pass

    def parse_mentions(self, text, members):
        return []

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []


class _GroupManager:
    def get_group(self, group_id):
        return _Group() if group_id == GROUP else None


def _payload_of(service):
    assert service.broadcast, "nothing was sent to the group"
    return service.broadcast[-1]["payload"]


@pytest.mark.asyncio
async def test_a_humans_name_reaches_the_wire():
    """The literal "User" meant the name never left the machine at all."""
    service = _Service()

    await CoreService.send_group_message(service, GROUP, "hello")

    assert _payload_of(service)["sender_name"] == DISPLAY


@pytest.mark.asyncio
async def test_the_stored_name_and_the_sent_name_are_the_same():
    """They are both inside the preimage; disagreeing means signing a lie."""
    service = _Service()

    await CoreService.send_group_message(service, GROUP, "hello")

    assert service.monitor.seen[-1].sender_name == _payload_of(service)["sender_name"]


@pytest.mark.asyncio
async def test_agent_owner_is_a_node_id_on_the_wire_as_it_is_in_the_record():
    """A display name on the wire and a node_id in the record cannot both be signed."""
    service = _Service()

    await CoreService.send_group_agent_message(service, GROUP, "Ark", "an answer")

    payload = _payload_of(service)
    assert payload["agent_owner"] == NODE
    assert service.monitor.seen[-1].agent_owner == payload["agent_owner"]
