"""A URI that survived a copy-paste must not look like an attack.

A dpc:// URI travels through chats, terminals and notes, and comes back with
whitespace in it. Nothing stripped it, so `node_id=" dpc-node-…"` reached the
certificate check as a distinct string, the CN did not match, and the client
told its user:

    Certificate validation failed … 3. MITM attack detected

The identity was correct. One space made the client accuse the peer.
"""

import pytest

from dpc_protocol.utils import parse_dpc_uri


NODE_ID = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"


@pytest.mark.parametrize("uri", [
    f"dpc://192.168.0.20:8888?node_id= {NODE_ID}",       # space after '='
    f"dpc://192.168.0.20:8888?node_id={NODE_ID} ",       # trailing space
    f"dpc://192.168.0.20:8888?node_id=\t{NODE_ID}\n",    # tab and newline
    f"  dpc://192.168.0.20:8888?node_id={NODE_ID}  ",    # padded whole URI
])
def test_stray_whitespace_does_not_change_the_identity(uri):
    host, port, node_id = parse_dpc_uri(uri)

    assert node_id == NODE_ID
    assert host == "192.168.0.20"
    assert port == 8888


def test_a_clean_uri_still_parses():
    assert parse_dpc_uri(f"dpc://192.168.0.20:8888?node_id={NODE_ID}") == (
        "192.168.0.20", 8888, NODE_ID
    )


def test_ipv6_keeps_its_brackets_handling():
    host, port, node_id = parse_dpc_uri(f"dpc://[2001:db8::1]:8888?node_id={NODE_ID}")

    assert host == "2001:db8::1"
    assert node_id == NODE_ID


def test_whitespace_alone_is_not_a_node_id():
    """Stripping must not turn an empty parameter into a valid-looking one."""
    with pytest.raises(ValueError):
        parse_dpc_uri("dpc://192.168.0.20:8888?node_id=   ")
