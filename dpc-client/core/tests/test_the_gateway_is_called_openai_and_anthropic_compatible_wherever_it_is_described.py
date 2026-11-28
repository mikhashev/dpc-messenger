"""The gateway is called OpenAI- and Anthropic-compatible wherever it is described.

The card THE-GATEWAY-IS-CALLED-OPENAI-ONLY-IN-LABELS-AND-COMMENTS-THOUGH-IT-ALSO-SERVES-
THE-ANTHROPIC-FORM (DPC Project #118 and #119): the door serves two request forms —
POST /v1/chat/completions (the OpenAI one) and POST /v1/messages (the Anthropic one) —
which the gateway's own route text has always said, while the comments, docstrings,
log lines, CLI help and the two settings forms around it called it "OpenAI-compatible"
alone. The words now follow docs/GLOSSARY.md's fixed phrase, "OpenAI- and
Anthropic-compatible", with the noun adapted to each place. This pins the phrase where
the gateway is described, so a future edit cannot quietly drop one of the two forms.
"""

from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1]
UI_DIR = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "components"

# docs/GLOSSARY.md, "gateway / IDE door": the fixed phrase, hyphen and all.
BOTH = "OpenAI- and Anthropic-compatible"


def source(relative: str) -> str:
    return (CORE_DIR / relative).read_text(encoding="utf-8")


def test_the_settings_seed_comment_and_both_getter_docstrings_say_the_two_forms():
    settings = source("dpc_client_core/settings.py")
    assert f"# The {BOTH} gateway (ADR-041 D1)" in settings
    # The [gateway] enabled key's comment names every route the door serves.
    assert "Serve /v1/models, /v1/chat/completions and /v1/messages to local tools" in settings
    assert f'"""Whether the {BOTH} gateway listens at all' in settings
    assert f'"""Get the {BOTH} gateway port."""' in settings


def test_the_service_comment_build_docstring_and_every_gateway_log_line_say_the_two_forms():
    service = source("dpc_client_core/service.py")
    assert f"# The {BOTH} gateway follows the coordinator" in service
    assert f'"""The {BOTH} gateway, or None when' in service
    # _build_gateway and _start_gateway each refuse a start with the same words.
    assert service.count(f'"{BOTH} gateway refused to start: %s"') == 2
    assert f'"{BOTH} gateway could not listen on %s:%d: %s"' in service


def test_the_gateway_server_docstring_stop_log_and_route_text_say_the_two_forms():
    gateway = source("dpc_client_core/gateway.py")
    assert f'"""The {BOTH} HTTP surface over one `Gateway`."""' in gateway
    assert f'"{BOTH} gateway stopped"' in gateway
    # The truth the phrase rests on: the door's own text names both request forms.
    assert "OpenAI /v1/chat/completions and Anthropic /v1/messages" in gateway
    assert "(the OpenAI form) and POST /v1/messages (the Anthropic one)" in gateway


def test_the_cli_help_names_the_two_forms():
    assert f'help="the {BOTH} gateway (IDE door)"' in source("dpc_client_core/cli.py")


def test_the_two_settings_forms_label_the_door_with_the_two_forms():
    network = (UI_DIR / "NetworkSettingsEditor.svelte").read_text(encoding="utf-8")
    sharing = (UI_DIR / "InferenceSharingEditor.svelte").read_text(encoding="utf-8")
    assert f"Gateway ({BOTH})" in network
    assert f"An {BOTH} listener for the clients on this machine" in sharing
    # The IDE door's "why" block points at the form that holds the switch and the port.
    assert "The switch and the port are set in the Network and Gateway form" in sharing
