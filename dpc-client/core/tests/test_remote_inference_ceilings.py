"""Every door a remote answer has to come back through (ADR-040 D4-0).

Three ceilings governed remote inference — 240 s at the UI door, 180 s for the
agent provider, 60 s for the remote-peer provider — against the host's own
`DEFAULT_TIMEOUT_SECONDS = 900.0`. Anything that runs longer than the
requester's ceiling and shorter than the host's is abandoned mid-flight while
the host keeps generating for nobody. 1200 s is the host's budget plus overhead.
"""

from __future__ import annotations

import inspect

from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.providers.dpc_agent_provider import DpcAgentProvider
from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider

CEILING = 1200.0


def test_the_ui_door_no_longer_gives_up_before_the_host_does():
    sig = inspect.signature(P2PCoordinator.request_inference_from_peer)
    assert sig.parameters["timeout"].default == CEILING


def test_the_remote_peer_provider_waits_the_full_budget():
    p = RemotePeerProvider("remote", {"peer_id": "dpc-node-x", "model": "m"})
    assert p.timeout == CEILING


def test_the_agent_provider_waits_the_full_budget():
    p = DpcAgentProvider("agent", {"model": "dpc_agent"})
    assert p.timeout == CEILING


def test_a_configured_timeout_still_wins():
    # The ceiling is a default, not a policy: an alias that knows its workload
    # is short may still say so.
    p = RemotePeerProvider("remote", {"peer_id": "dpc-node-x", "timeout": 90.0})
    assert p.timeout == 90.0


def test_the_ui_door_reads_its_ceiling_from_configuration(tmp_path):
    from dpc_client_core.settings import Settings
    s = Settings(tmp_path)
    assert s.get_remote_inference_timeout() == CEILING
    (tmp_path / "config.ini").write_text("[connection]\nremote_inference_timeout = 300\n")
    assert Settings(tmp_path).get_remote_inference_timeout() == 300.0
