"""A snapshot names a secret field; it never prints its value.

The snapshot reaches a model provider, so every character the walk prints
leaves the machine. The rule lives in JS, so this runs the real walk in a real
browser: a hand-written node dict cannot say what the browser does with
`type=hidden`, nor what `el.value` holds for a file input.
"""

import json
import re
from typing import NamedTuple

import pytest

from dpc_client_core.dpc_agent.tools.browser import (
    _A11Y_DOM_SNAPSHOT_JS,
    _build_a11y_tree,
)


_FORM_HTML = """<!doctype html>
<html><body>
  <form>
    <input type="text" aria-label="Username" value="alice@example.com">
    <input type="password" aria-label="Password" value="hunter2">
    <input type="text" autocomplete="one-time-code" aria-label="Code"
           value="314159">
    <input type="text" autocomplete="section-pay billing cc-number"
           aria-label="Card" value="4111111111111111">
    <input type="file" aria-label="Attachment">
    <input type="hidden" name="csrf" value="csrf-token-zzz"
           aria-label="Hidden field for the form ticket">
    <textarea aria-label="Notes">notes stay visible</textarea>
    <select aria-label="Country">
      <option value="country-nl" selected>Netherlands</option>
    </select>
    <select aria-label="Expiry month" autocomplete="cc-exp-month">
      <option value="exp-month-06">June</option>
      <option value="exp-month-07" selected>July</option>
    </select>
    <select aria-label="Saved card" autocomplete="cc-number">
      <option value="card-visa-4021" selected>Visa 1234</option>
      <option value="card-mc-5533">Mastercard 5678</option>
      <option value="card-amex-9077">Amex 9012</option>
    </select>
    <select aria-label="Backup card" autocomplete="cc-number">
      <option value="">Choose a card</option>
      <option value="card-backup-7712">Discover 3456</option>
      <option value="card-backup-8823">Diners 7890</option>
    </select>
    <select aria-label="Grouped card" autocomplete="cc-number">
      <optgroup label="Personal cards">
        <option value="grp-visa-1188" selected>Visa 1111</option>
        <option value="grp-visa-2299">Visa 2222</option>
      </optgroup>
      <optgroup label="Work cards">
        <option value="grp-amex-3377">Amex 3333</option>
      </optgroup>
    </select>
    <textarea aria-label="Second factor"
              autocomplete="one-time-code">otp-828171</textarea>
    <input type="password" aria-label="Unfilled password">
    <input type="text" autocomplete="one-time-code" aria-label="Unfilled code">
  </form>
</body></html>
"""

_UPLOAD_NAME = "secret-plan.txt"

# The accessible name of the `type=hidden` input. Printable, and free of the
# word "csrf" on purpose — see the hidden-input test.
_HIDDEN_FIELD_NAME = "Hidden field for the form ticket"

# The `value` attribute of every option belonging to a select the predicate
# withholds. Part of `_SECRETS` rather than a list beside it, so the sweep
# over the whole page cannot fall behind this one.
_WITHHELD_OPTION_VALUES = (
    "exp-month-06",
    "exp-month-07",
    "card-visa-4021",
    "card-mc-5533",
    "card-amex-9077",
    "card-backup-7712",
    "card-backup-8823",
    "grp-visa-1188",
    "grp-visa-2299",
    "grp-amex-3377",
)

_SECRETS = (
    "hunter2",
    "314159",
    "4111111111111111",
    "csrf-token-zzz",
    "otp-828171",
    _UPLOAD_NAME,
) + _WITHHELD_OPTION_VALUES

# The visible text of every option belonging to a select the predicate
# withholds. On a card picker that text *is* the secret — the last four
# digits — so it is held to the same rule as a value, and a placeholder
# option is in the list because printing it would say which cards are not
# chosen.
_WITHHELD_OPTION_TEXTS = (
    "June",
    "July",
    "Visa 1234",
    "Mastercard 5678",
    "Amex 9012",
    "Choose a card",
    "Discover 3456",
    "Diners 7890",
    "Visa 1111",
    "Visa 2222",
    "Amex 3333",
)

# An <optgroup> names a run of options, and on a card picker that name tells
# whose cards they are. Held to the same rule as the options it holds.
_WITHHELD_OPTGROUP_LABELS = (
    "Personal cards",
    "Work cards",
)


@pytest.fixture(scope="module")
def _chromium():
    """A real browser, or a clean skip: playwright is an extra, and an
    installed package still has no binary until `playwright install` ran."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - no browser extra
        pytest.skip(f"playwright not installed: {exc}")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"no chromium binary for playwright: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def _raw_snapshot(_chromium, tmp_path):
    """The walk's own dict for `_FORM_HTML`, before any rendering."""
    upload = tmp_path / _UPLOAD_NAME
    upload.write_text("x", encoding="utf-8")
    page = _chromium.new_page()
    try:
        page.set_content(_FORM_HTML)
        page.set_input_files("input[type=file]", str(upload))
        return page.evaluate(_A11Y_DOM_SNAPSHOT_JS, 1)
    finally:
        page.close()


@pytest.fixture()
def _snapshot(_raw_snapshot):
    """(tree_text, refs) for `_FORM_HTML`, exactly as `a11y_snapshot()`."""
    return _build_a11y_tree(_raw_snapshot)


def _raw_nodes(node: dict) -> list[dict]:
    """Every node of the walk's own dict, depth first."""
    found = [node]
    for child in node.get("children") or []:
        found.extend(_raw_nodes(child))
    return found


def _raw_node(root: dict, name: str) -> dict:
    """The single node of the walk's dict whose accessible name is `name`."""
    matches = [n for n in _raw_nodes(root) if n.get("name") == name]
    assert len(matches) == 1, f"expected one {name} node, got {len(matches)}"
    return matches[0]


def _one_line(tree: str, name: str) -> str:
    """The single rendered line of the *control* called `name`.

    Selected by its ref: a `<label>` or a `<span>` naming a field prints a
    line of its own carrying the same text, and only the control gets a ref.
    """
    lines = [
        ln for ln in tree.split("\n")
        if f'"{name}"' in ln and re.search(r"\[@e\d+\]", ln)
    ]
    assert len(lines) == 1, f"expected one {name} line, got {lines}\n{tree}"
    return lines[0]


def test_no_secret_field_value_reaches_the_rendered_snapshot(_snapshot):
    tree, _refs = _snapshot
    leaked = [s for s in _SECRETS if s in tree]
    assert not leaked, f"secret values printed into the snapshot: {leaked}\n{tree}"


def test_a_non_secret_value_is_still_printed(_snapshot):
    tree, _refs = _snapshot
    assert "alice@example.com" in tree
    assert "notes stay visible" in tree
    assert "Netherlands" in tree


def test_an_ordinary_select_and_textarea_keep_printing_their_values(_snapshot):
    tree, _refs = _snapshot
    country = _one_line(tree, "Country")
    assert re.fullmatch(
        r'\s*- combobox "Country" = "country-nl" \[@e\d+\]', country
    ), country
    notes = _one_line(tree, "Notes")
    assert '= "notes stay visible"' in notes, notes


def test_the_password_field_keeps_its_role_name_and_ref(_snapshot):
    tree, refs = _snapshot
    lines = [ln for ln in tree.split("\n") if '"Password"' in ln]
    assert len(lines) == 1, f"expected one Password line, got {lines}\n{tree}"
    assert re.fullmatch(
        r'\s*- textbox "Password" = \[withheld\] \[@e\d+\]', lines[0]
    ), lines[0]
    ref = re.search(r"@e\d+", lines[0]).group(0)
    assert refs[ref]["role"] == "textbox"
    assert refs[ref]["name"] == "Password"


def test_a_secret_marked_only_by_autocomplete_is_withheld(_snapshot):
    tree, _refs = _snapshot
    for name in ("Code", "Card"):
        lines = [ln for ln in tree.split("\n") if f'"{name}"' in ln]
        assert len(lines) == 1, f"expected one {name} line, got {lines}"
        assert "[withheld]" in lines[0], lines[0]


def test_a_select_marked_secret_by_autocomplete_is_withheld(_snapshot):
    tree, refs = _snapshot
    line = _one_line(tree, "Expiry month")
    assert re.fullmatch(
        r'\s*- combobox "Expiry month" = \[withheld\] \(2 options\) \[@e\d+\]',
        line,
    ), line
    ref = re.search(r"@e\d+", line).group(0)
    assert refs[ref]["role"] == "combobox"
    assert refs[ref]["name"] == "Expiry month"
    # An option is a node of its own and is named by its text, so the month
    # names used to print underneath a line that had just promised nothing.
    # The count replaces them: the agent learns there is a choice to make
    # without learning what was chosen.
    assert "July" not in tree, tree
    assert "June" not in tree, tree


def test_a_withheld_card_select_prints_no_option_text_and_no_option_value(
    _snapshot,
):
    """The reason the option children had to go.

    On a card picker the option texts are the last four digits, so a parent
    promising `[withheld]` above three readable cards withheld nothing.
    """
    tree, refs = _snapshot
    line = _one_line(tree, "Saved card")
    assert re.fullmatch(
        r'\s*- combobox "Saved card" = \[withheld\] \(3 options\) \[@e\d+\]',
        line,
    ), line
    ref = re.search(r"@e\d+", line).group(0)
    assert refs[ref]["role"] == "combobox"
    assert refs[ref]["name"] == "Saved card"
    for text in ("Visa 1234", "Mastercard 5678", "Amex 9012"):
        assert text not in tree, tree
    for value in ("card-visa-4021", "card-mc-5533", "card-amex-9077"):
        assert value not in tree, tree


def test_an_empty_valued_secret_select_counts_its_options_without_the_marker(
    _snapshot,
):
    """Nothing is chosen, and the line says so by omitting `[withheld]` —
    that marker means "filled". The options stay hidden all the same: a
    card picker lists the same cards before anyone touches it."""
    tree, _refs = _snapshot
    line = _one_line(tree, "Backup card")
    assert re.fullmatch(
        r'\s*- combobox "Backup card" \(3 options\) \[@e\d+\]', line
    ), line
    for text in ("Choose a card", "Discover 3456", "Diners 7890"):
        assert text not in tree, tree


def test_a_withheld_select_counts_its_options_across_optgroups(_snapshot):
    """An `<optgroup>` puts a level between the select and its options, so a
    count read off the select's own children would say two here, not three —
    and would say one for a picker that groups all its cards."""
    tree, _refs = _snapshot
    line = _one_line(tree, "Grouped card")
    assert re.fullmatch(
        r'\s*- combobox "Grouped card" = \[withheld\] \(3 options\) \[@e\d+\]',
        line,
    ), line
    for text in ("Visa 1111", "Visa 2222", "Amex 3333"):
        assert text not in tree, tree
    for label in _WITHHELD_OPTGROUP_LABELS:
        assert label not in tree, tree


def test_no_option_of_a_withheld_select_reaches_the_snapshot(_snapshot):
    """One sweep, the counterpart of the value sweep above."""
    tree, _refs = _snapshot
    leaked = [
        t for t in _WITHHELD_OPTION_TEXTS + _WITHHELD_OPTGROUP_LABELS
        if t in tree
    ]
    assert not leaked, f"option texts printed into the snapshot: {leaked}\n{tree}"


def test_the_walk_itself_hands_over_no_option_of_a_withheld_select(
    _raw_snapshot,
):
    """The guard belongs to the walk, not only to the renderer.

    This dict is what every consumer other than `_build_a11y_tree` sees — a
    later renderer, a cache, a debug dump, a summarizer — so the options must
    never be collected, rather than be collected and then dropped on the way
    to the text.
    """
    for name in ("Expiry month", "Saved card", "Backup card", "Grouped card"):
        node = _raw_node(_raw_snapshot, name)
        assert node["optionsWithheld"] is True, name
        assert node["children"] == [], (name, node["children"])
    dumped = json.dumps(_raw_snapshot, ensure_ascii=False)
    leaked = [
        s for s in (
            _WITHHELD_OPTION_TEXTS + _WITHHELD_OPTGROUP_LABELS
            + _WITHHELD_OPTION_VALUES
        )
        if s in dumped
    ]
    assert not leaked, f"options carried by the walk: {leaked}"


def test_a_secret_textarea_is_withheld_and_keeps_its_ref(_snapshot):
    tree, refs = _snapshot
    line = _one_line(tree, "Second factor")
    assert re.fullmatch(
        r'\s*- textbox "Second factor" = \[withheld\] \[@e\d+\]', line
    ), line
    ref = re.search(r"@e\d+", line).group(0)
    assert refs[ref]["role"] == "textbox"
    assert refs[ref]["name"] == "Second factor"


def test_an_empty_secret_field_prints_neither_a_value_nor_the_marker(_snapshot):
    """The marker is the only thing telling a filled secret from an empty
    one, so an untouched field must not claim to hold something."""
    tree, _refs = _snapshot
    for name in ("Unfilled password", "Unfilled code"):
        line = _one_line(tree, name)
        assert '= "' not in line, line
        assert "[withheld]" not in line, line


def test_a_chosen_file_name_is_withheld(_snapshot):
    tree, _refs = _snapshot
    lines = [ln for ln in tree.split("\n") if '"Attachment"' in ln]
    assert len(lines) == 1, f"expected one Attachment line, got {lines}"
    assert "[withheld]" in lines[0], lines[0]


def test_a_hidden_input_never_reaches_the_tree(_snapshot):
    """Settles the open `type=hidden` question: `display: none` for
    `input[type=hidden]` is in the rendering section of the HTML standard,
    not in one engine's stylesheet, so `isHidden` drops the node before any
    value is read wherever the snapshot runs.

    The field carries a printable accessible name, and that name is what is
    asserted absent. Watching only for the value would pass for the wrong
    reason: were `isHidden` to stop dropping the node, the type allowlist
    would still keep the value back and the row would arrive as a named
    `[withheld]` textbox. Only `isHidden` can keep the name out.
    """
    tree, _refs = _snapshot
    assert _HIDDEN_FIELD_NAME not in tree, tree
    assert "csrf" not in tree.lower()


# ---------------------------------------------------------------------------
# The channels that are not `autocomplete`.
#
# Two tables, two pages, never mixed: the closed table lists fields the walk
# must refuse to print, the open table lists fields it must keep printing.
# Each is meant to be read as "field -> expectation", so a later edit to the
# JS pattern lands here as a diff in a row rather than as a silent change of
# behaviour. Every row carries a value of its own so a failure names the row.


class _Row(NamedTuple):
    """One field of a fixture page.

    `name` is what `getName()` makes of the markup — the accessible name the
    snapshot prints — and is used to find the row's single rendered line.
    `value` is the row's distinctive value.
    """

    case: str
    html: str
    name: str
    value: str
    needs_css_mask: bool = False


# A field is secret because of what it is called, whatever tag carries it.
_CLOSED_ROWS: tuple[_Row, ...] = (
    _Row("name-otp",
         '<input type="text" title="c01" name="otp" value="closed-otp-01">',
         "c01", "closed-otp-01"),
    _Row("name-otp-code",
         '<input type="text" title="c02" name="otp_code" value="closed-otp-code-02">',
         "c02", "closed-otp-code-02"),
    _Row("name-user-pin",
         '<input type="text" title="c03" name="user_pin" value="closed-user-pin-03">',
         "c03", "closed-user-pin-03"),
    _Row("name-sms-otp",
         '<input type="text" title="c04" name="sms_otp" value="closed-sms-otp-04">',
         "c04", "closed-sms-otp-04"),
    _Row("name-cc-number",
         '<input type="text" title="c05" name="cc-number" value="closed-cc-number-05">',
         "c05", "closed-cc-number-05"),
    _Row("name-ccnum",
         '<input type="text" title="c06" name="ccnum" value="closed-ccnum-06">',
         "c06", "closed-ccnum-06"),
    _Row("name-Password-capitalised",
         '<input type="text" title="c07" name="Password" value="closed-password-07">',
         "c07", "closed-password-07"),
    _Row("id-OTP-capitals",
         '<input type="text" title="c08" id="OTP" value="closed-id-otp-08">',
         "c08", "closed-id-otp-08"),
    _Row("aria-label-security-code",
         '<input type="text" aria-label="Security code" value="closed-aria-09">',
         "Security code", "closed-aria-09"),
    _Row("placeholder-only",
         '<input type="text" name="code_input" placeholder="Enter OTP code"'
         ' value="closed-placeholder-10">',
         "Enter OTP code", "closed-placeholder-10"),
    # The masking pair: a harmless placeholder over a telling name. Reading
    # one attribute and stopping would print this one.
    _Row("name-otp-under-placeholder-search",
         '<input type="text" name="otp" placeholder="Search" value="closed-masked-11">',
         "Search", "closed-masked-11"),
    _Row("textarea-name-otp",
         '<textarea title="c12" name="otp">closed-textarea-12</textarea>',
         "c12", "closed-textarea-12"),
    # The option text deliberately contains the row value, so the sweep over
    # the whole page covers the option's text as well as the select's value:
    # a select caught by its name must hide both.
    _Row("select-name-cc-number",
         '<select title="c13" name="cc-number">'
         '<option value="closed-select-13" selected>closed-select-13-chosen'
         '</option></select>',
         "c13", "closed-select-13"),
    _Row("search-type-name-otp",
         '<input type="search" title="c14" name="otp" value="closed-search-14">',
         "c14", "closed-search-14"),
    # No attribute says "secret" here; the page itself draws dots.
    _Row("css-webkit-text-security",
         '<input type="text" title="c15" style="-webkit-text-security: disc"'
         ' value="closed-css-15">',
         "c15", "closed-css-15", True),
    # A digit on the right of the token. Forms number serial fields, and a
    # boundary that refuses a digit on that side prints every one of them.
    _Row("name-otp1",
         '<input type="text" title="c16" name="otp1" value="closed-otp1-16">',
         "c16", "closed-otp1-16"),
    _Row("name-otp2",
         '<input type="text" title="c17" name="otp2" value="closed-otp2-17">',
         "c17", "closed-otp2-17"),
    _Row("name-otp4",
         '<input type="text" title="c18" name="otp4" value="closed-otp4-18">',
         "c18", "closed-otp4-18"),
    _Row("name-cvv2",
         '<input type="text" title="c19" name="cvv2" value="closed-cvv2-19">',
         "c19", "closed-cvv2-19"),
    _Row("name-pin2",
         '<input type="text" title="c20" name="pin2" value="closed-pin2-20">',
         "c20", "closed-pin2-20"),
    _Row("name-csc2",
         '<input type="text" title="c21" name="csc2" value="closed-csc2-21">',
         "c21", "closed-csc2-21"),
    _Row("name-ssn1",
         '<input type="text" title="c22" name="ssn1" value="closed-ssn1-22">',
         "c22", "closed-ssn1-22"),
    _Row("name-tan1",
         '<input type="text" title="c23" name="tan1" value="closed-tan1-23">',
         "c23", "closed-tan1-23"),
    _Row("name-pwd",
         '<input type="text" title="c24" name="pwd" value="closed-pwd-24">',
         "c24", "closed-pwd-24"),
    _Row("name-verify-code",
         '<input type="text" title="c25" name="verify_code"'
         ' value="closed-verify-code-25">',
         "c25", "closed-verify-code-25"),
    # The remaining rows are named through channels no attribute of the
    # element carries: the row is found by the name the snapshot resolves.
    _Row("title-cvc",
         '<input type="text" title="CVC" value="closed-title-cvc-26">',
         "CVC", "closed-title-cvc-26"),
    _Row("aria-labelledby-one-time-code",
         '<span id="otc-label-27">One-time code</span>'
         '<input type="text" aria-labelledby="otc-label-27"'
         ' value="closed-labelledby-27">',
         "One-time code", "closed-labelledby-27"),
    # A <label for>, and the id it points at says nothing on its own: only
    # the label text is telling.
    _Row("label-for-one-time-code",
         '<label for="field-28">One-time code</label>'
         '<input type="text" id="field-28" title="c28"'
         ' value="closed-label-for-28">',
         "c28", "closed-label-for-28"),
    _Row("wrapping-label-security-code",
         '<label>Security code '
         '<input type="text" title="c29" value="closed-wrapping-29"></label>',
         "c29", "closed-wrapping-29"),
)

# Boundaries. None of these is a secret, and each value must survive the walk.
_OPEN_ROWS: tuple[_Row, ...] = (
    _Row("name-cardholder",
         '<input type="text" title="o01" name="cardholder" value="open-cardholder-01">',
         "o01", "open-cardholder-01"),
    _Row("name-username",
         '<input type="text" title="o02" name="username" value="open-username-02">',
         "o02", "open-username-02"),
    _Row("name-shipping-contains-pin",
         '<input type="text" title="o03" name="shipping" value="open-shipping-03">',
         "o03", "open-shipping-03"),
    _Row("name-opinion-contains-pin",
         '<input type="text" title="o04" name="opinion" value="open-opinion-04">',
         "o04", "open-opinion-04"),
    _Row("name-notes",
         '<input type="text" title="o05" name="notes" value="open-notes-05">',
         "o05", "open-notes-05"),
    _Row("name-description",
         '<input type="text" title="o06" name="description" value="open-description-06">',
         "o06", "open-description-06"),
    _Row("name-passenger-contains-pass",
         '<input type="text" title="o07" name="passenger" value="open-passenger-07">',
         "o07", "open-passenger-07"),
    _Row("name-spinner-contains-pin",
         '<input type="text" title="o08" name="spinner" value="open-spinner-08">',
         "o08", "open-spinner-08"),
    _Row("search-box-unmarked",
         '<input type="search" title="o09" value="open-search-09">',
         "o09", "open-search-09"),
    # An unnamed contenteditable is not a form control at all: the walk has
    # no `value` to weigh and prints the text as generic content. Documented
    # here as the boundary it is.
    _Row("contenteditable-div",
         '<div contenteditable="true">open-editable-10</div>',
         "open-editable-10", "open-editable-10"),
    # A letter on either side of a token is an ordinary word, not a field.
    _Row("name-pinball",
         '<input type="text" title="o11" name="pinball" value="open-pinball-11">',
         "o11", "open-pinball-11"),
    _Row("name-tango",
         '<input type="text" title="o12" name="tango" value="open-tango-12">',
         "o12", "open-tango-12"),
    _Row("name-sultan",
         '<input type="text" title="o13" name="sultan" value="open-sultan-13">',
         "o13", "open-sultan-13"),
    # The label channel reads a label, it does not assume one is secret.
    _Row("label-for-shipping-address",
         '<label for="ship-14">Shipping address</label>'
         '<input type="text" id="ship-14" title="o14"'
         ' value="open-shipping-address-14">',
         "o14", "open-shipping-address-14"),
    _Row("title-notes",
         '<input type="text" title="Notes" value="open-title-notes-15">',
         "Notes", "open-title-notes-15"),
    # Two boundaries chosen, not overlooked. The left lookbehind still
    # refuses a digit, so a token numbered on its left stays open; and a
    # letter before `2fa` makes it part of a longer word. Both print, and
    # the cost of closing them would be every id that ends in a digit.
    _Row("name-h1pin-digit-before-the-token",
         '<input type="text" title="o16" name="h1pin" value="open-h1pin-16">',
         "o16", "open-h1pin-16"),
    _Row("name-sms2fa-letter-before-the-token",
         '<input type="text" title="o17" name="sms2fa" value="open-sms2fa-17">',
         "o17", "open-sms2fa-17"),
)

_EMPTY_SECRET_HTML = '<input type="text" title="c99" name="otp">'

_CSS_MASK_PROBE_JS = (
    "() => typeof window.getComputedStyle(document.body).webkitTextSecurity"
    " === 'string'"
)


def _page_html(rows: tuple[_Row, ...], extra: str = "") -> str:
    body = "\n    ".join(row.html for row in rows)
    return (
        "<!doctype html>\n<html><body>\n  <form>\n    "
        + body + "\n    " + extra + "\n  </form>\n</body></html>\n"
    )


def _render(browser, html: str) -> tuple[str, dict, bool]:
    page = browser.new_page()
    try:
        page.set_content(html)
        css_mask = bool(page.evaluate(_CSS_MASK_PROBE_JS))
        raw = page.evaluate(_A11Y_DOM_SNAPSHOT_JS, 1)
    finally:
        page.close()
    tree, refs = _build_a11y_tree(raw)
    return tree, refs, css_mask


@pytest.fixture(scope="module")
def _closed_snapshot(_chromium):
    """(tree, refs, css_mask_supported) for the closed table."""
    return _render(
        _chromium, _page_html(_CLOSED_ROWS, _EMPTY_SECRET_HTML)
    )


@pytest.fixture(scope="module")
def _open_snapshot(_chromium):
    """(tree, refs, css_mask_supported) for the open table."""
    return _render(_chromium, _page_html(_OPEN_ROWS))


@pytest.mark.parametrize("row", _CLOSED_ROWS, ids=[r.case for r in _CLOSED_ROWS])
def test_a_field_named_as_a_secret_is_withheld_and_keeps_its_ref(
    _closed_snapshot, row: _Row,
):
    tree, _refs, css_mask = _closed_snapshot
    if row.needs_css_mask and not css_mask:
        pytest.skip("engine reports no -webkit-text-security property")
    assert row.value not in tree, f"{row.case}: value printed\n{tree}"
    line = _one_line(tree, row.name)
    assert "[withheld]" in line, f"{row.case}: {line}"
    assert re.search(r"\[@e\d+\]", line), f"{row.case}: no ref\n{line}"


def test_no_value_from_the_closed_table_reaches_the_tree(_closed_snapshot):
    """One sweep over the whole page: a value must not surface through some
    other node either — a label, an option, a title."""
    tree, _refs, css_mask = _closed_snapshot
    expected = [
        row.value for row in _CLOSED_ROWS
        if css_mask or not row.needs_css_mask
    ]
    leaked = [v for v in expected if v in tree]
    assert not leaked, f"secret values printed into the snapshot: {leaked}\n{tree}"


@pytest.mark.parametrize("row", _OPEN_ROWS, ids=[r.case for r in _OPEN_ROWS])
def test_an_ordinary_field_next_to_the_boundary_still_prints_its_value(
    _open_snapshot, row: _Row,
):
    """The green half of the contract. None of these fields is a secret, so
    widening the pattern until one of them is caught turns this red: the
    snapshot would go quiet about ordinary form content and the agent would
    lose the page it is working on.

    Two rows here are open by decision rather than by accident: `h1pin`
    (a digit sits before the token, and the left boundary refuses one, so
    that every id ending in a digit keeps printing) and `sms2fa` (a letter
    before `2fa` makes it one word). Closing either would cost more than
    it buys; they are recorded so the next reader knows they were weighed.
    """
    tree, _refs, _css_mask = _open_snapshot
    assert row.value in tree, f"{row.case}: value missing\n{tree}"


def test_a_secret_select_of_the_closed_table_hides_its_single_option(
    _closed_snapshot,
):
    """The name channel, not `autocomplete`, and one option — the singular
    of the count is rendered here rather than left to a reader's guess."""
    tree, _refs, _css_mask = _closed_snapshot
    line = _one_line(tree, "c13")
    assert re.fullmatch(
        r'\s*- combobox "c13" = \[withheld\] \(1 option\) \[@e\d+\]', line
    ), line
    assert "closed-select-13-chosen" not in tree, tree


def test_an_empty_field_caught_by_its_name_prints_neither_value_nor_marker(
    _closed_snapshot,
):
    tree, _refs, _css_mask = _closed_snapshot
    line = _one_line(tree, "c99")
    assert '= "' not in line, line
    assert "[withheld]" not in line, line


def test_the_renderer_prints_the_marker_without_a_browser():
    filled = {
        "role": "textbox", "name": "Password", "value": "",
        "withheld": True, "children": [], "el": "1:0",
    }
    empty = dict(filled, withheld=False)
    assert "= [withheld]" in _build_a11y_tree(filled)[0]
    assert "=" not in _build_a11y_tree(empty)[0]


def test_the_renderer_withholds_even_when_a_value_arrives_beside_the_flag():
    """The marker wins over a value on the same node.

    The walk never sets both today, so this is not a bug report about the
    JS: it is where the invariant stops living in one layer. A renderer
    that reads the value first would print whatever some later change,
    another producer of these dicts, or a merge left in the field.
    """
    node = {
        "role": "textbox", "name": "Password", "value": "leak",
        "withheld": True, "children": [], "el": "1:0",
    }
    tree, _refs = _build_a11y_tree(node)
    assert "leak" not in tree, tree
    assert "= [withheld]" in tree, tree


def test_the_renderer_drops_option_children_of_a_withheld_select():
    """The same invariant, one layer down, for the options.

    The walk never hands these children over today. The flag is read before
    the children all the same, so that a later change, another producer of
    these dicts or a merge cannot turn a promise of `[withheld]` back into a
    list of cards.
    """
    node = {
        "role": "combobox", "name": "Card", "value": "",
        "withheld": True, "optionsWithheld": True, "optionCount": 2,
        "children": [
            {"role": "option", "name": "Visa 1234", "value": "",
             "withheld": False, "children": [], "el": "1:1"},
            {"role": "option", "name": "Amex 9012", "value": "",
             "withheld": False, "children": [], "el": "1:2"},
        ],
        "el": "1:0",
    }
    tree, refs = _build_a11y_tree(node)
    assert "Visa 1234" not in tree, tree
    assert "Amex 9012" not in tree, tree
    assert re.fullmatch(
        r'- combobox "Card" = \[withheld\] \(2 options\) \[@e1\]', tree
    ), tree
    assert list(refs) == ["@e1"]


# Every row of both tables is labelled by a short `title`, and since the
# resolved name became a channel of its own, that label is itself tested
# against the pattern. The scheme holds only while no label reads as a
# secret, so it is checked rather than assumed.
_ROW_LABELS: tuple[str, ...] = tuple(
    row.name for row in _CLOSED_ROWS + _OPEN_ROWS
    if re.fullmatch(r"[co]\d+", row.name)
) + ("c99",)


def test_no_row_label_of_the_tables_is_itself_read_as_a_secret(_chromium):
    """The labelling scheme, under test rather than on trust.

    A label containing `otp`, `pin` or `tan` as a bounded token, or
    `token`/`secret` anywhere, would withhold its own row and every
    closed-table assertion would pass without proving anything.
    """
    rows = tuple(
        _Row(
            f"label-{label}",
            f'<input type="text" title="{label}" value="label-probe-{label}">',
            label,
            f"label-probe-{label}",
        )
        for label in _ROW_LABELS
    )
    tree, _refs, _css_mask = _render(_chromium, _page_html(rows))
    caught = [row.name for row in rows if row.value not in tree]
    assert not caught, f"row labels read as secret: {caught}\n{tree}"


_OVERWITHHELD_ROWS: tuple[_Row, ...] = (
    _Row("name-secretary",
         '<input type="text" title="w01" name="secretary"'
         ' value="over-secretary-01">',
         "w01", "over-secretary-01"),
)


def test_a_word_that_merely_contains_an_unbounded_token_is_withheld(_chromium):
    """Over-withholding, deliberately, in the one direction that is cheap.

    `secret` is matched unbounded, so `secretary` is withheld although no
    secret is in it. The cost is a quiet field; the cost of bounding
    `secret` would be `secret_answer`, `clientsecret`, `secretkey` and
    every other run-together spelling going out in the snapshot. Recorded
    here so that the next reader sees a decision and not a defect, and
    does not "repair" it into a leak.
    """
    tree, _refs, _css_mask = _render(_chromium, _page_html(_OVERWITHHELD_ROWS))
    row = _OVERWITHHELD_ROWS[0]
    assert row.value not in tree, tree
    assert "[withheld]" in _one_line(tree, row.name), tree
