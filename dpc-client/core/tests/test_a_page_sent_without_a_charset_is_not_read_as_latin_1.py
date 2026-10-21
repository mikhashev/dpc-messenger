"""A page whose Content-Type names no charset is decoded by what the page says.

`requests` gives a `text/*` response with no `charset=` the encoding
ISO-8859-1 and `.text` follows it, ignoring `<meta charset="utf-8">`. The agent's
`browse_page` saved UTF-8 Russian as mojibake for such a server
(`http://dialog-seversk.ru/news`: `Content-Type: text/html`, `<meta charset="utf-8"/>`).

The tests build real `requests.Response` objects, so the layer under test is the
one that broke: `_fetch_url` reading `response.content`, not a helper in isolation.
Nothing here prints non-ASCII text; all comparisons are in memory.
"""

import io

import pytest
import requests

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import _decode_body, _fetch_url

PHRASE = "Категории новостей города"
URL = "http://example.test/news"


def _response(body: bytes, content_type: str) -> requests.Response:
    resp = requests.Response()
    resp._content = body
    resp.status_code = 200
    resp.url = URL
    if content_type is not None:
        resp.headers["Content-Type"] = content_type
    # What requests itself would have assigned from the header alone.
    resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
    return resp


def _serve(monkeypatch, body: bytes, content_type: str) -> dict:
    monkeypatch.setattr(
        requests, "get", lambda url, headers=None, timeout=None: _response(body, content_type)
    )
    return _fetch_url(URL)


def _page(meta: str, text: str = PHRASE) -> str:
    return f"<!DOCTYPE html><html><head>{meta}<title>t</title></head><body><p>{text}</p></body></html>"


class TestRequestsBranch:
    def test_the_precondition_requests_would_misread_this_response(self):
        # Guards the test itself: if requests ever stops assuming latin-1 the
        # cases below no longer exercise the defect.
        resp = _response(_page('<meta charset="utf-8"/>').encode("utf-8"), "text/html")
        assert resp.encoding == "ISO-8859-1"
        assert PHRASE not in resp.text

    def test_meta_charset_utf8_without_a_header_charset(self, monkeypatch):
        body = _page('<meta charset="utf-8"/>').encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert PHRASE in result["content"]
        assert "Ð" not in result["content"]

    def test_meta_charset_windows_1251(self, monkeypatch):
        body = _page('<meta charset="windows-1251">').encode("cp1251")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert PHRASE in result["content"]

    def test_meta_http_equiv_form(self, monkeypatch):
        meta = '<meta http-equiv="Content-Type" content="text/html; charset=KOI8-R">'
        body = _page(meta).encode("koi8-r")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert PHRASE in result["content"]

    def test_header_charset_still_wins_over_the_sniff(self, monkeypatch):
        body = _page('<meta charset="utf-8">').encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html; charset=utf-8")
        assert PHRASE in result["content"]
        assert result["content_type"] == "text/html; charset=utf-8"

    def test_header_charset_beats_a_wrong_meta(self, monkeypatch):
        body = _page('<meta charset="windows-1251">').encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html; charset=UTF-8")
        assert PHRASE in result["content"]

    def test_unknown_header_charset_falls_through_to_meta(self, monkeypatch):
        body = _page('<meta charset="utf-8">').encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html; charset=not-a-codec")
        assert PHRASE in result["content"]

    def test_no_charset_no_meta_utf8_bytes_take_the_strict_utf8_step(self, monkeypatch):
        body = _page("").encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert PHRASE in result["content"]

    def test_no_declaration_and_not_utf8_uses_the_guess_not_an_exception(self, monkeypatch):
        body = _page("", "Привет, мир " * 20).encode("cp1251")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert result["success"] is True
        assert result["content"]  # decoded with replacement, never raised

    def test_ascii_body(self, monkeypatch):
        result = _fetch_url_ok(monkeypatch, b"<html><body>plain ascii</body></html>", "text/html")
        assert "plain ascii" in result["content"]

    def test_returned_dict_shape_is_unchanged(self, monkeypatch):
        body = _page('<meta charset="utf-8">').encode("utf-8")
        result = _fetch_url_ok(monkeypatch, body, "text/html")
        assert set(result) == {"success", "content", "status_code", "content_type"}
        assert result["status_code"] == 200
        assert result["content_type"] == "text/html"


def _fetch_url_ok(monkeypatch, body: bytes, content_type: str) -> dict:
    result = _serve(monkeypatch, body, content_type)
    assert result["success"] is True, result
    return result


class TestUrllibBranch:
    def test_meta_windows_1251_without_requests(self, monkeypatch):
        import urllib.request

        body = _page('<meta charset="windows-1251">').encode("cp1251")

        class _Resp(io.BytesIO):
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(browser_mod, "HAS_REQUESTS", False)
        # `urllib` is only imported by the module when requests is missing.
        monkeypatch.setattr(browser_mod, "urllib", urllib, raising=False)
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=None, context=None: _Resp(body)
        )
        result = _fetch_url(URL)
        assert result["success"] is True, result
        assert PHRASE in result["content"]
        assert result["content_type"] == "text/html"


class TestDecodeBody:
    def test_utf8_bom_is_stripped(self):
        assert _decode_body(b"\xef\xbb\xbf" + "Привет".encode("utf-8"), "text/html") == "Привет"

    def test_utf16_bom(self):
        assert _decode_body("Привет".encode("utf-16"), "text/plain") == "Привет"

    def test_utf16_le_bom_is_not_mistaken_for_utf32(self):
        assert _decode_body(b"\xff\xfe" + "ab".encode("utf-16-le"), "") == "ab"

    def test_xml_declaration(self):
        raw = '<?xml version="1.0" encoding="windows-1251"?><a>Привет</a>'.encode("cp1251")
        assert _decode_body(raw, "application/xml") == '<?xml version="1.0" encoding="windows-1251"?><a>Привет</a>'

    def test_declared_latin_1_is_read_as_windows_1252_like_a_browser(self):
        raw = b"<p>\x93quoted\x94</p>"
        assert _decode_body(raw, "text/html; charset=ISO-8859-1") == "<p>“quoted”</p>"

    def test_meta_naming_utf16_inside_ascii_bytes_reads_as_utf8(self):
        raw = _page('<meta charset="utf-16">').encode("utf-8")
        assert PHRASE in _decode_body(raw, "text/html")

    def test_a_json_body_is_not_sniffed_for_meta(self):
        raw = '{"s": "<meta charset=windows-1251>", "t": "Привет"}'.encode("utf-8")
        assert "Привет" in _decode_body(raw, "application/json")

    def test_empty_body(self):
        assert _decode_body(b"", "text/html") == ""

    def test_undecodable_bytes_are_replaced_not_raised(self):
        assert "�" in _decode_body(b"abc\xff\xfe\xfd", "text/html; charset=utf-8")

    def test_guess_is_consulted_only_after_strict_utf8_fails(self):
        calls = []

        def guess():
            calls.append(1)
            return "cp1251"

        assert _decode_body("Привет".encode("utf-8"), "text/html", guess) == "Привет"
        assert calls == []
        assert _decode_body("Привет".encode("cp1251"), "text/html", guess) == "Привет"
        assert calls == [1]

    def test_a_guess_that_raises_falls_back_to_cp1252(self):
        def boom():
            raise RuntimeError("detector exploded")

        assert _decode_body(b"caf\xe9", "text/html", boom) == "café"


class TestBrowseSyncEndToEnd:
    def test_static_browse_of_an_undeclared_utf8_page_keeps_the_cyrillic(self, monkeypatch):
        pytest.importorskip("trafilatura")
        paragraph = (PHRASE + ". ") * 30
        body = _page('<meta charset="utf-8"/>', paragraph).encode("utf-8")
        monkeypatch.setattr(
            requests, "get", lambda url, headers=None, timeout=None: _response(body, "text/html")
        )
        result = browser_mod._browse_sync(URL)
        assert result["success"] is True, result
        assert PHRASE in (result.get("text") or "")
        assert "Ð" not in (result.get("text") or "")
