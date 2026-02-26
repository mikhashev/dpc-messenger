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

import logging
import re
import ssl
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        if HAS_REQUESTS:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            content = response.text
        else:
            req = urllib.request.Request(url, headers=headers)
            # Create SSL context with system certificates for proper TLS verification
            ssl_context = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
                content = response.read().decode("utf-8", errors="replace")

        return {
            "success": True,
            "content": content,
            "status_code": 200 if not HAS_REQUESTS else response.status_code,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def _extract_text(html: str) -> str:
    """
    Extract readable text from HTML.

    Args:
        html: HTML content

    Returns:
        Extracted text
    """
    # Remove script and style elements
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Decode HTML entities
    import html as html_module
    text = html_module.unescape(text)

    # Clean up whitespace
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)

    return text.strip()


def browse_page(ctx: ToolContext, url: str, extract_text: bool = True) -> str:
    """
    Fetch and optionally parse a web page.

    Args:
        ctx: Tool context (unused, but required for tool interface)
        url: URL to fetch
        extract_text: Whether to extract text from HTML

    Returns:
        Page content or extracted text
    """
    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Failed to fetch page: {result['error']}"

    content = result["content"]

    if extract_text:
        text = _extract_text(content)
        # Truncate if too long
        if len(text) > 10000:
            text = text[:10000] + f"\n\n... (truncated, {len(text)} total chars)"
        return f"Content from {url}:\n\n{text}"
    else:
        if len(content) > 15000:
            content = content[:15000] + f"\n\n... (truncated, {len(content)} total chars)"
        return f"Raw HTML from {url}:\n\n{content}"


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


def extract_links(ctx: ToolContext, url: str) -> str:
    """
    Extract all links from a web page.

    Args:
        ctx: Tool context (unused)
        url: URL to fetch

    Returns:
        List of links found on the page
    """
    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Failed to fetch page: {result['error']}"

    html = result["content"]

    # Extract links
    link_pattern = r'<a[^>]+href=["\']([^"\']+)["\']'
    links = re.findall(link_pattern, html, re.IGNORECASE)

    # Filter and deduplicate
    seen = set()
    unique_links = []
    for link in links:
        # Skip anchors and javascript
        if link.startswith(("#", "javascript:", "mailto:")):
            continue
        if link not in seen:
            seen.add(link)
            unique_links.append(link)

    if not unique_links:
        return f"No links found on {url}"

    # Format output
    output_lines = [f"Links from {url} ({len(unique_links)} found):\n"]
    for i, link in enumerate(unique_links[:50], 1):
        output_lines.append(f"  {i}. {link}")

    if len(unique_links) > 50:
        output_lines.append(f"\n  ... and {len(unique_links) - 50} more")

    return "\n".join(output_lines)


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


def search_duckduckgo(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo (no API key required).

    Args:
        ctx: Tool context (unused)
        query: Search query
        max_results: Maximum number of results

    Returns:
        Search results
    """
    # Use DuckDuckGo HTML version for scraping
    url = f"https://html.duckduckgo.com/html/?q={query}"

    result = _fetch_url(url)

    if not result["success"]:
        return f"⚠️ Search failed: {result['error']}"

    html = result["content"]

    # Extract search results from DDG HTML
    results = []

    # DDG result pattern
    result_pattern = r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>'
    matches = re.findall(result_pattern, html, re.IGNORECASE)

    for href, title in matches[:max_results]:
        # Clean up title
        title = re.sub(r"<[^>]+>", "", title).strip()
        results.append(f"  • {title}\n    {href}")

    if not results:
        return f"No results found for: {query}"

    return f"Search results for '{query}':\n\n" + "\n\n".join(results)


def get_tools() -> List[ToolEntry]:
    """Export browser tools for registry."""
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": "Fetch and parse a web page to extract text content",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch"
                        },
                        "extract_text": {
                            "type": "boolean",
                            "description": "Extract text from HTML (true) or return raw HTML (false)",
                            "default": True
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
            name="extract_links",
            schema={
                "name": "extract_links",
                "description": "Extract all links from a web page",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to extract links from"
                        }
                    },
                    "required": ["url"]
                }
            },
            handler=extract_links,
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
                "description": "Search the web using DuckDuckGo (no API key required)",
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
                        }
                    },
                    "required": ["query"]
                }
            },
            handler=search_duckduckgo,
            timeout_sec=30,
        ),
    ]
