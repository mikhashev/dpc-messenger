"""read_file refuses documents, containers and images instead of returning U+FFFD garbage."""
import pytest

from dpc_client_core.dpc_agent.tools.core import get_tools, read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(agent_root=tmp_path)


def _put(ctx, name, data):
    (ctx.agent_root / name).write_bytes(data)
    return name


def _description():
    entry = next(t for t in get_tools() if t.name == "read_file")
    return entry.schema["description"]


@pytest.mark.parametrize("name", ["a.docx", "a.pdf", "a.epub", "a.fb2", "a.rtf", "a.djvu", "a.PNG", "a.zip"])
def test_suffix_is_refused(ctx, name):
    out = read_file(ctx, _put(ctx, name, b"plain ascii text"))
    assert out.startswith("⚠️")


@pytest.mark.parametrize("name,data", [
    ("note.txt", b"PK\x03\x04" + b"\x00" * 20),
    ("x.txt", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20),
    ("p.txt", b"%PDF-1.7\n" + b"x" * 20),
    ("d.txt", b"AT&TFORM\x00\x00\x00\x10DJVU"),
    ("i.txt", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8),
    ("j.txt", b"\xff\xd8\xff\xe0" + b"\x00" * 12),
    ("w.txt", b"RIFF\x10\x00\x00\x00WEBPVP8 "),
    ("r.txt", b"Rar!\x1a\x07\x00"),
    ("g.txt", b"\x1f\x8b\x08\x00" + b"\x00" * 8),
])
def test_signature_is_refused_under_a_text_name(ctx, name, data):
    assert read_file(ctx, _put(ctx, name, data)).startswith("⚠️")


def test_refusal_names_the_right_tool(ctx):
    assert "read_document" in read_file(ctx, _put(ctx, "a.pdf", b"x"))
    assert "describe_image" in read_file(ctx, _put(ctx, "a.png", b"x"))
    docx = read_file(ctx, _put(ctx, "a.docx", b"x"))
    assert "does not support this format yet" in docx
    assert "Archives are not read" in read_file(ctx, _put(ctx, "a.zip", b"x"))


def test_text_still_reads(ctx):
    assert read_file(ctx, _put(ctx, "n.md", b"# Title\nbody\n")) == "# Title\nbody\n"
    assert read_file(ctx, _put(ctx, "s.py", b"print(1)\n")) == "print(1)\n"
    cyr = "Привет, мир\n"
    assert read_file(ctx, _put(ctx, "c.txt", cyr.encode("utf-8"))) == cyr


def test_empty_and_short_files_read(ctx):
    assert read_file(ctx, _put(ctx, "e.txt", b"")) == ""
    assert read_file(ctx, _put(ctx, "s.txt", b"hi")) == "hi"


def test_description_names_the_tools_the_refusal_names(ctx):
    desc = _description()
    pdf = read_file(ctx, _put(ctx, "a.pdf", b"x"))
    img = read_file(ctx, _put(ctx, "a.jpg", b"x"))
    assert "read_document" in pdf and "read_document" in desc
    assert "describe_image" in img and "describe_image" in desc


TEXT = "Привет, мир\nline two\nline three\n"
CRLF_TEXT = TEXT.replace("\n", "\r\n")
LE, BE, U8 = b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"


@pytest.mark.parametrize("name,data", [
    ("le.txt", LE + TEXT.encode("utf-16-le")),
    ("be.txt", BE + TEXT.encode("utf-16-be")),
    ("bom8.txt", U8 + TEXT.encode("utf-8")),
])
def test_bom_text_reads_as_text(ctx, name, data):
    assert read_file(ctx, _put(ctx, name, data)) == TEXT


def test_crlf_utf16_reads_the_same_on_every_platform(ctx):
    # bytes in, bytes decoded: the decode never goes through the locale or newline translation
    out = read_file(ctx, _put(ctx, "crlf.txt", LE + CRLF_TEXT.encode("utf-16-le")))
    assert out == CRLF_TEXT


def test_utf16_pagination_counts_lines(ctx):
    name = _put(ctx, "p16.txt", LE + TEXT.encode("utf-16-le"))
    out = read_file(ctx, name, offset=1, limit=1)
    assert "line two" in out and "line three" not in out


def test_extended_path_read_decodes_utf16(ctx, tmp_path):
    from dpc_client_core.dpc_agent.tools.core import extended_path_read
    f = tmp_path / "ext16.txt"
    f.write_bytes(LE + TEXT.encode("utf-16-le"))
    ctx.validate_extended_path = lambda path, require_write=False: f
    assert extended_path_read(ctx, str(f)) == TEXT
