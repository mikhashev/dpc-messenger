"""
Tests for browser tools in dpc_agent/tools/browser.py.

Tests cover:
- HTML text extraction with script/style tag filtering
- Edge cases that regexp-based filtering misses
- Malformed HTML handling
- URL fetching and JSON parsing
"""

import pytest
from dpc_client_core.dpc_agent.tools.browser import (
    _extract_text,
    _fetch_url,
    browse_page,
    fetch_json,
    extract_links,
    check_url,
)


class TestExtractText:
    """Test HTML text extraction with robust filtering."""

    def test_basic_html_extraction(self):
        """Test basic HTML to text conversion."""
        html = "<html><body><h1>Hello World</h1><p>This is a test.</p></body></html>"
        result = _extract_text(html)
        assert "Hello World" in result
        assert "This is a test." in result

    def test_remove_script_tags(self):
        """Test that script tags are properly removed."""
        html = '<html><body><p>Hello</p><script>alert("xss")</script><p>World</p></body></html>'
        result = _extract_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "xss" not in result
        assert "alert" not in result

    def test_remove_style_tags(self):
        """Test that style tags are properly removed."""
        html = '<html><head><style>body { color: red; }</style></head><body>Hello</body></html>'
        result = _extract_text(html)
        assert "Hello" in result
        assert "color: red" not in result

    def test_script_with_space_before_closing(self):
        """Test script tags with space before closing bracket (CodeQL edge case)."""
        # This is the edge case that CodeQL warns about - regexp doesn't match </script >
        html = '<html><body><p>Hello</p><script >alert("xss")</script ><p>World</p></body></html>'
        result = _extract_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "xss" not in result
        assert "alert" not in result

    def test_script_with_gt_in_attribute(self):
        """Test script tags with > character in attributes."""
        html = '<html><body><p>Hello</p><script data="foo>bar">alert("xss")</script><p>World</p></body></html>'
        result = _extract_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "xss" not in result

    def test_multiple_scripts_and_styles(self):
        """Test multiple script and style tags."""
        html = '''<html>
            <head>
                <style>body { color: red; }</style>
                <script>var x = 1;</script>
            </head>
            <body>
                <h1>Title</h1>
                <script>alert("xss1")</script>
                <p>Content</p>
                <style>p { font-size: 12px; }</style>
                <script>alert("xss2")</script>
            </body>
        </html>'''
        result = _extract_text(html)
        assert "Title" in result
        assert "Content" in result
        assert "color: red" not in result
        assert "var x = 1" not in result
        assert "xss1" not in result
        assert "font-size" not in result
        assert "xss2" not in result

    def test_malformed_html(self):
        """Test handling of malformed HTML."""
        html = '<html><body><p>Hello</p><script>unclosed<script>more</body></html>'
        result = _extract_text(html)
        # Should handle gracefully without crashing
        assert isinstance(result, str)
        assert "Hello" in result

    def test_html_entities(self):
        """Test that HTML entities are properly decoded."""
        html = '<html><body><p>Hello &amp; World</p><p>&lt;tag&gt;</p></body></html>'
        result = _extract_text(html)
        assert "Hello & World" in result
        assert "<tag>" in result

    def test_nested_tags(self):
        """Test handling of nested HTML tags."""
        html = '<html><body><div><p>Nested <span>content</span> here</p></div></body></html>'
        result = _extract_text(html)
        assert "Nested content here" in result

    def test_whitespace_normalization(self):
        """Test that whitespace is properly normalized."""
        html = '<html><body><p>Hello</p>   <p>World</p>\n\n<p>Test</p></body></html>'
        result = _extract_text(html)
        # Should normalize excessive whitespace
        assert "Hello" in result
        assert "World" in result
        assert "Test" in result

    def test_empty_html(self):
        """Test handling of empty HTML."""
        result = _extract_text("")
        assert result == ""

    def test_html_only_with_scripts(self):
        """Test HTML that contains only script tags."""
        html = '<html><body><script>alert("xss")</script></body></html>'
        result = _extract_text(html)
        # Should return empty string or whitespace-only string
        assert result.strip() == "" or "xss" not in result

    def test_script_tags_with_various_cases(self):
        """Test that script tags are removed regardless of case."""
        # HTMLParser is case-insensitive by default
        html1 = '<SCRIPT>alert("xss")</SCRIPT><p>Hello</p>'
        result1 = _extract_text(html1)
        assert "Hello" in result1
        assert "xss" not in result1

        html2 = '<Script>alert("xss")</script><p>Hello</p>'
        result2 = _extract_text(html2)
        assert "Hello" in result2
        assert "xss" not in result2

    def test_style_tags_with_various_cases(self):
        """Test that style tags are removed regardless of case."""
        html = '<STYLE>body { color: red; }</style><p>Hello</p>'
        result = _extract_text(html)
        assert "Hello" in result
        assert "color: red" not in result

    def test_complex_real_world_html(self):
        """Test with more complex real-world HTML structure."""
        html = '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Test Page</title>
            <style>
                body { font-family: Arial; }
                .container { max-width: 800px; }
            </style>
            <script src="analytics.js"></script>
            <script>
                (function(i,s,o,g,r,a,m){...})(window,document,'script','ga');
            </script>
        </head>
        <body>
            <div class="container">
                <h1>Welcome to Test Page</h1>
                <p>This is a paragraph with <strong>bold text</strong>.</p>
                <ul>
                    <li>Item 1</li>
                    <li>Item 2</li>
                </ul>
                <script>console.log("debug");</script>
                <p>Final paragraph.</p>
            </div>
        </body>
        </html>
        '''
        result = _extract_text(html)
        assert "Welcome to Test Page" in result
        assert "This is a paragraph with bold text." in result
        assert "Item 1" in result
        assert "Item 2" in result
        assert "Final paragraph." in result
        # Ensure scripts and styles are removed
        assert "font-family: Arial" not in result
        assert "analytics.js" not in result
        assert "console.log" not in result
        assert "max-width: 800px" not in result


class TestFetchURL:
    """Test URL fetching functionality."""

    def test_invalid_url(self, monkeypatch):
        """Test handling of invalid URLs."""
        # Mock the requests library to avoid actual network calls
        def mock_get(url, headers=None, timeout=None):
            class MockResponse:
                def raise_for_status(self):
                    raise Exception("Invalid URL")
            return MockResponse()

        try:
            import requests
            monkeypatch.setattr("requests.get", mock_get)
        except ImportError:
            pass

        result = _fetch_url("not-a-valid-url")
        assert result["success"] is False
        assert "error" in result


class TestBrowsePage:
    """Test browse_page tool."""

    def test_browse_page_with_extraction(self, monkeypatch):
        """Test browse_page with text extraction enabled."""
        # Mock _fetch_url to return sample HTML
        def mock_fetch(url, timeout=30):
            return {
                "success": True,
                "content": "<html><body><h1>Test</h1><p>Content</p><script>alert('xss')</script></body></html>",
                "status_code": 200
            }

        monkeypatch.setattr("dpc_client_core.dpc_agent.tools.browser._fetch_url", mock_fetch)

        from dpc_client_core.dpc_agent.tools.registry import ToolContext
        import pathlib

        # Create a mock ToolContext with correct signature
        ctx = ToolContext(
            agent_root=pathlib.Path("/tmp/test_agent"),
        )

        result = browse_page(ctx, "http://example.com", extract_text=True)
        assert "Test" in result
        assert "Content" in result
        assert "xss" not in result  # Script content should be removed


class TestFetchJSON:
    """Test fetch_json tool."""

    def test_fetch_json_success(self, monkeypatch):
        """Test successful JSON fetching."""
        def mock_fetch(url, timeout=30):
            return {
                "success": True,
                "content": '{"key": "value", "number": 42}',
                "status_code": 200
            }

        monkeypatch.setattr("dpc_client_core.dpc_agent.tools.browser._fetch_url", mock_fetch)

        from dpc_client_core.dpc_agent.tools.registry import ToolContext
        import pathlib

        ctx = ToolContext(
            agent_root=pathlib.Path("/tmp/test_agent"),
        )

        result = fetch_json(ctx, "http://example.com/api")
        assert "key" in result
        assert "value" in result
        assert "42" in result

    def test_fetch_json_invalid_json(self, monkeypatch):
        """Test handling of invalid JSON."""
        def mock_fetch(url, timeout=30):
            return {
                "success": True,
                "content": "not valid json",
                "status_code": 200
            }

        monkeypatch.setattr("dpc_client_core.dpc_agent.tools.browser._fetch_url", mock_fetch)

        from dpc_client_core.dpc_agent.tools.registry import ToolContext
        import pathlib

        ctx = ToolContext(
            agent_root=pathlib.Path("/tmp/test_agent"),
        )

        result = fetch_json(ctx, "http://example.com/api")
        assert "Invalid JSON" in result
