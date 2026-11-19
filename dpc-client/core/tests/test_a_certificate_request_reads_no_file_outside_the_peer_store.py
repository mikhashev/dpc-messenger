"""A CERT_REQUEST names node_id; only a key-hash id may name a certificate file.

Before this guard `_read_local_certificate` built ~/.dpc/peers/{node_id}.crt from
whatever the asking peer wrote, so "C:/any/path/x", "../../etc/ssl/foo" or an
upper-case copy of an id read any readable *.crt and sent it back (second review
of ADR-047 phase 1, 2026-11-20). The shared check is dpc_protocol.crypto
.is_valid_node_id (2a930e66).
"""

import pytest

from dpc_client_core.message_handlers import certificate_handler as ch

GOOD = "dpc-node-" + "a" * 32


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "DPC_HOME_DIR", tmp_path / ".dpc")
    peers = tmp_path / ".dpc" / "peers"
    peers.mkdir(parents=True)
    (peers / f"{GOOD}.crt").write_text("GOOD CERT", encoding="utf-8")
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    (secret / "x.crt").write_text("SECRET", encoding="utf-8")
    return tmp_path


def test_a_key_hash_id_reads_its_certificate(home):
    assert ch._read_local_certificate(GOOD, own_node_id="dpc-node-" + "b" * 32) == "GOOD CERT"


@pytest.mark.parametrize("make_id", [
    lambda home: str(home / "elsewhere" / "x"),          # absolute path
    lambda home: "../../elsewhere/x",                     # relative escape
    lambda home: f"{GOOD}/../../../elsewhere/x",           # prefix, then escape
    lambda home: GOOD.upper(),                             # case variant
    lambda home: None,                                     # not a string
])
def test_any_other_id_reads_nothing(home, make_id):
    assert ch._read_local_certificate(make_id(home), own_node_id="dpc-node-" + "b" * 32) is None
