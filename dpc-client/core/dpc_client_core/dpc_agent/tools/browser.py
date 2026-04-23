"""
DPC Agent — Browser Tools.

Provides web browsing capabilities for the embedded agent:
- Web page fetching and parsing
- Text extraction from URLs
- Basic web search integration

Note: Full browser automation (Playwright/Selenium) is not included
to keep dependencies minimal. These tools use simple HTTP requests.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import re
import ssl
from html.parser import HTMLParser
from io import StringIO
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# Try to import requests, fall back to urllib
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    HAS_REQUESTS = False


def _fetch_url(url: str, timeout: int = 30) -> Dict[str, Any]:
    """
    Fetch content from a URL.

    Args:
        url: URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Dict with success, content, and error fields
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        # Prefer markdown/plain text — servers that support content negotiation
        # (GitHub, Reddit, many CMS) return clean content, cutting token usage 77–86%.
        # Falls back to HTML for servers that ignore the header.
        "Accept": "text/markdown, text/plain, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        if HAS_REQUESTS:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            content = response.text
            return {
                "success": True,
                "content": content,
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type", ""),
            }
        else:
            req = urllib.request.Request(url, headers=headers)
            # Create SSL context with system certificates for proper TLS verification
            ssl_context = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
                content = response.read().decode("utf-8", errors="replace")
                return {
                    "success": True,
                    "content": content,
                    "status_code": 200,
                    "content_type": response.headers.get("Content-Type", ""),
                }

    except Exception as e:
        return {"success": False, "error": str(e)}


class _TextExtractor(HTMLParser):
    """HTML parser that extracts text content, skipping script/style tags.

    Uses HTMLParser instead of regexps for robustness against malformed HTML
    and edge cases that regexp-based filtering misses (e.g., spaces before
    closing tags, attributes containing '>', malformed HTML).
    """

    def __init__(self):
        super().__init__()
        self.text = StringIO()
        self.skip_tags = {'script', 'style'}
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        """Track when we enter a script or style tag."""
        if tag in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        """Track when we exit a script or style tag."""
        if tag in self.skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        """Collect text data, skipping script/style content."""
        if self.skip_depth == 0:
            self.text.write(data)

    def get_text(self) -> str:
        """Get the extracted and cleaned text."""
        text = self.text.getvalue()

        # Clean up whitespace
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\n\s*\n", "\n\n", text)

        return text.strip()


def _extract_text(html: str) -> str:
    """
    Extract readable text from HTML.

    Uses HTMLParser instead of regexps for robustness against malformed HTML
    and edge cases that regexp-based filtering misses. This addresses CodeQL
    warning py/bad-tag-filter about regexp-based HTML tag filtering.

    Args:
        html: HTML content

    Returns:
        Extracted text with script/style content removed
    """
    import html as html_module

    # Use HTMLParser to extract text, skipping script/style tags
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    text = parser.get_text()

    # Decode HTML entities
    text = html_module.unescape(text)

    return text


_SIZE_PRESETS = {
    "s": 5000,
    "m": 10000,
    "l": 25000,
    "f": None,
}


def _browse_sync(url: str) -> Dict[str, Any]:
    result = _fetch_url(url)
    if not result["success"]:
        return result

    content = result["content"]
    content_type = result.get("content_type", "")
    is_clean_text = any(ct in content_type for ct in ("text/markdown", "text/plain"))

    if is_clean_text:
        text = content
    else:
        try:
            import trafilatura
            text = trafilatura.extract(
                content,
                output_format="markdown",
                include_formatting=True,
                include_links=True,
                include_tables=True,
                favor_recall=True,
            )
        except Exception:
            text = None

        if not text:
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    extracts = list(ddgs.extract([url]))
                    if extracts and extracts[0].get("content"):
                        text = extracts[0]["content"]
            except Exception:
                pass

        if not text:
            text = _extract_text(content)

    result["text"] = text
    return result


async def browse_page(ctx: ToolContext, url: str, size: str = "m") -> str:
    """
    Fetch a web page and extract content as structured markdown.

    Uses trafilatura for high-quality extraction that preserves headings,
    lists, tables, and links. Falls back to basic text extraction if
    trafilatura fails.

    Size presets control output length:
      s = 5K chars (quick summary)
      m = 10K chars (default)
      l = 25K chars (deep reading)
      f = full content (no truncation)

    Args:
        ctx: Tool context (unused, but required for tool interface)
        url: URL to fetch
        size: Size preset (s/m/l/f)

    Returns:
        Page content as markdown
    """
    result = await asyncio.to_thread(_browse_sync, url)

    if not result.get("success", False) and "error" in result:
        return f"⚠️ Failed to fetch page: {result['error']}"

    text = result.get("text", "")
    max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
    total = len(text)
    if max_chars and total > max_chars:
        text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"

    return f"Content from {url} (markdown, {total} chars):\n\n{text}"


def fetch_json(ctx: ToolContext, url: str) -> str:
    """
    Fetch JSON data from a URL.

    Args:
        ctx: Tool context (unused)
        url: URL to fetch

    Returns:
        JSON content formatted for reading
    """
    import json

    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Failed to fetch JSON: {result['error']}"

    try:
        data = json.loads(result["content"])
        formatted = json.dumps(data, indent=2, ensure_ascii=False)

        if len(formatted) > 10000:
            formatted = formatted[:10000] + f"\n\n... (truncated)"

        return f"JSON from {url}:\n\n{formatted}"

    except json.JSONDecodeError as e:
        return f"⚠️ Invalid JSON: {e}"



def check_url(ctx: ToolContext, url: str) -> str:
    """
    Check if a URL is accessible.

    Args:
        ctx: Tool context (unused)
        url: URL to check

    Returns:
        Status information
    """
    import time

    start = time.time()
    result = _fetch_url(url, timeout=10)
    elapsed = time.time() - start

    if result["success"]:
        content_length = len(result.get("content", ""))
        return f"✓ URL accessible: {url}\n" \
               f"  Status: {result.get('status_code', 'OK')}\n" \
               f"  Response time: {elapsed:.2f}s\n" \
               f"  Content size: {content_length} bytes"
    else:
        return f"✗ URL not accessible: {url}\n" \
               f"  Error: {result['error']}\n" \
               f"  Time: {elapsed:.2f}s"


def _search_ddgs_sync(query: str, max_results: int, backend: str) -> list:
    from ddgs import DDGS
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=backend))


async def search_web_ddgs(ctx: ToolContext, query: str, max_results: int = 5, backend: str = "auto") -> str:
    """
    Search the web using multiple engines via ddgs (no API key required).

    Supports 8+ backends: duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia.
    Returns title + URL + snippet for each result, reducing need to browse each page.

    Args:
        ctx: Tool context (unused)
        query: Search query
        max_results: Maximum number of results (1-20)
        backend: Search backend ("auto", "duckduckgo", "bing", "brave", "google", "yandex", "mojeek", "yahoo", "wikipedia")

    Returns:
        Search results with snippets
    """
    max_results = max(1, min(max_results, 20))

    try:
        results = await asyncio.to_thread(_search_ddgs_sync, query, max_results, backend)
    except ImportError:
        return "⚠️ ddgs package not installed. Run: pip install ddgs"
    except Exception as e:
        return f"⚠️ Search failed: {e}"

    if not results:
        return f"No results found for: {query}"

    output_lines = [f"Search results for '{query}' ({len(results)} found, backend={backend}):\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", "")
        snippet = r.get("body", "")
        output_lines.append(f"  {i}. {title}\n     {url}\n     {snippet}")

    return "\n\n".join(output_lines)


def get_tools() -> List[ToolEntry]:
    """Export browser tools for registry."""
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": "Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch"
                        },
                        "size": {
                            "type": "string",
                            "description": "Output size preset: s (5K summary), m (10K default), l (25K deep), f (full)",
                            "default": "m",
                            "enum": ["s", "m", "l", "f"]
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=browse_page,
            timeout_sec=60,
        ),

        ToolEntry(
            name="fetch_json",
            schema={
                "name": "fetch_json",
                "description": "Fetch JSON data from a URL API endpoint",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch JSON from"
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=fetch_json,
            timeout_sec=30,
        ),


        ToolEntry(
            name="check_url",
            schema={
                "name": "check_url",
                "description": "Check if a URL is accessible and measure response time",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to check"
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=check_url,
            timeout_sec=15,
        ),

        ToolEntry(
            name="search_web",
            schema={
                "name": "search_web",
                "description": "Search the web using multiple engines (duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia). Returns title + URL + snippet for each result. Use backend='auto' for automatic fallback across engines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results",
                            "default": 5,
                            "minimum": 1,
                            "maximum": 20
                        },
                        "backend": {
                            "type": "string",
                            "description": "Search backend: auto, duckduckgo, bing, brave, google, yandex, mojeek, yahoo, wikipedia",
                            "default": "auto"
                        }
                    },
                    "required": ["query"]
                }
            },
            handler=search_web_ddgs,
            timeout_sec=30,
        ),
    ]
