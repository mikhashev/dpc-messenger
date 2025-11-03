# dpc/dpc/utils.py

from urllib.parse import urlparse, parse_qs

def parse_dpc_uri(uri: str) -> tuple[str, int, str]:
    """
    Parses a dpc:// URI and returns (host, port, node_id).
    Raises ValueError for invalid format.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "dpc":
        raise ValueError("Invalid URI scheme. Must be 'dpc://'")
    
    host = parsed.hostname
    port = parsed.port
    
    query_params = parse_qs(parsed.query)
    node_id = query_params.get('node_id', [None])[0]

    if not all([host, port, node_id]):
        raise ValueError("URI must contain host, port, and node_id query parameter.")
        
    return host, port, node_id