# dpc/dpc/utils.py

from urllib.parse import urlparse, parse_qs

def parse_dpc_uri(uri: str) -> tuple[str, int, str]:
    """
    Parses a dpc:// URI and returns (host, port, node_id).
    Raises ValueError for invalid format.

    Whitespace is stripped from the URI and from node_id. These URIs are passed
    around by hand — through chats, terminals, notes — and come back padded or
    line-wrapped. Left in, a single space made node_id a different string from
    the certificate's CN, and the client reported the mismatch to its user as
    "MITM attack detected". The identity was right; the copy-paste was not.
    """
    parsed = urlparse(uri.strip())
    if parsed.scheme != "dpc":
        raise ValueError("Invalid URI scheme. Must be 'dpc://'")

    host = parsed.hostname
    port = parsed.port

    query_params = parse_qs(parsed.query)
    node_id = query_params.get('node_id', [None])[0]
    if node_id:
        node_id = node_id.strip()

    if not all([host, port, node_id]):
        raise ValueError("URI must contain host, port, and node_id query parameter.")

    return host, port, node_id