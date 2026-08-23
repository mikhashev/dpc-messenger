"""Two caps stack on one tool result, and the outer one used to delete the
inner one's notice.

Measured 2026-08-23: `git log --stat -n 120` produced 699 370 chars,
`run_shell` capped it at 50 000 and appended `... (truncated, 699370 total
chars)`, then `loop._truncate_tool_result` cut that string at 15 000 — taking
the appended notice with it — and announced `50,036 bytes total`. The agent
was told a number 14x below the truth, with confidence, which is worse than
being told nothing. These tests go red if either half regresses.
"""

from dpc_client_core.dpc_agent.loop import _truncate_tool_result
from dpc_client_core.dpc_agent.tools.shell import MAX_OUTPUT, _cap_stream


def _oversized(chars: int) -> str:
    """A stream long enough to trip both caps, on line boundaries."""
    line = "x" * 79 + "\n"
    return line * (chars // len(line) + 1)


class TestTheToolsNoticeSurvivesTheHarnessCap:
    def test_the_notice_is_a_prefix_not_a_suffix(self):
        capped = _cap_stream(_oversized(200_000), "stdout")
        assert capped.startswith("[stdout: ")

    def test_the_true_size_reaches_the_model_through_both_caps(self):
        raw = _oversized(200_000)
        seen = _truncate_tool_result(_cap_stream(raw, "stdout"))
        # The number the command actually produced, not what either cap kept.
        assert str(len(raw)) in seen

    def test_the_notice_names_a_way_to_read_the_rest_and_its_price(self):
        capped = _cap_stream(_oversized(200_000), "stdout")
        head = capped.split("\n", 1)[0]
        assert "read_file(" in head
        assert "EXECUTES THE COMMAND AGAIN" in head

    def test_the_notice_says_kept_not_shown(self):
        # Two caps are in play and each shows a different amount; a prefix
        # that says "showing 50000" is contradicted by the harness marker
        # below it, which shows far less. The tool kept 50 000 — what the
        # model is shown is the harness marker's business, not this one's.
        head = _cap_stream(_oversized(200_000), "stdout").split("\n", 1)[0]
        assert "kept first" in head
        assert "showing" not in head

    def test_the_notice_names_where_the_redirect_lands(self):
        head = _cap_stream(_oversized(200_000), "stdout").split("\n", 1)[0]
        assert "cwd" in head

    def test_the_hint_keeps_the_form_cmd_exe_and_sh_share(self):
        # This guards the chosen string against regressing to PowerShell
        # syntax (`*>` works in neither cmd.exe nor sh). It is NOT evidence
        # that /bin/sh or zsh were run — as of 2026-08-23 nobody has run
        # them; the form is verified live on Windows/cmd.exe only.
        head = _cap_stream(_oversized(200_000), "stdout").split("\n", 1)[0]
        assert "> out.txt 2> err.txt" in head
        assert "*>" not in head

    def test_a_stream_under_the_cap_is_returned_untouched(self):
        small = "one line\n"
        assert _cap_stream(small, "stdout") == small
        assert _cap_stream("y" * MAX_OUTPUT, "stderr") == "y" * MAX_OUTPUT


class TestTheHarnessMarkerNoLongerClaimsATotal:
    def test_it_does_not_call_what_it_received_a_total(self):
        marker = _truncate_tool_result(_oversized(200_000))[15000:]
        assert "total" not in marker.lower()

    def test_it_counts_characters_and_says_characters(self):
        # len() of a str is characters; 61 200 Cyrillic chars are 114 000
        # UTF-8 bytes, so the old "bytes" label was wrong by 1.86x there.
        cyrillic = "строка с кириллицей\n" * 1200
        marker = _truncate_tool_result(cyrillic)[15000:]
        assert "bytes" not in marker
        assert f"{len(cyrillic):,} chars" in marker

    def test_it_no_longer_offers_search_files_for_every_tool(self):
        # search_files is advice about a repository; a shell stream or a JSON
        # body from the network has no section for it to locate.
        marker = _truncate_tool_result(_oversized(200_000))[15000:]
        assert "search_files" not in marker

    def test_a_result_under_the_cap_is_returned_untouched(self):
        assert _truncate_tool_result("short") == "short"
