"""A file sent to a group or to one peer is held to the rules' file_transfer.max_size_mb.

A-GROUP-FILE-SEND-HAS-NO-SIZE-CAP-ON-THE-SENDER (Zcode review of b5c31fcd,
2026-11-19): the 1:1 agent-chat paperclip and the P2P voice send refused a file
over the cap, but `send_group_file` copied, hashed and offered a file of any
size, and the one-to-one `send_file` offered it too. Both now refuse first, with
the paperclip's text and its parsing (`file_transfer_cap_mb`): a cap that is a
bool, not a number, NaN, an infinity or 0 or less is the default, never "no cap".
The sender's default is SENDER_FILE_DEFAULT_MAX_MB (1000 MB, the receivers'
`proved_connection` default, Mike 2026-11-20), used when the rules have no
top-level `file_transfer.max_size_mb`; the paperclip keeps
AGENT_CHAT_FILE_DEFAULT_MAX_MB (100 MB). The receiver still holds the offer to
its own limit (FileTransferManager._file_transfer_decision); this is the
sender's early refusal, not a security boundary.

A small cap is set through the rules, or the default is patched down to 1 MB,
so no test writes a file anywhere near 1000 MB.

Real FileTransferManager over the in-process network of
test_a_group_file_crosses_a_full_mesh_once.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.service import CoreService
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP, _Network, full_mesh, records

MB = 1024 * 1024
INVALID_CAPS = [0, -5, "big", True, None, float("nan"), float("inf"), float("-inf")]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _picked(root, size, name="big.bin"):
    f = root / "outbox" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00" * size)
    return f


def _two_nodes(root, cap):
    net = _Network()
    a, b = full_mesh(net, root, 2)
    a.service.firewall.rules["file_transfer"]["max_size_mb"] = cap
    return net, a, b


# --- the defaults and the helper ----------------------------------------------------

def test_the_senders_default_is_1000_mb_and_the_paperclips_stays_100():
    assert service_mod.SENDER_FILE_DEFAULT_MAX_MB == 1000
    assert service_mod.AGENT_CHAT_FILE_DEFAULT_MAX_MB == 100


@pytest.mark.parametrize("cap", INVALID_CAPS)
def test_a_senders_invalid_cap_is_1000_mb(cap):
    assert service_mod.file_transfer_cap_mb(
        {"file_transfer": {"max_size_mb": cap}}, service_mod.SENDER_FILE_DEFAULT_MAX_MB) == 1000


@pytest.mark.parametrize("rules", [{}, {"file_transfer": {}}, {"file_transfer": None}, None])
def test_a_senders_rules_without_a_cap_are_1000_mb(rules):
    assert service_mod.file_transfer_cap_mb(rules, service_mod.SENDER_FILE_DEFAULT_MAX_MB) == 1000


# The paperclip's own default and parsing: test_the_file_transfer_cap_never_reads_as_no_cap.


def test_the_refusal_reads_as_the_paperclips():
    assert service_mod.file_transfer_cap_refusal(MB + 1, 1) == \
        "File too large (1.00 MB > 1 MB limit, file_transfer.max_size_mb)"
    assert service_mod.file_transfer_cap_refusal(MB, 1) is None


# --- a group file --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_group_file_over_the_cap_is_refused_before_any_copy_hash_or_offer(root, monkeypatch):
    net, a, b = _two_nodes(root, 1)
    hashed = []
    signed = []
    monkeypatch.setattr(ftm, "hash_file_in_one_pass", lambda *args, **kw: hashed.append(args))
    monkeypatch.setattr(service_mod, "hash_file_in_one_pass", lambda *args, **kw: hashed.append(args))
    monkeypatch.setattr(service_mod, "sign_file_description", lambda *args, **kw: signed.append(args))
    big = _picked(root, MB + 1)

    result = await CoreService.send_group_file(a.service, GROUP, str(big))

    assert result == {"status": "error",
                      "message": "File too large (1.00 MB > 1 MB limit, file_transfer.max_size_mb)"}
    assert not (a.root / GROUP / "files").exists(), "nothing copied"
    assert records(a) == [] and a.api.named("group_file_received") == []
    assert net.frames == [] and not a.manager.active_transfers, "nothing offered"
    assert hashed == [] and signed == [], "nothing hashed or signed"
    assert big.exists()


@pytest.mark.asyncio
async def test_a_group_file_within_the_cap_is_kept_and_offered(root):
    _net, a, b = _two_nodes(root, 1)

    result = await CoreService.send_group_file(a.service, GROUP, str(_picked(root, MB, "ok.bin")))

    assert result["status"] == "success", result
    assert len(result["transfer_ids"]) == 1
    assert (a.root / GROUP / "files" / "ok.bin").stat().st_size == MB


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", INVALID_CAPS)
async def test_a_group_files_invalid_cap_falls_back_to_the_senders_default_not_to_none(root, monkeypatch, cap):
    monkeypatch.setattr(service_mod, "SENDER_FILE_DEFAULT_MAX_MB", 1)
    net, a, _b = _two_nodes(root, cap)

    result = await CoreService.send_group_file(a.service, GROUP, str(_picked(root, MB + 1)))

    assert result["status"] == "error"
    assert "1.00 MB > 1 MB limit, file_transfer.max_size_mb" in result["message"]
    assert net.frames == [] and not (a.root / GROUP / "files").exists()


@pytest.mark.asyncio
async def test_a_group_file_with_no_cap_in_the_rules_is_held_to_the_senders_default(root, monkeypatch):
    monkeypatch.setattr(service_mod, "SENDER_FILE_DEFAULT_MAX_MB", 1)
    net, a, _b = _two_nodes(root, 1)
    del a.service.firewall.rules["file_transfer"]["max_size_mb"]

    result = await CoreService.send_group_file(a.service, GROUP, str(_picked(root, MB + 1)))

    assert result == {"status": "error",
                      "message": "File too large (1.00 MB > 1 MB limit, file_transfer.max_size_mb)"}
    assert net.frames == [] and not (a.root / GROUP / "files").exists()


@pytest.mark.asyncio
async def test_a_group_file_over_100_mb_is_not_held_to_the_paperclips_default(root, monkeypatch):
    # The real defaults scaled down 1000x: a file over the paperclip's default and
    # under the sender's passes when the rules name no cap.
    monkeypatch.setattr(service_mod, "AGENT_CHAT_FILE_DEFAULT_MAX_MB", 0.1)
    monkeypatch.setattr(service_mod, "SENDER_FILE_DEFAULT_MAX_MB", 1)
    _net, a, _b = _two_nodes(root, 1)
    del a.service.firewall.rules["file_transfer"]["max_size_mb"]

    result = await CoreService.send_group_file(a.service, GROUP, str(_picked(root, MB // 2, "mid.bin")))

    assert result["status"] == "success", result


# --- a one-to-one file ---------------------------------------------------------------

def _peer_send_stub(cap):
    coordinator = SimpleNamespace(send_file=AsyncMock(return_value={"status": "pending"}))
    rules = {"file_transfer": {} if cap is ... else {"max_size_mb": cap}}
    service = SimpleNamespace(
        p2p_coordinator=coordinator,
        file_transfer_manager=SimpleNamespace(firewall=SimpleNamespace(rules=rules)),
    )
    return service, coordinator


@pytest.mark.asyncio
async def test_a_peer_file_over_the_cap_is_refused_and_not_offered(root):
    service, coordinator = _peer_send_stub(1)

    with pytest.raises(ValueError, match=r"File too large \(1\.00 MB > 1 MB limit, file_transfer\.max_size_mb\)"):
        await CoreService.send_file(service, "dpc-node-" + "b" * 32, str(_picked(root, MB + 1)))

    coordinator.send_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_peer_file_within_the_cap_is_offered(root):
    service, coordinator = _peer_send_stub(1)
    path = str(_picked(root, MB, "ok.bin"))

    assert await CoreService.send_file(service, "dpc-node-" + "b" * 32, path, text="hi") == {"status": "pending"}

    coordinator.send_file.assert_awaited_once_with("dpc-node-" + "b" * 32, path, None, text="hi")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", INVALID_CAPS + [...])
async def test_a_peer_files_invalid_or_absent_cap_falls_back_to_the_senders_default(root, monkeypatch, cap):
    monkeypatch.setattr(service_mod, "SENDER_FILE_DEFAULT_MAX_MB", 1)
    service, coordinator = _peer_send_stub(cap)

    with pytest.raises(ValueError, match="1 MB limit"):
        await CoreService.send_file(service, "dpc-node-" + "b" * 32, str(_picked(root, MB + 1)))

    coordinator.send_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_peer_file_still_reaches_the_coordinators_not_found(root):
    service, coordinator = _peer_send_stub(1)
    coordinator.send_file.side_effect = FileNotFoundError("File not found")

    with pytest.raises(FileNotFoundError):
        await CoreService.send_file(service, "dpc-node-" + "b" * 32, str(root / "nowhere.bin"))
