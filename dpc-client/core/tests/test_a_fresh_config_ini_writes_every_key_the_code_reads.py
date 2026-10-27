"""A fresh config.ini holds every key the code reads from it.

On the Old PC the embedding model loaded on the card at start and held 1.3 GB
while its owner believed everything ran on the CPU: the setting existed, and a
new install had no line that said so. Keys read through a fallback and never
written are invisible by construction, so this compares the two sets the way
the 2026-08-24 audit did — the literal `get(section, key)` reads by AST across
the package, the written keys by generating a config into a temp home — and a
future getter without a default-config line fails here instead of on a user.

Composed keys (`get_connection_timeout(strategy)` builds `f'{strategy}_timeout'`)
and per-model keys (`[model_downloads]`) have no literal and are not seen; the
first are written already, the second are written only by a decline.
"""

import ast
import configparser
import re
from pathlib import Path

from dpc_client_core.settings import Settings

PACKAGE = Path(__file__).resolve().parents[1] / "dpc_client_core"
RUN_SERVICE = PACKAGE.parent / "run_service.py"

READ_METHODS = {"get", "getboolean", "getint", "getfloat", "has_option"}
# Who reads config.ini: a Settings object under any name, and the two raw
# ConfigParsers that read it before Settings exists (run_service, process.py).
RECEIVER = re.compile(r"settings|_hf_cfg|^parser$", re.I)

# Read but deliberately not written, each with the reason it must stay absent.
LEFT_OUT = {
    # Removed from the template in v0.15.0 (settings.py says so); its getters
    # survive only for the one-time migration in dpc_agent/utils.py.
    ("dpc_agent_telegram", "enabled"),
    ("dpc_agent_telegram", "bot_token"),
    ("dpc_agent_telegram", "allowed_chat_ids"),
    ("dpc_agent_telegram", "event_filter"),
    ("dpc_agent_telegram", "transcription_enabled"),
    # Absence is a third answer here: run_service.py goes offline by itself when
    # every model it needs is cached, and only when the key is NOT set. Writing
    # `false` for a new user would switch that detection off.
    ("hf", "offline_mode"),
}


def _module_constants(tree):
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out[t.id] = node.value.value
            elif isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for n, v in zip(t.elts, node.value.elts):
                    if isinstance(n, ast.Name) and isinstance(v, ast.Constant) and isinstance(v.value, str):
                        out[n.id] = v.value
    return out


def _literal(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _keys_the_code_reads():
    found = {}
    for path in [*PACKAGE.rglob("*.py"), RUN_SERVICE]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)
        in_settings = path == PACKAGE / "settings.py"
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in READ_METHODS or len(node.args) < 2:
                continue
            receiver = ast.unparse(node.func.value)
            if not (RECEIVER.search(receiver) or (in_settings and receiver == "self")):
                continue
            section, key = _literal(node.args[0], consts), _literal(node.args[1], consts)
            if section is None or key is None:
                continue
            found.setdefault((section, key.lower()), []).append(f"{path.name}:{node.lineno}")
    return found


def _keys_a_fresh_install_writes(tmp_path):
    Settings(tmp_path)
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(tmp_path / "config.ini", encoding="utf-8")
    return {(s, k): parser[s][k] for s in parser.sections() for k in parser[s]}


def test_the_scan_sees_the_reads_it_is_meant_to_see():
    """Guards the instrument: a scan that matched nothing would pass below."""
    reads = _keys_the_code_reads()
    assert ("local_transcription", "device") in reads
    assert ("system", "shell_memory_limit_mb") in reads  # via module constants in process.py
    assert ("discord.guardrails", "url_whitelist") in reads  # outside settings.py
    assert ("hf", "offline_mode") in reads
    assert len(reads) > 100


def test_every_key_the_code_reads_is_written_for_a_new_user(tmp_path):
    reads = _keys_the_code_reads()
    written = _keys_a_fresh_install_writes(tmp_path)
    missing = {k: v for k, v in reads.items() if k not in written and k not in LEFT_OUT}
    assert not missing, (
        "read from config.ini but not written by Settings._create_default_config "
        "(add a line with the getter's default and a comment, or a reason to LEFT_OUT): "
        + ", ".join(f"[{s}] {k} ({sites[0]})" for (s, k), sites in sorted(missing.items()))
    )


def test_the_left_out_list_holds_only_keys_that_are_still_read_and_still_absent(tmp_path):
    reads = _keys_the_code_reads()
    written = _keys_a_fresh_install_writes(tmp_path)
    assert all(k in reads for k in LEFT_OUT), "a LEFT_OUT key nothing reads any more"
    assert not any(k in written for k in LEFT_OUT), "a LEFT_OUT key is written after all"


def test_the_background_model_devices_are_written_as_auto(tmp_path):
    written = _keys_a_fresh_install_writes(tmp_path)
    assert written[("memory", "embedding_device")] == "auto"
    assert written[("knowledge_graph", "gliner_device")] == "auto"
    assert written[("knowledge_graph", "backend")] == "sqlite"
    assert written[("local_transcription", "device")] == "auto"


def test_secrets_are_written_empty(tmp_path):
    written = _keys_a_fresh_install_writes(tmp_path)
    for key in [("discord", "bot_token"), ("telegram", "bot_token"),
                ("turn", "username"), ("turn", "credential")]:
        assert written[key] == "", key
