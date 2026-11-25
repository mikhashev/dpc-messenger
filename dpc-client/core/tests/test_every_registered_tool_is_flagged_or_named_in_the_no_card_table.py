"""ADR-053 step 2: every registered tool asks the owner on a foreign request, or is named
here as a tool with no card. A new tool fails this test until someone decides (acceptance
check 1). Also: the flag survives override_handler (D-h), and every flagged call on a
foreign turn has the card's wait in its budget (D-c).
"""
import pathlib

import pytest

from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, Asker
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry

# ADR-053 Decision 2.5, Mike's list (#105-#128), his answer to Q3 (#138), the plain web
# fetchers (#148), and Zcode's review (browse_page with the owner's login, list_auth_domains).
FLAGGED = {
    "run_shell", "write_file", "repo_delete",
    "git_add", "git_commit", "git_init", "git_checkout", "git_merge", "git_tag", "git_reset",
    "git_snapshot", "git_push",
    "schedule_task", "send_user_message",
    "browser_navigate", "browser_click", "browser_fill", "browser_select", "browser_download",
    "browse_page", "fetch_json", "check_url", "search_web", "list_auth_domains",
    "music_capabilities", "music_propose_job", "music_check", "music_wait", "music_cancel_job",
    "music_list_takes", "music_list_library", "music_get_lyrics", "music_playlist_add",
    "comfyui_submit", "comfyui_check", "comfyui_wait", "comfyui_queue_status",
    "comfyui_progress", "comfyui_convert", "comfyui_diagnose",
    "chat_post_file",
    "execute_skill", "import_skill_from_agent", "register_task_type", "unregister_task_type",
    "describe_image", "transcribe_audio_file",
}

# No card: reads, self-introspection, browser reads, the L5 writes the room already closes
# (ADR-048), and kill_my_process (Q3).
NO_CARD = {
    "read_file", "list_dir", "list_extended_sandbox_paths", "search_files", "search_in_file",
    "read_document", "chat_history", "knowledge_list", "memory_search", "get_dpc_context",
    "read_session_archive", "read_session_detail", "search_session_archives",
    "git_status", "git_diff", "git_log", "git_branch",
    "list_my_tools", "list_my_skills", "list_agent_skills", "list_local_agents",
    "list_task_types", "get_task_status", "get_task_board", "card_status", "list_my_processes",
    "browser_snapshot", "browser_screenshot", "browser_extract", "browser_wait_for",
    "browser_scroll", "browser_collect", "browser_switch_tab", "browser_close",
    "update_scratchpad", "update_identity", "deduplicate_identity",
    "kill_my_process",
}

# Their own timeout already holds the 60 s card (D-c).
BUDGET_HOLDS_APPROVAL = {"run_shell", "schedule_task", "music_propose_job"}


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    reg = ToolRegistry(agent_root=tmp_path_factory.mktemp("agent_reg"))
    assert reg.load_failures == {}, "a module that failed to load hides its tools from this test"
    return reg


def test_every_tool_is_in_exactly_one_column(registry):
    names = set(registry.available_tools())
    assert FLAGGED.isdisjoint(NO_CARD)
    undecided = names - FLAGGED - NO_CARD
    assert not undecided, (
        f"tools neither flagged owner_approval_for_others nor named in NO_CARD: {sorted(undecided)}; "
        "decide each (ADR-053 2.5) and add it to one of the two sets")
    assert not (FLAGGED | NO_CARD) - names, "a name here is no longer registered"


def test_the_flag_is_on_exactly_the_flagged_column(registry):
    assert registry.owner_approval_names() == FLAGGED


def test_no_tool_is_owner_only_any_more(registry):
    assert registry.owner_only_names() == set()


def test_override_handler_keeps_every_flag(registry):
    before = registry._entries["write_file"]
    registry.override_handler("write_file", lambda ctx, **kw: "wrapped")
    after = registry._entries["write_file"]
    try:
        assert after.owner_approval_for_others and after.approval_check is before.approval_check
        assert after.timeout_sec == before.timeout_sec
        assert after.budget_holds_approval == before.budget_holds_approval
    finally:
        registry._entries["write_file"] = before


def test_every_flagged_call_on_a_foreign_turn_has_the_cards_wait_in_its_budget(registry, tmp_path):
    friend = ToolContext(agent_root=tmp_path, message_source="group", turn_class="other_node",
                         asker=Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", False, True))
    owner = ToolContext(agent_root=tmp_path, message_source="ui", turn_class="owner",
                        asker=Asker(ASKER_HUMAN, "Mike", "dpc-node-this", True, True))
    for name in sorted(FLAGGED):
        entry = registry._entries[name]
        own = registry.get_timeout(name)
        total = own + registry.approval_wait_for(name, friend)
        if name in BUDGET_HOLDS_APPROVAL:
            assert entry.budget_holds_approval, name
            assert own >= owner_approval.APPROVAL_TTL_SECONDS, name
            assert total == own, name
        else:
            assert total == own + owner_approval.APPROVAL_TTL_SECONDS, name
        assert registry.approval_wait_for(name, owner) == 0, name
    for name in NO_CARD:
        assert registry.approval_wait_for(name, friend) == 0, name


def test_the_music_budgets_d_c_names(registry):
    assert registry.get_timeout("music_wait") == 915
    assert registry.get_timeout("music_propose_job") == 100
    for name in ("music_capabilities", "music_check", "music_cancel_job", "music_list_takes",
                 "music_list_library", "music_get_lyrics", "music_playlist_add"):
        assert registry.get_timeout(name) == 30, name


def test_the_loop_adds_the_wait_to_a_flagged_calls_clock():
    source = pathlib.Path(__file__).resolve().parents[1] / "dpc_client_core" / "dpc_agent" / "loop.py"
    text = source.read_text(encoding="utf-8")
    assert 'getattr(tools, "approval_wait_for", None)' in text
    assert "run_with_call_deadline(" in text


def test_browse_page_asks_with_the_owners_login_and_as_a_plain_fetch(registry, tmp_path):
    from dpc_client_core.dpc_agent.owner_approval import Ask
    from dpc_client_core.dpc_agent.tools import browser
    check = registry._entries["browse_page"].approval_check
    ctx = ToolContext(agent_root=tmp_path, message_source="group", turn_class="other_node",
                      asker=Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", False, True))
    with_login = check(ctx, {"url": "https://shop.example", "use_auth": "shop.example"})
    assert isinstance(with_login, Ask) and "logged in as the owner" in with_login.details
    # Mike, AI Studio group #148: a plain fetch sends a model-chosen URL out; it asks too.
    assert browser.CARD_FOR_PLAIN_WEB_FETCH is True
    assert isinstance(check(ctx, {"url": "https://example.com"}), Ask)
