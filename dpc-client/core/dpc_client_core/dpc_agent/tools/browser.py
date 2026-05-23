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
    result["needs_js"] = not is_clean_text and len(text or "") < 200
    return result


def _browse_with_camoufox(url: str) -> Optional[str]:
    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        return None

    try:
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()

        import trafilatura
        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_formatting=True,
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
        return text
    except Exception as e:
        log.warning(f"Camoufox fallback failed for {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# ADR-028 T4 — AuthBrowser (authenticated read-only Camoufox)
# ─────────────────────────────────────────────────────────────


class AuthRequiredError(Exception):
    """Raised when no cookies exist for the requested domain in the
    agent's vault. Surfaced to the agent as a re-login prompt."""


class AuthExpiredError(Exception):
    """Raised when cookies exist but have expired. Surfaced to the agent
    as a re-login prompt."""


def _to_playwright_cookies(cookies: list[dict]) -> list[dict]:
    """Convert DPC snake_case Cookie dicts → Playwright camelCase format.

    Mirrors the T1 spike `normalize_cookie()` output:
      httponly → httpOnly, samesite → sameSite. expires stays as Unix
      epoch seconds (Playwright accepts numeric). Empty samesite is
      omitted rather than passed as None (Playwright rejects None)."""
    out = []
    for c in cookies:
        pc = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or "",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httponly", False)),
        }
        if c.get("expires") is not None:
            pc["expires"] = c["expires"]
        if c.get("samesite"):
            pc["sameSite"] = c["samesite"]
        out.append(pc)
    return out


def _domain_matches(url: str, etld1: str) -> bool:
    """Check whether URL host is the same eTLD+1 as `etld1` (or a
    subdomain of it). Prevents leaking cookies to unrelated hosts that
    happen to embed the auth-domain string in their URL (path / query
    params / fragments)."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        return False
    host = host.lower()
    etld1 = etld1.lower()
    return host == etld1 or host.endswith("." + etld1)


class AuthBrowser:
    """Restricted Camoufox wrapper for authenticated reads (ADR-028 T4).

    Exposes ONLY navigation + content reading — no `click`, `fill`,
    `evaluate`, or any other interactive method. This is tool-level
    enforcement of READ-only access per ADR-028 Phase 1; WRITE
    permissions are explicitly out of scope until Phase 3.

    Cookies are loaded from the agent's encrypted vault (T3
    `web_auth.py`) at construction time. Two failure modes:

      AuthRequiredError — no cookies for the domain. User must log in
        via the Tauri WebView popup (T2) before this works.
      AuthExpiredError — cookies present but expired. Same fix as
        AuthRequiredError — user re-logs in.

    Use as a context manager so the Camoufox browser is always closed:

        with AuthBrowser(agent_id="agent_001", domain="ozon.ru") as ab:
            ab.goto("https://ozon.ru/my/orders")
            html = ab.get_page_content()
    """

    def __init__(self, agent_id: str, domain: str):
        from dpc_client_core import web_auth

        self._agent_id = agent_id
        self._domain = domain.lower()
        self._etld1 = web_auth.resolve_etld1(self._domain)
        cookies = web_auth.load_cookies(agent_id, domain)
        if cookies is None:
            raise AuthRequiredError(
                f"No cookies for {domain} (agent={agent_id}) — re-login required"
            )
        if web_auth.is_expired(cookies):
            raise AuthExpiredError(
                f"Cookies for {domain} expired (agent={agent_id}) — re-login required"
            )
        self._cookies = cookies
        self._cm = None
        self._browser = None
        self._context = None
        self._page = None

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def domain(self) -> str:
        return self._domain

    def _open(self) -> None:
        from camoufox.sync_api import Camoufox

        self._cm = Camoufox(headless=True)
        self._browser = self._cm.__enter__()
        self._context = self._browser.new_context()
        self._context.add_cookies(_to_playwright_cookies(self._cookies))
        self._page = self._context.new_page()

    def goto(self, url: str) -> None:
        """Navigate to URL. URL eTLD+1 must match the auth domain or be
        a subdomain — otherwise raises ValueError without making any
        network request. This prevents the agent from accidentally (or
        adversarially) leaking auth cookies to an unrelated origin."""
        if self._page is None:
            raise RuntimeError("AuthBrowser not opened — use as context manager")
        if not _domain_matches(url, self._etld1):
            raise ValueError(
                f"URL {url!r} is outside auth domain {self._etld1!r}"
            )
        self._page.goto(url, wait_until="networkidle", timeout=30000)

    def get_page_content(self) -> str:
        """Return the current page as markdown via trafilatura. Same
        extraction pipeline as the anonymous browse path.

        Note: `page.content()` and `trafilatura.extract()` are called
        without explicit timeouts — a hung render or pathological HTML
        could block. The framework-level tool timeout (60s for
        browse_page) is the backstop. Phase 2 may add an explicit
        per-call timeout if observed in practice."""
        if self._page is None:
            raise RuntimeError("AuthBrowser not opened — use as context manager")
        html = self._page.content()
        import trafilatura

        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_formatting=True,
            include_links=True,
            include_tables=True,
            favor_recall=True,
        )
        return text or ""

    def close(self) -> None:
        """Release browser resources. Safe to call multiple times."""
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            finally:
                self._cm = None
                self._browser = None
                self._context = None
                self._page = None


def _auth_browse(agent_id: str, domain: str, url: str) -> str:
    """Sync helper used from the async browse_page via asyncio.to_thread."""
    with AuthBrowser(agent_id=agent_id, domain=domain) as ab:
        ab.goto(url)
        return ab.get_page_content()


async def browse_page(ctx: ToolContext, url: str, size: str = "m", use_auth: Optional[str] = None) -> str:
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
        ctx: Tool context (agent_root used to derive agent_id when use_auth set)
        url: URL to fetch
        size: Size preset (s/m/l/f)
        use_auth: If set, fetch the page authenticated for this domain.
            Routes through restricted AuthBrowser with cookies from the
            agent's encrypted vault (ADR-028). The URL must be within
            the same eTLD+1 as use_auth (subdomains allowed). Returns a
            re-login prompt if cookies are missing or expired.

    Returns:
        Page content as markdown
    """
    if use_auth:
        # Contract: ctx.agent_root is ~/.dpc/agents/{agent_id}/ (see
        # dpc_agent.utils.get_agent_root). Last path component IS the
        # agent_id. If the agent storage layout changes, this derivation
        # must move to a helper there — track via grep on `agent_root.name`.
        agent_id = ctx.agent_root.name

        # ADR-028 T5: per-agent + per-domain auth gate. Reject before
        # opening Camoufox if the agent's web_auth.allowed_domains
        # whitelist doesn't permit this domain, or if no cookies are in
        # the vault. firewall is None in pure-unit-test contexts (no
        # dpc_service wired) — those tests bypass the gate by design.
        firewall = None
        dpc_service = getattr(ctx, "dpc_service", None)
        if dpc_service is not None:
            firewall = getattr(dpc_service, "firewall", None)
        if firewall is not None and not firewall.is_auth_domain_allowed(agent_id, use_auth):
            return (
                f"⚠️ Domain '{use_auth}' is not authorized for agent "
                f"'{agent_id}'. Add it to privacy_rules.json under "
                f"agent_profiles.{agent_id}.web_auth.allowed_domains, "
                f"then re-login via the web-auth UI."
            )

        try:
            text = await asyncio.to_thread(_auth_browse, agent_id, use_auth, url)
        except (AuthRequiredError, AuthExpiredError) as e:
            return f"⚠️ {e}"
        except ValueError as e:
            return f"⚠️ {e}"
        except ImportError:
            return (
                "⚠️ Camoufox browser is not installed. Run "
                "`uv sync --extra browser` in dpc-client/core to enable."
            )
        except (RuntimeError, OSError) as e:
            # Camoufox launch / browser-binary failure / page load timeout
            # — surface to the agent as a single warning rather than a raw
            # stack trace. Common cases: missing browser binary, network
            # timeout (goto's 30s default), page render hang.
            return f"⚠️ Camoufox browser failed: {e}"
        max_chars = _SIZE_PRESETS.get(size, _SIZE_PRESETS["m"])
        total = len(text)
        if max_chars and total > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, {total} total chars, use size='l' or 'f' for more)"
        return f"Content from {url} (markdown, auth={use_auth}, {total} chars):\n\n{text}"

    result = await asyncio.to_thread(_browse_sync, url)

    if not result.get("success", False) and "error" in result:
        return f"⚠️ Failed to fetch page: {result['error']}"

    text = result.get("text", "")

    if result.get("needs_js"):
        js_text = await asyncio.to_thread(_browse_with_camoufox, url)
        if js_text and len(js_text) > len(text or ""):
            text = js_text
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
                "description": "Fetch a web page and extract content as structured markdown. Preserves headings, lists, tables, and links. Use size presets to control output length: s=5K, m=10K (default), l=25K, f=full. Set use_auth=<domain> to fetch authenticated content using stored cookies (requires prior login via the web-auth UI).",
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
                        },
                        "use_auth": {
                            "type": "string",
                            "description": "Optional auth domain (eg 'ozon.ru'). When set, the page is fetched authenticated using cookies from the agent's encrypted vault. The URL must be within the same eTLD+1 as use_auth (subdomains allowed). Returns a re-login prompt if cookies are missing or expired."
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
