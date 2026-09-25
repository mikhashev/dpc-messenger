"""Markdown from the agent, rendered for Telegram.

Agents write ordinary Markdown, which the DPC chat renders. Telegram does not:
sent as plain text or through `escape_markdown` it shows every `**`, `#` and
backtick. This module turns that Markdown into the HTML subset Telegram's
`parse_mode="HTML"` accepts, splits it under the message limit without cutting
a tag in half, and sends it with a plain-text fallback so a message Telegram
refuses to parse is still delivered.

HTML rather than MarkdownV2: MarkdownV2 needs eighteen characters escaped
everywhere outside entities, and one miss rejects the whole message; HTML needs
three (`<`, `>`, `&`).

Written by hand rather than on a Markdown library: none is in the lock, and a
library would still need a renderer for Telegram's tag subset, which is most of
the work. The block grammar here is the one models actually produce.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Awaitable, Callable, List, Optional

log = logging.getLogger(__name__)

TELEGRAM_MESSAGE_MAX_LENGTH = 4096

# ---------------------------------------------------------------------------
# Inline
# ---------------------------------------------------------------------------

_PH = "\x00{}\x00"
_PH_RE = re.compile(r"\x00(\d+)\x00")

_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.S)
_LINK = re.compile(r"\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_AUTOLINK = re.compile(r"<((?:https?|tg|mailto):[^>\s]+)>")
_SAFE_SCHEME = re.compile(r"^(?:https?://|tg://|mailto:)", re.I)

_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC = re.compile(
    r"(?<![\w*\\])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"
    r"|(?<![\w_\\])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"
)
# Only the markers this renderer acts on: a Windows path (`C:\Users\x\.dpc`)
# keeps its backslashes.
_BACKSLASH_ESCAPE = re.compile(r"\\([`*_\[\]#|~])")

_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")


def _balanced(fragment: str) -> bool:
    """True when every tag in the fragment closes in the order it opened."""
    stack: List[str] = []
    for closing, name, _attrs in _TAG.findall(fragment):
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _inline(text: str) -> str:
    """Render one line (or one table cell) of inline Markdown as Telegram HTML."""
    held: List[str] = []

    def hold(rendered: str) -> str:
        held.append(rendered)
        return _PH.format(len(held) - 1)

    text = text.replace("\x00", "")
    text = _CODE_SPAN.sub(lambda m: hold(f"<code>{html.escape(m.group(2).strip() or m.group(2), quote=False)}</code>"), text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not _SAFE_SCHEME.match(url):
            return hold(html.escape(f"{label} ({url})", quote=False))
        return hold(f'<a href="{html.escape(url, quote=True)}">{_emphasis(html.escape(label, quote=False))}</a>')

    text = _LINK.sub(link, text)
    text = _AUTOLINK.sub(lambda m: hold(f'<a href="{html.escape(m.group(1), quote=True)}">{html.escape(m.group(1), quote=False)}</a>'), text)
    text = _BACKSLASH_ESCAPE.sub(lambda m: hold(html.escape(m.group(1), quote=False)), text)

    out = _emphasis(html.escape(text, quote=False))
    while _PH_RE.search(out):
        out = _PH_RE.sub(lambda m: held[int(m.group(1))], out)
    return out


def _emphasis(escaped: str) -> str:
    steps = (
        (_BOLD_ITALIC, lambda m: f"<b><i>{m.group(1)}</i></b>"),
        (_BOLD, lambda m: f"<b>{m.group(1) or m.group(2)}</b>"),
        (_STRIKE, lambda m: f"<s>{m.group(1)}</s>"),
        (_ITALIC, lambda m: f"<i>{m.group(1) or m.group(2)}</i>"),
    )
    rendered = escaped
    for pattern, tag in steps:
        candidate = pattern.sub(tag, rendered)
        # Markers that overlap (`**a *b** c*`) would nest wrongly and Telegram
        # rejects the message; such a step is dropped and its markers stay text.
        if _balanced(candidate):
            rendered = candidate
    return rendered


def _strip_inline(text: str) -> str:
    """Plain text of an inline Markdown fragment (for table cells in <pre>)."""
    text = _CODE_SPAN.sub(lambda m: m.group(2), text)
    text = _LINK.sub(lambda m: m.group(1), text)
    for pattern in (_BOLD_ITALIC, _BOLD, _STRIKE, _ITALIC):
        text = pattern.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    return _BACKSLASH_ESCAPE.sub(r"\1", text)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.\-]*)[^\n]*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _table_cells(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _render_table(rows: List[List[str]]) -> str:
    """Telegram has no tables; a monospaced grid in <pre> keeps the columns."""
    rows = [[_strip_inline(c) for c in row] for row in rows]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(width)]
    lines = []
    for n, row in enumerate(rows):
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if n == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """Render Markdown as the HTML subset Telegram's parse_mode="HTML" accepts."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            marker, lang = fence.group(1), fence.group(2)
            body: List[str] = []
            i += 1
            while i < len(lines):
                closing = lines[i].strip()
                if closing.startswith(marker[0] * len(marker)) and not closing.strip(marker[0]):
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            code = html.escape("\n".join(body), quote=False)
            if lang:
                out.append(f'<pre><code class="language-{html.escape(lang, quote=True)}">{code}</code></pre>')
            else:
                out.append(f"<pre>{code}</pre>")
            continue

        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            rows = [_table_cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_table_cells(lines[i]))
                i += 1
            out.append(_render_table(rows))
            continue

        if _QUOTE.match(line):
            quoted: List[str] = []
            while i < len(lines):
                m = _QUOTE.match(lines[i])
                if not m:
                    break
                quoted.append(m.group(1))
                i += 1
            inner = markdown_to_telegram_html("\n".join(quoted))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        heading = _HEADING.match(line)
        if heading:
            # The whole line is bold already; bold inside it would nest <b> in <b>.
            title = _BOLD.sub(lambda m: m.group(1) or m.group(2), heading.group(1))
            out.append(f"<b>{_inline(title)}</b>")
            i += 1
            continue

        if _HR.match(line):
            out.append("──────────")
            i += 1
            continue

        bullet = _BULLET.match(line)
        if bullet:
            indent, item = bullet.group(1), bullet.group(2)
            task = _TASK.match(item)
            if task:
                mark = "☑" if task.group(1).strip() else "☐"
                out.append(f"{'  ' * (len(indent.expandtabs(4)) // 2)}{mark} {_inline(task.group(2))}")
            else:
                out.append(f"{'  ' * (len(indent.expandtabs(4)) // 2)}• {_inline(item)}")
            i += 1
            continue

        numbered = _NUMBERED.match(line)
        if numbered:
            indent, number, item = numbered.groups()
            out.append(f"{'  ' * (len(indent.expandtabs(4)) // 2)}{number}. {_inline(item)}")
            i += 1
            continue

        out.append(_inline(line.strip()) if line.strip() else "")
        i += 1

    rendered = "\n".join(out)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip("\n")


def html_to_plain(fragment: str) -> str:
    """The text Telegram would have shown, without the markup (fallback path)."""
    return html.unescape(_TAG.sub("", fragment))


def strip_markdown(text: str) -> str:
    """Markdown with its markers taken out, for a plain-text send."""
    return html_to_plain(markdown_to_telegram_html(text))


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def _tg_len(text: str) -> int:
    """Telegram measures in UTF-16 code units; an emoji counts twice."""
    return len(text.encode("utf-16-le")) // 2


_ATOM = re.compile(r"<[^>]*>|&[#\w]+;|\n|[ \t]+|[^<&\s]+|[<&]")


def _after(stack: tuple, atom: str) -> tuple:
    """The open-tag stack after one atom: (name, opening tag as written) pairs."""
    m = _TAG.fullmatch(atom)
    if not m:
        return stack
    if m.group(1):
        return stack[:-1] if stack and stack[-1][0] == m.group(2) else stack
    return stack + ((m.group(2), atom),)


def _openers(stack: tuple) -> str:
    return "".join(opening for _, opening in stack)


def _closers(stack: tuple) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def split_telegram_html(text: str, limit: int = TELEGRAM_MESSAGE_MAX_LENGTH) -> List[str]:
    """Split rendered HTML into messages of at most `limit` UTF-16 units.

    Cuts at the last line break that fits, else the last space, else between
    characters of an over-long word; never inside a tag or an entity. Every
    tag open at a cut is closed at the end of its chunk and reopened, with its
    attributes, at the start of the next.
    """
    if _tg_len(text) <= limit:
        return [text]

    chunks: List[str] = []
    start: tuple = ()          # tags open where the current chunk begins
    buf: List[tuple] = []      # (atom, stack after it)

    def stack_now() -> tuple:
        return buf[-1][1] if buf else start

    def size(extra: str = "", stack: Optional[tuple] = None) -> int:
        body = "".join(a for a, _ in buf) + extra
        return _tg_len(_openers(start) + body + _closers(stack if stack is not None else stack_now()))

    def emit(upto: int) -> None:
        end = buf[upto - 1][1] if upto else start
        body = _openers(start) + "".join(a for a, _ in buf[:upto]) + _closers(end)
        if html_to_plain(body).strip():
            chunks.append(body.strip("\n"))

    def cut() -> None:
        nonlocal start, buf
        j = next((k for k in range(len(buf) - 1, 0, -1) if buf[k][0] == "\n"), None)
        if j is None:
            j = next((k for k in range(len(buf) - 1, 0, -1) if buf[k][0].isspace()), None)
        if j is None:
            emit(len(buf))
            start, buf = stack_now(), []
            return
        emit(j)
        start = buf[j][1]
        buf = buf[j + 1:]
        while buf and buf[0][0] == "\n":
            buf.pop(0)

    for atom in _ATOM.findall(text):
        if not buf and atom == "\n":
            continue
        after = _after(stack_now(), atom)
        if size(atom, after) <= limit:
            buf.append((atom, after))
            continue
        if buf:
            cut()
            if not buf and atom == "\n":
                continue
            after = _after(stack_now(), atom)
            if size(atom, after) <= limit:
                buf.append((atom, after))
                continue
        if _TAG.fullmatch(atom) or atom.startswith("&"):
            buf.append((atom, after))  # cannot be cut; the chunk runs over by one tag
            continue
        # A word longer than a whole message: cut it between characters.
        while atom:
            room = max(1, limit - size())
            piece, atom = atom[:room], atom[room:]
            buf.append((piece, stack_now()))
            if atom:
                emit(len(buf))
                start, buf = stack_now(), []

    if buf:
        emit(len(buf))
    return chunks


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

try:  # python-telegram-bot is a hard dependency; the guard keeps this importable in tests
    from telegram.error import BadRequest as _BadRequest
except ImportError:  # pragma: no cover
    class _BadRequest(Exception):  # type: ignore[no-redef]
        pass


SendFn = Callable[[str, Optional[str]], Awaitable[object]]


async def send_rendered(
    send: SendFn,
    markdown_text: str,
    *,
    limit: int = TELEGRAM_MESSAGE_MAX_LENGTH,
    label_parts: bool = True,
    pause: float = 0.1,
) -> List[object]:
    """Render Markdown and send it as one or more HTML messages.

    `send(text, parse_mode)` is whatever posts one message — `bot.send_message`,
    `update.message.reply_text` — wrapped by the caller. A chunk Telegram
    refuses (BadRequest, e.g. "can't parse entities") is sent again as plain
    text, so a rendering mistake costs the formatting and never the message.
    A network error is not a rendering problem and is raised as it came.
    """
    rendered = markdown_to_telegram_html(markdown_text)
    if not rendered.strip():
        return []
    label_room = 16 if label_parts else 0
    chunks = split_telegram_html(rendered, limit - label_room)
    results: List[object] = []
    for n, chunk in enumerate(chunks, 1):
        if n > 1 and pause:
            await asyncio.sleep(pause)  # consecutive parts, gently on the rate limit
        if label_parts and len(chunks) > 1:
            chunk = f"<i>[{n}/{len(chunks)}]</i>\n{chunk}"
        try:
            results.append(await send(chunk, "HTML"))
        except _BadRequest as e:
            log.warning("Telegram refused the HTML (%s); sending this part as plain text", e)
            results.append(await send(html_to_plain(chunk), None))
    return results
