"""Agent Markdown reaches Telegram rendered, not as raw `**`, `#` and backticks.

The DPC chat renders the agent's Markdown; Telegram received the same text
either with no parse mode or run through `escape_markdown`, so the user saw
every marker. `telegram_format` turns the Markdown into Telegram's HTML
subset, splits it under the 4096 limit with every chunk's tags balanced, and
sends a chunk Telegram refuses again as plain text.
"""

import re
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from dpc_client_core.telegram_format import (
    TELEGRAM_MESSAGE_MAX_LENGTH,
    _balanced,
    _tg_len,
    html_to_plain,
    markdown_to_telegram_html,
    send_rendered,
    split_telegram_html,
    strip_markdown,
)

md = markdown_to_telegram_html


# --- inline -----------------------------------------------------------------


@pytest.mark.parametrize("source, rendered", [
    ("**bold**", "<b>bold</b>"),
    ("__bold__", "<b>bold</b>"),
    ("*italic*", "<i>italic</i>"),
    ("_italic_", "<i>italic</i>"),
    ("~~gone~~", "<s>gone</s>"),
    ("***both***", "<b><i>both</i></b>"),
    ("`x = 1`", "<code>x = 1</code>"),
    ("``a ` b``", "<code>a ` b</code>"),
])
def test_inline_markers_become_tags(source, rendered):
    assert md(source) == rendered


def test_ordinary_text_is_escaped():
    assert md("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"


def test_code_is_escaped_and_not_formatted():
    assert md("`<b>**x**</b>`") == "<code>&lt;b&gt;**x**&lt;/b&gt;</code>"


def test_snake_case_and_arithmetic_stay_text():
    assert md("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4"


def test_a_windows_path_keeps_its_backslashes():
    assert md(r"C:\Users\mikha\.dpc") == r"C:\Users\mikha\.dpc"


def test_backslash_escaped_marker_is_literal():
    assert md(r"\*not italic\*") == "*not italic*"


def test_link_becomes_anchor_with_escaped_href():
    assert md("[docs](https://ex.com/?a=1&b=2)") == '<a href="https://ex.com/?a=1&amp;b=2">docs</a>'


def test_link_with_unsafe_scheme_is_text():
    assert md("[x](javascript:alert(1))").startswith("x (javascript:")
    assert "<a" not in md("[x](javascript:alert(1))")


def test_overlapping_markers_never_produce_misnested_tags():
    out = md("**a *b** c*")
    assert _balanced(out)
    assert "<b>a *b</b>" in out


# --- blocks -----------------------------------------------------------------


def test_heading_becomes_a_bold_line():
    assert md("## Plan **now** *soon*") == "<b>Plan now <i>soon</i></b>"
    assert md("# Title ##") == "<b>Title</b>"


def test_bullets_and_numbers_become_plain_lines():
    out = md("- one\n* two\n  - nested\n1. first\n2) second\n- [x] done\n- [ ] todo")
    assert out.split("\n") == [
        "• one", "• two", "  • nested", "1. first", "2. second", "☑ done", "☐ todo",
    ]


def test_fenced_code_block_keeps_language_and_escapes():
    out = md("```python\nif a < b:\n    print('&')\n```")
    assert out == (
        '<pre><code class="language-python">if a &lt; b:\n    print(\'&amp;\')</code></pre>'
    )


def test_fence_without_language_and_unclosed_fence():
    assert md("```\nx\n```") == "<pre>x</pre>"
    assert md("~~~\n**not bold**") == "<pre>**not bold**</pre>"


def test_markdown_inside_a_fence_is_left_alone():
    assert md("```\n# not a heading\n- not a bullet\n```") == "<pre># not a heading\n- not a bullet</pre>"


def test_blockquote():
    assert md("> said **this**\n> and that") == "<blockquote>said <b>this</b>\nand that</blockquote>"


def test_table_becomes_an_aligned_grid_in_pre():
    out = md("| name | n |\n|:---|---:|\n| **ark** | 1 |\n| cc | 22 |")
    assert out.startswith("<pre>") and out.endswith("</pre>")
    lines = html_to_plain(out).split("\n")
    assert lines[0] == "name | n"
    assert lines[1] == "-----+---"
    assert lines[2] == "ark  | 1"   # markers stripped inside the grid
    assert lines[3] == "cc   | 22"


def test_horizontal_rule_and_blank_lines():
    assert md("a\n\n\n\n---\nb") == "a\n\n──────────\nb"


def test_output_is_always_balanced_on_a_mixed_message():
    text = (
        "# Report\n**Done:** 3 of *4* ~~tasks~~\n> note `x<y`\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n```js\nlet a = b && c;\n```\n"
        "- [link](https://e.com) *half **open\n"
    )
    assert _balanced(md(text))


def test_strip_markdown_leaves_the_words():
    assert strip_markdown("## Hi **there** `x<1`") == "Hi there x<1"


# --- splitting --------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert split_telegram_html("<b>hi</b>") == ["<b>hi</b>"]


def test_long_text_is_split_under_the_limit_with_balanced_tags():
    html = md(("**bold para** " * 30 + "\n\n") * 30)
    chunks = split_telegram_html(html, TELEGRAM_MESSAGE_MAX_LENGTH)
    assert len(chunks) > 1
    for chunk in chunks:
        assert _tg_len(chunk) <= TELEGRAM_MESSAGE_MAX_LENGTH
        assert _balanced(chunk)
    # Nothing lost: the words survive the cuts.
    joined = " ".join(html_to_plain(c) for c in chunks)
    assert joined.count("bold para") == 900


def test_a_code_block_cut_in_two_is_reopened_with_its_language():
    html = md("```python\n" + "line = 1\n" * 1000 + "```")
    chunks = split_telegram_html(html, 1000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith('<pre><code class="language-python">'), chunk[:50]
        assert chunk.endswith("</code></pre>")
        assert _tg_len(chunk) <= 1000


def test_a_cut_prefers_a_line_break():
    html = "\n".join(f"line {n} " + "w " * 10 for n in range(100))
    for chunk in split_telegram_html(html, 300):
        assert chunk.startswith("line "), chunk[:20]


def test_a_word_longer_than_a_message_is_cut_but_nothing_is_lost():
    word = "x" * 2500
    chunks = split_telegram_html(f"<b>{word}</b>", 1000)
    assert all(_balanced(c) and _tg_len(c) <= 1000 for c in chunks)
    assert "".join(html_to_plain(c) for c in chunks) == word


def test_no_cut_falls_inside_an_entity():
    html = "&amp;" * 500
    for chunk in split_telegram_html(html, 101):
        assert not re.search(r"&[a-z]*$", chunk)
        assert html_to_plain(chunk) == "&" * (len(chunk) // 5)


def test_emoji_counts_twice():
    assert _tg_len("😀") == 2


# --- sending ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_uses_html_parse_mode():
    send = AsyncMock()
    await send_rendered(send, "**hi** `x`")
    send.assert_awaited_once_with("<b>hi</b> <code>x</code>", "HTML")


@pytest.mark.asyncio
async def test_a_refused_parse_is_resent_as_plain_text():
    send = AsyncMock(side_effect=[BadRequest("Can't parse entities: unsupported start tag"), "ok"])
    results = await send_rendered(send, "**hi** a < b")
    assert results == ["ok"]
    assert send.await_args_list[1].args == ("hi a < b", None)


@pytest.mark.asyncio
async def test_a_network_error_is_not_mistaken_for_a_parse_error():
    send = AsyncMock(side_effect=NetworkError("down"))
    with pytest.raises(NetworkError):
        await send_rendered(send, "hi")
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_long_messages_go_in_labelled_parts():
    send = AsyncMock()
    await send_rendered(send, ("word " * 50 + "\n") * 100)
    texts = [c.args[0] for c in send.await_args_list]
    assert len(texts) > 1
    assert texts[0].startswith(f"<i>[1/{len(texts)}]</i>\n")
    assert all(_tg_len(t) <= TELEGRAM_MESSAGE_MAX_LENGTH for t in texts)


@pytest.mark.asyncio
async def test_empty_text_sends_nothing():
    send = AsyncMock()
    assert await send_rendered(send, "   \n") == []
    send.assert_not_awaited()
