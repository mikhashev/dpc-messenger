"""An agent message from this node carries only a name this node registered.

`send_group_agent_message` is on the local API whitelist. It copied `agent_name`
into `sender_name`, stamped `is_agent` and `agent_owner = this node`, and asked
nothing — so any process holding the WS token could post as «Ark», as «Mike»,
or under another node's `ext:` tag, and the record was signed by our key.

The registry is what `set_group_agents` writes: `group.agents[node]` (folder
ids and `ext:` tags) and `group.agent_names[node]`. The same transitional rule
as the mention gate applies: with no `ext:` tag registered here, the configured
CC display name still passes, so an install that never filled the Group
Settings field keeps posting.

The fake stubs collaborators only; the method under test and the registry
lookup are the real ones bound to it.
"""
import logging

import pytest

from dpc_client_core.service import CoreService


NODE = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
GROUP = "group-1234"


class _Group:
    group_id = GROUP
    members = [NODE]
    name = "1234"

    def __init__(self, agents, agent_names):
        self.agents = {NODE: agents}
        self.agent_names = {NODE: agent_names}


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
        })

    def save_history(self):
        pass

    def get_message_history(self):
        return self._history

    def set_token_count(self, _):
        pass

    def get_token_usage(self):
        return {"token_limit": 128000}


class _Service:
    def __init__(self, agents=(), agent_names=None, display_names=None, cc_name="CC"):
        self.group = _Group(list(agents), dict(agent_names or {}))
        self.group_manager = type("GM", (), {"get_group": lambda _s, gid: self.group if gid == GROUP else None})()
        self.p2p_manager = type("P2P", (), {"node_id": NODE})()
        self.local_api = _Api()
        self.monitor = _Monitor()
        self.broadcast = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self._display_names = dict(display_names or {})
        self._cc_name = cc_name

    # The real ones — a stub would only assert the stub agrees with itself.
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as
    _signature_fields_for = staticmethod(CoreService._signature_fields_for)

    def _get_agent_display_name(self, agent_id):
        return self._display_names.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return self._cc_name

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    async def _handle_group_agent_mentions(self, *a, **kw):
        pass

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []


async def _post(service, name):
    return await CoreService.send_group_agent_message(service, GROUP, name, "an answer")


def _assert_posted(service, result, name):
    assert isinstance(result, str) and len(result) == 16, f"no message id came back: {result!r}"
    assert service.monitor.seen[-1].sender_name == name, "the history write was not reached"
    assert service.broadcast[-1]["payload"]["sender_name"] == name, "the broadcast was not reached"


def _assert_refused(service, result, name, caplog):
    assert result == {"status": "error",
                      "message": f"agent name not registered for this node in this group: {name}"}
    assert service.monitor.seen == [], "the refusal still wrote history"
    assert service.broadcast == [], "the refusal still reached the peers"
    assert service.local_api.events == [], "the refusal still reached the UI"
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and name in r.getMessage() and GROUP in r.getMessage()]
    assert warnings, f"no WARNING named the group and the offered name: {[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_a_registered_external_tag_posts():
    service = _Service(agents=["ext:CC_mike"], agent_names={"ext:CC_mike": "CC_mike"})

    result = await _post(service, "CC_mike")

    _assert_posted(service, result, "CC_mike")


@pytest.mark.asyncio
async def test_a_teammates_name_is_refused_when_only_our_tag_is_registered(caplog):
    """The defect: the bridge registered as CC_mike could still post as Ark."""
    service = _Service(agents=["ext:CC_mike"], agent_names={"ext:CC_mike": "CC_mike"})

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        result = await _post(service, "Ark")

    _assert_refused(service, result, "Ark", caplog)


@pytest.mark.asyncio
async def test_nothing_registered_still_lets_the_configured_cc_name_through():
    """The transition: an install that never filled the field keeps posting."""
    service = _Service(agents=[], agent_names={}, cc_name="CC")

    result = await _post(service, "cc")

    _assert_posted(service, result, "cc")


@pytest.mark.asyncio
async def test_nothing_registered_refuses_any_other_name(caplog):
    service = _Service(agents=[], agent_names={}, cc_name="CC")

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        result = await _post(service, "Warren")

    _assert_refused(service, result, "Warren", caplog)


@pytest.mark.asyncio
async def test_an_embedded_agents_display_name_posts_even_with_a_stale_map():
    """`agent_names` was written at registration; the config name is read now."""
    service = _Service(agents=["agent_001"], agent_names={},
                       display_names={"agent_001": "Ark"})

    result = await _post(service, "Ark")

    _assert_posted(service, result, "Ark")


@pytest.mark.asyncio
async def test_registering_a_tag_closes_the_configured_name(caplog):
    """Once anything external is registered, the config name is no longer a way in —
    the same rule the mention gate applies, or the collision survives for anyone
    who registers."""
    service = _Service(agents=["ext:CC_mike"], agent_names={"ext:CC_mike": "CC_mike"}, cc_name="CC")

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        result = await _post(service, "CC")

    _assert_refused(service, result, "CC", caplog)
