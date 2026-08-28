import pytest

from dpc_client_core.dpc_agent.tools.core import _paginate_content

LIMIT = 1000


def _doc(lines: int = 400) -> str:
    return "".join(f"line {i}\n" for i in range(lines))


def test_truncated_read_announces_extent_in_a_header():
    content = _doc()
    out = _paginate_content(content, "doc.md", None, None, fallback_truncate=LIMIT)
    header = out.splitlines()[0]
    assert header.startswith("[Lines 1-")
    assert f"of {len(content.splitlines())}" in header
    assert "continue: offset=" in header


def test_offered_offset_continues_exactly_where_the_head_stopped():
    content = _doc()
    out = _paginate_content(content, "doc.md", None, None, fallback_truncate=LIMIT)
    header, _, head = out.partition("\n")
    offset = int(header.split("offset=")[1].rstrip("]"))

    rest = _paginate_content(content, "doc.md", offset, None, fallback_truncate=LIMIT)
    rest_body = rest.partition("\n")[2]

    # No gap, no overlap: the two halves reassemble the document.
    assert head + rest_body == content


def test_short_file_is_returned_untouched():
    content = "one\ntwo\n"
    assert _paginate_content(content, "doc.md", None, None, fallback_truncate=LIMIT) == content


def test_explicit_pagination_keeps_its_own_header():
    content = _doc()
    out = _paginate_content(content, "doc.md", 10, 5, fallback_truncate=LIMIT)
    assert out.splitlines()[0] == f"[Lines 11-15 of {len(content.splitlines())} total | doc.md]"
