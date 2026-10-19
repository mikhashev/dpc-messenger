"""
Strata Supervisor — one DPC-owned Strata launcher per alias (ADR-044).

DPC's child is Strata's Python launcher, `serve/server.py --engine strata`,
not its engine: the launcher spawns `strata.exe --serve` and
`strata-vision.exe` over pipes and puts them in a kill-on-close job object, so
one `_proc` per alias is the whole tree. Everything the base supervisor does
about the process itself is kept — CREATE_NEW_PROCESS_GROUP, CTRL_BREAK_EVENT
and never `terminate()`, the log capture, drain, the predecessor wait, the
registry the provider keeps.

What differs, by ADR-044 point:

- 6. DPC owns the start config. A Strata run config (`strata_base_config`) is
  the template; the typed fields override its flags and the result is written
  under `~/.dpc/strata/`, never into the Strata install. There is no default
  for `conversation_cache_mib`: the working value is a dated measurement.
- 1. `build_command` emits the venv's python and `serve/server.py` with
  `--engine strata` and `--port` always explicit — the launcher defaults to
  the mock engine and to a port of its own.
- 3. An engine death is fail-closed. The launcher restarts a dead engine on
  the next request, so a watcher reads `/metrics` while the launcher runs; a
  `live.state` of `unloaded` that no idle unload explains, a 503 naming the
  engine's exit code, or a launcher that exited on its own stops the
  launcher and records why. `ensure_running` then refuses until
  `reset_failure()` is called by a person.
- 5. After `/health` the engine's version is read from `/metrics`
  `engine.version` (fallback `/props` `build_info`) and compared with the
  alias's tested version: a mismatch warns, an absent version is an ERROR,
  because the mock engine reports none and would pass every other check.
  Then, once per start, the smoke: `/health`, `/props` and two forced tool
  calls, thinking off and then on at the alias's effort. An HTTP error or no
  tool call at all fails closed like the missing version; a call that timed
  out or came back with other arguments only warns, and so does a thinking
  call with no tool call whose finish_reason is `length` (the window spent).
- 2. `stop` waits for the card's free VRAM to come back to what it was before
  the start; if it does not, an engine outlived the launcher (`contain()` can
  fail harmlessly) and the card is not claimed free.
- 9. A peer's call is marked (`serving_a_peer`) so the supervisor can tell the
  owner's calls from a peer's: a peer's call never starts the launcher itself,
  and the owner's calls since the start are what the peer policy in
  `strata_provider.peer_refusal` weighs against the conversation cache. The
  policy may begin a start in the background instead (`start_for_peer`), one
  at a time, and answers the peer "loading" meanwhile.
"""

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import llama_server_supervisor as _base
from .commit_memory import describe_free_commit, read_free_commit
from .llama_server_supervisor import LlamaServerError, LlamaServerSupervisor, _free_port

logger = logging.getLogger(__name__)

# ADR-044 point 9: set while a call is served to a peer, holding its node id.
# A ContextVar because the mark has to reach `ensure_running` and `call_slot`
# through LLMManager and the provider without a parameter on every signature;
# it follows the awaits of the task that set it and nothing else.
_PEER_CALL: contextvars.ContextVar = contextvars.ContextVar("dpc_strata_peer_call", default=None)


@contextlib.contextmanager
def serving_a_peer(peer_id: str):
    """Mark the calls made inside as served to `peer_id`."""
    token = _PEER_CALL.set(peer_id or "peer")
    try:
        yield
    finally:
        _PEER_CALL.reset(token)


def peer_being_served() -> Optional[str]:
    """The peer the current call is served to, or None for the owner's own call."""
    return _PEER_CALL.get()


class PeerCallRefused(LlamaServerError):
    """A peer's call refused by the Strata supervisor itself (ADR-044 point 9):
    the host answers it `provider_unavailable`, and it is never retried."""


class PeerStartRefused(PeerCallRefused):
    """A peer's call met a launcher that is not up; the call does not start it
    (ADR-044 point 9 — only the peer policy may, in the background)."""


# Typed alias key -> (engine flag, Strata's own default, highest value Strata
# takes). The rules that decide what the conversation cache admits, from the
# engine's flag parser and help (src/program/generate.cpp, Strata 0.1.45 at
# 477ac13c, read 2026-10-19). Each is written into every start config, the
# default too, so the value the engine parks by is ours and named even when the
# alias sets none (Mike's call, 2026-10-19). The RAM floor is physical RAM —
# MemAvailable, GlobalMemoryStatusEx ullAvailPhys on Windows, under the cgroup
# limits and --memory-limit-mib — not free commit.
CACHE_ADMISSION_FLAGS: Dict[str, tuple] = {
    "conversation_cache_min_free_mib": ("--conversation-cache-min-free-mib", 2560, (2**63 - 1) // (1024 * 1024)),
    "conversation_cache_min_tokens": ("--conversation-cache-min-tokens", 0, 2**31 - 1),
    "conversation_cache_park_gain": ("--conversation-cache-park-gain", 0, 2**31 - 1),
}


def admission_error(key: str, value: Any) -> Optional[str]:
    """Why `value` cannot be the alias's `key` in CACHE_ADMISSION_FLAGS, or None
    (None is an absence: the Strata default is written)."""
    if value is None:
        return None
    _flag, _default, hi = CACHE_ADMISSION_FLAGS[key]
    if (not isinstance(value, bool) and isinstance(value, (int, float))
            and value == int(value) and 0 <= value <= hi):
        return None
    return f"{key}: expected a whole number from 0 to {hi}, not {value!r}"


def cache_admission(config: Dict[str, Any]) -> Dict[str, int]:
    """The admission values a start of `config` writes: the alias's, else Strata's default."""
    return {
        key: int(config[key]) if config.get(key) is not None else default
        for key, (_flag, default, _hi) in CACHE_ADMISSION_FLAGS.items()
    }


STRATA_DEFAULTS: Dict[str, Any] = {
    "strata_install_dir": None,
    "strata_base_config": None,
    # MiB of host RAM the launcher parks conversations in. Deliberately no
    # value here: the budget is bounded by the Windows commit limit (RAM plus
    # page file), the working pair is a dated measurement stored with the
    # alias, and a start without one is refused (ADR-044 point 7).
    "conversation_cache_mib": None,
    "conversation_cache_slots": None,
    # The admission rules: Strata's own defaults, held here so that they are
    # written explicitly (CACHE_ADMISSION_FLAGS).
    **{key: default for key, (_flag, default, _hi) in CACHE_ADMISSION_FLAGS.items()},
    # The server's window (`--max-context`), not the agent's: that one is
    # `context_window`, a config field nothing derives.
    "max_context": None,
    # `--resident-budget-gib` (GiB of host RAM the engine may pin for the experts
    # the GPU cache does not hold) and `--vram-reserve-mib` (VRAM left free for
    # the desktop and other programs). None keeps the base config's value.
    "resident_budget_gib": None,
    "vram_reserve_mib": None,
    # None keeps whatever the base config says; True / False decide.
    "vision": None,
    "control_vector": None,
    # Strata `/config` keys written into the start config the way Strata's own
    # serve/runconfig.py stores them: `sampling.<k>` in the "sampling" block,
    # `vram_reserve_mib` as its engine flag, everything else top level.
    "model_params": {},
    # Model files, each overriding what the base config names; None keeps the
    # base config's value. See MODEL_FILE_FLAGS for the engine flag of each.
    "native_gguf": None,
    "ple_gguf": None,
    "pack_dir": None,
    "mtp_dir": None,
    "expert_profile": None,
    # The vision block's projector, and the file part of
    # `--control-vector-scaled <file>:<scale>` (the scale stays the base's).
    "mmproj": None,
    "control_vector_file": None,
    # Engine flags, appended to the start config's "args" last.
    "extra_args": [],
    "tested_version": None,
    "tested_version_date": None,
    # The launcher loads a 20+ GB model before it listens; "a minute or two"
    # by its own words, longer on a cold disk.
    "start_timeout_s": 600.0,
    "engine_watch_interval_s": 5.0,
    # Each start-smoke tool call (ADR-044 point 5). /health answers before
    # the model is loaded, so the first call is the one that pulls the shards
    # in; running past this only warns.
    "smoke_timeout_s": 120.0,
    "vram_release_timeout_s": 30.0,
    # How far below its pre-start level free VRAM may sit after a stop and
    # still count as released: the desktop moves meanwhile.
    "vram_release_tolerance_mib": 1024,
}

# The keys a start config takes from the base config and from DPC, never from
# `model_params`: the program, its flags, where it runs and logs, and where it
# listens. `vision` has its own typed field.
_STRUCTURAL_KEYS = frozenset({
    "exe", "args", "cwd", "log", "port", "host", "tokenizer", "lib_dirs", "vision",
})

# Strata `/config` keys that live in "args" as an engine flag
# (serve/runconfig.py EDITABLE, kind ("arg", flag)).
_ARG_PARAMS = {"vram_reserve_mib": "--vram-reserve-mib"}

# The keys Strata's own page edits, with their kinds — serve/runconfig.py
# EDITABLE as of Strata 477ac13c (read 2026-10-18). `open_browser` is left out:
# DPC's start config always writes it false. Mirrored for the UI in
# dpc-client/ui/src/lib/utils/strataFields.ts (a test keeps the two equal).
STRATA_MODEL_PARAMS: Dict[str, Any] = {
    "sampling.temperature": ("sampling", "num>=0"),
    "sampling.top_p": ("sampling", "0<x<=1"),
    "sampling.top_k": ("sampling", "1..64"),
    "sampling.min_p": ("sampling", "0<=x<=1"),
    "reasoning_budget_tokens": "int>=0",
    "fit_max_tokens": "bool",
    "anthropic_thinking": ("enum", ("model", "on_request")),
    "effort_position": ("enum", ("start", "end")),
    "aliases": "names",
    "idle_unload_s": "num>=0",
    "lazy_load": "bool",
    "engine_silence_s": "num>=0",
    "api_monitor": "bool",
    "vram_reserve_mib": ("arg", "--vram-reserve-mib"),
}

# Typed alias key -> (engine flag, whole number, lowest value). Strata reads the
# budget as a float and clamps it to the commit capacity itself, so no upper
# bound here; a reserve of 0 is Strata's own "leave nothing", as model_params allows.
BUDGET_FLAGS: Dict[str, tuple] = {
    "resident_budget_gib": ("--resident-budget-gib", False, 0.0),
    "vram_reserve_mib": ("--vram-reserve-mib", True, 0),
}


def budget_error(key: str, value: Any) -> Optional[str]:
    """Why `value` cannot be the alias's `key` in BUDGET_FLAGS, or None (None is an absence)."""
    if value is None:
        return None
    _flag, whole, lo = BUDGET_FLAGS[key]
    ok = (not isinstance(value, bool) and isinstance(value, (int, float))
          and not (whole and value != int(value))
          and (value > lo if not whole else value >= lo))
    if ok:
        return None
    rule = "a whole number, at least 0" if whole else "a number more than 0"
    return f"{key}: expected {rule}, not {value!r}"


# Alias key -> the engine flag whose value it replaces (ADR-044 point 7).
MODEL_FILE_FLAGS: Dict[str, str] = {
    "native_gguf": "--native",
    "ple_gguf": "--ple-gguf",
    "pack_dir": "--pack",
    "mtp_dir": "--mtp",
    "expert_profile": "--expert-profile",
}

# Every alias key that names a file or folder, for the existence check on save.
STRATA_PATH_KEYS = (
    "strata_install_dir", "strata_base_config", *MODEL_FILE_FLAGS, "mmproj", "control_vector_file",
)

_SHARD_RE = re.compile(r"^(?P<stem>.*)-(?P<i>\d{5})-of-(?P<n>\d{5})(?P<ext>\.gguf)$", re.IGNORECASE)


def suggest_ple_gguf(native_gguf: Any) -> Optional[str]:
    """The second shard beside a first shard `...-00001-of-0000N.gguf`, or None.

    A suggestion only: the UI pre-fills it when the first shard is picked, and
    nothing applies it to an alias that leaves `ple_gguf` unset."""
    m = _SHARD_RE.match(str(native_gguf or "").strip())
    if not m or int(m["i"]) != 1 or int(m["n"]) < 2:
        return None
    return f"{m['stem']}-00002-of-{m['n']}{m['ext']}"


def model_param_error(key: str, value: Any) -> Optional[str]:
    """Why `value` cannot be stored for Strata `/config` key `key`, or None.

    Strata's own rules (serve/runconfig.py `check`); None removes the key."""
    if key not in STRATA_MODEL_PARAMS:
        if key == "open_browser":
            return "open_browser is always false in DPC's start config"
        return f"{key!r} is not a key Strata's page edits ({', '.join(STRATA_MODEL_PARAMS)})"
    if value is None:
        return None
    kind = STRATA_MODEL_PARAMS[key]
    k = kind[0] if isinstance(kind, tuple) else kind

    def number(whole=False, lo=0.0, hi=None, lo_open=False) -> Optional[str]:
        ok = (not isinstance(value, bool) and isinstance(value, (int, float))
              and not (whole and value != int(value))
              and not (value <= lo if lo_open else value < lo)
              and not (hi is not None and value > hi))
        if ok:
            return None
        rng = f"{'more than' if lo_open else 'at least'} {lo:g}" + (f" and at most {hi:g}" if hi is not None else "")
        return f"{key}: expected a {'whole ' if whole else ''}number, {rng}, not {value!r}"

    if k == "bool":
        return None if isinstance(value, bool) else f"{key}: expected true or false, not {value!r}"
    if k == "enum":
        return None if value in kind[1] else f"{key}: expected one of {', '.join(kind[1])}, not {value!r}"
    if k == "names":
        names = [x.strip() for x in value.split(",")] if isinstance(value, str) else value
        if isinstance(names, list) and all(isinstance(x, str) and x.strip() and len(x) <= 128 for x in names):
            return None
        return f"{key}: expected a list of model names, not {value!r}"
    if k == "int>=0" or k == "arg":
        return number(whole=True)
    if k == "num>=0":
        return number()
    rule = kind[1]
    if rule == "num>=0":
        return number()
    if rule == "0<x<=1":
        return number(hi=1, lo_open=True)
    if rule == "0<=x<=1":
        return number(hi=1)
    return number(whole=True, lo=1, hi=64)


def strata_config_errors(config: Dict[str, Any], check_paths: bool = False) -> List[str]:
    """What keeps a `strata` alias from being saved; empty when nothing does.

    The required keys, the cache budget's type and the model parameters always;
    with `check_paths`, also that every path the alias sets exists (a relative
    base config is read against the install dir, as the supervisor reads it),
    and that `reasoning_effort` is a word the chat template accepts. Both read
    the disk, so both are the save's checks only: the load calls this without
    `check_paths`, and an alias saved before the word check is not dropped
    from the loaded set — its provider warns instead (`StrataProvider`)."""
    errors: List[str] = []
    missing = [k for k in ("strata_install_dir", "strata_base_config") if not config.get(k)]
    if missing:
        errors.append(
            f"needs {' and '.join(missing)}: the Strata root (serve/server.py, .venv) "
            "and an existing Strata run config to start from"
        )
    if config.get("conversation_cache_mib") is None:
        errors.append(
            "needs conversation_cache_mib: there is no default — the parking budget is "
            "bounded by the Windows commit limit (RAM + page file) and is a value measured "
            "on this machine"
        )
    for key in ("conversation_cache_mib", "conversation_cache_slots", "max_context"):
        if config.get(key) is not None:
            try:
                _base._positive_int(key, config[key])
            except LlamaServerError as e:
                errors.append(str(e))
    errors.extend(e for e in (budget_error(k, config.get(k)) for k in BUDGET_FLAGS) if e)
    errors.extend(e for e in (admission_error(k, config.get(k)) for k in CACHE_ADMISSION_FLAGS) if e)
    params = config.get("model_params")
    if params is not None and not isinstance(params, dict):
        errors.append(f"model_params must be an object, got {params!r}")
    elif params:
        errors.extend(e for e in (model_param_error(k, v) for k, v in params.items()) if e)
    extra = config.get("extra_args")
    if extra is not None and not isinstance(extra, list):
        errors.append(f"extra_args must be a list of tokens, got {extra!r}")
    if check_paths:
        install = Path(str(config["strata_install_dir"])) if config.get("strata_install_dir") else None
        for key in STRATA_PATH_KEYS:
            value = config.get(key)
            if not value:
                continue
            path = Path(str(value))
            if key == "strata_base_config" and not path.is_absolute() and install is not None:
                path = install / path
            if not path.exists():
                errors.append(f"{key}: {path} does not exist")
        word_error = reasoning_effort_error(config)
        if word_error:
            errors.append(word_error)
    return errors


def strata_chat_template(config: Dict[str, Any]) -> str:
    """The text of `<tokenizer>/chat_template.jinja` the alias's base config names.

    A relative base config is read against the install dir, a relative
    tokenizer against the base config's `cwd` (else the install dir), as
    Strata reads them. Raises OSError, ValueError, TypeError or AttributeError."""
    install = Path(str(config.get("strata_install_dir") or ""))
    base = Path(str(config.get("strata_base_config") or ""))
    if not base.is_absolute():
        base = install / base
    cfg = json.loads(base.read_text(encoding="utf-8-sig"))
    tokenizer = Path(str(cfg.get("tokenizer") or ""))
    if not tokenizer.is_absolute():
        tokenizer = Path(str(cfg.get("cwd") or install)) / tokenizer
    return (tokenizer / "chat_template.jinja").read_text(encoding="utf-8")


def reasoning_effort_error(config: Dict[str, Any]) -> Optional[str]:
    """Why the alias's `reasoning_effort` is a word its chat template refuses, or None.

    The provider's own rule (`_template_effort`): `off`, a template word, or a
    fleet word folding onto one passes. Anything else the provider drops and
    sends the template's default in its place (`StrataProvider._build_extra_body`),
    so the owner's word never reaches the engine. None too when the template is
    unreadable or unguarded: there is nothing to check the word against, and
    the provider falls back to its table."""
    from ..providers.base import REASONING_OFF
    from ..providers.llamacpp_server_provider import FLEET_TO_TEMPLATE

    word = str(config.get("reasoning_effort") or "").strip().lower()
    if not word or word == REASONING_OFF:
        return None
    try:
        found = _base.effort_dictionary_of(strata_chat_template(config))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not found:
        return None
    words, default = found
    if word in words or FLEET_TO_TEMPLATE.get(word) in words:
        return None
    sent = default if default in words else ("xhigh" if "xhigh" in words else None)
    return (
        f"reasoning_effort: {word!r} is not a word the model's chat template accepts "
        f"({'/'.join(words)}); the provider would drop it and send "
        f"{sent or 'no word'} instead"
    )


def read_base_config(path: Path) -> Dict[str, Any]:
    """A Strata run config as a dict; ValueError naming what is wrong with it."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ValueError(f"cannot read the base config {path}: {e}") from None
    except ValueError as e:
        raise ValueError(f"the base config {path} is not JSON: {e}") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("args"), list) or not cfg.get("exe"):
        raise ValueError(
            f"the base config {path} has no \"exe\" and \"args\" — is it a Strata run config?"
        )
    return cfg


def reads_images(cfg: Dict[str, Any]) -> bool:
    """Whether a Strata run or start config starts the vision encoder: `--vision`
    among its args and a "vision" block to start it with."""
    args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
    return "--vision" in [str(a) for a in args] and isinstance(cfg.get("vision"), dict)


def _number_or_raw(raw: Optional[str]) -> Any:
    """A flag value as a number when it is one (24, 24.5), else as written."""
    if raw is None:
        return None
    try:
        n = float(raw)
    except ValueError:
        return raw
    return int(n) if n.is_integer() else n


def _flag_value(args: List[str], flag: str) -> Optional[str]:
    """The value after `flag` in `args`, or None when the flag is absent or bare."""
    args = [str(a) for a in args]
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    return None


def split_control_vector(value: str) -> tuple:
    """(file, scale) of a `--control-vector-scaled` value; the scale is None when absent.

    Split at the last colon only when what follows is a number, so `C:\\x.gguf`
    keeps its drive letter."""
    head, sep, tail = str(value).rpartition(":")
    if sep:
        try:
            float(tail)
            return head, tail
        except ValueError:
            pass
    return str(value), None


def inspect_base_config(path: Any, install_dir: Any = None) -> Dict[str, Any]:
    """What a base config would start with, keyed like the alias fields, for the UI's placeholders.

    ValueError when the file is not a readable Strata run config."""
    p = Path(str(path or ""))
    if not str(path or "").strip():
        raise ValueError("no base config path given")
    if not p.is_absolute() and install_dir:
        p = Path(str(install_dir)) / p
    cfg = read_base_config(p)
    args = [str(a) for a in cfg["args"]]
    values: Dict[str, Any] = {key: _flag_value(args, flag) for key, flag in MODEL_FILE_FLAGS.items()}
    for key, flag in (("max_context", "--max-context"),
                      ("conversation_cache_mib", "--conversation-cache-mib"),
                      ("conversation_cache_slots", "--conversation-cache-slots")):
        raw = _flag_value(args, flag)
        values[key] = int(raw) if raw is not None and raw.isdigit() else raw
    # Shown as "the base config's N is not used": the start writes the alias's
    # value or Strata's default over it.
    for key, (flag, _default, _hi) in CACHE_ADMISSION_FLAGS.items():
        raw = _flag_value(args, flag)
        values[key] = int(raw) if raw is not None and raw.isdigit() else raw
    for key, (flag, _whole, _lo) in BUDGET_FLAGS.items():
        values[key] = _number_or_raw(_flag_value(args, flag))
    block = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else None
    values["vision"] = reads_images(cfg)
    values["mmproj"] = (block or {}).get("mmproj")
    cvec = _flag_value(args, "--control-vector-scaled")
    cv_file, cv_scale = split_control_vector(cvec) if cvec else (None, None)
    values["control_vector"] = any(_is_control_vector_flag(a) for a in args)
    values["control_vector_file"] = cv_file
    params: Dict[str, Any] = {}
    for key, kind in STRATA_MODEL_PARAMS.items():
        if isinstance(kind, tuple) and kind[0] == "sampling":
            s = cfg.get("sampling")
            v = s.get(key.split(".", 1)[1]) if isinstance(s, dict) else None
        elif isinstance(kind, tuple) and kind[0] == "arg":
            raw = _flag_value(args, kind[1])
            v = int(raw) if raw is not None and raw.isdigit() else raw
        else:
            v = cfg.get(key)
        if v is not None:
            params[key] = v
    named = [v for k, v in values.items() if k in MODEL_FILE_FLAGS or k in ("mmproj", "control_vector_file")]
    return {
        "path": str(p),
        "model_name": cfg.get("model_name"),
        "values": values,
        "control_vector_scale": cv_scale,
        "vision_model": (block or {}).get("model"),
        "model_params": params,
        "suggested_ple_gguf": suggest_ple_gguf(values.get("native_gguf")),
        "missing_files": [v for v in named if v and not Path(str(v)).exists()],
    }

# ADR-044 point 5: a start whose launcher names no engine version is refused.
NO_ENGINE_VERSION = "no engine version reported — mock engine or wrong --engine"
_NO_VERSION_NOTE = (
    "/metrics engine.version and /props build_info are both absent after /health, "
    "which is what Strata's mock engine looks like; check that the launcher got "
    "--engine strata and that the engine is a real build"
)

# ADR-044 point 5: the start smoke after the version check. One trivial tool,
# forced by name, called twice: thinking off, then thinking on at the alias's
# own effort, the path every agent round takes. `strata_checkpoint: false` is
# Strata's own key for a one-shot call (serve/server.py, #830), so the probe
# takes no conversation checkpoint and parks nothing.
SMOKE_FAILED = "the start smoke failed"
SMOKE_TOOL = "report_ready"
SMOKE_MAX_TOKENS = 64
# The thinking call: Strata closes the thinking at the budget with its wrap-up
# and writes the forced call's opening itself, so the call needs only the room
# left after it. 512 of 1024 leaves 512 for a call of about twenty tokens; with
# max_tokens 512 and no budget, an xhigh thinking phase could spend the whole
# window and come back with no call — a false fail-closed. (Strata's own
# answer_room starts only at max_tokens 1024 and a config can turn it off.)
SMOKE_THINKING_MAX_TOKENS = 1024
SMOKE_THINKING_BUDGET = 512
_SMOKE_NOTE = (
    "the launcher answered /health and named an engine version; read the launcher "
    "and engine logs"
)


def smoke_request(
    model: str, thinking: bool = False, template_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One OpenAI-shape tool-call request of the smoke.

    Thinking off by default; with `thinking`, `template_kwargs` is what the
    provider would send on a tools call of this alias (None: no word, the
    template's default runs), beside the smoke's own window and budget."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": f'Call {SMOKE_TOOL} with status "ok".'}],
        "tools": [{
            "type": "function",
            "function": {
                "name": SMOKE_TOOL,
                "description": "Report that the engine is ready.",
                "parameters": {
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"],
                },
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": SMOKE_TOOL}},
        "max_tokens": SMOKE_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "strata_checkpoint": False,
    }
    if thinking:
        body.pop("chat_template_kwargs")
        if template_kwargs:
            body["chat_template_kwargs"] = dict(template_kwargs)
        body["max_tokens"] = SMOKE_THINKING_MAX_TOKENS
        body["reasoning_budget_tokens"] = SMOKE_THINKING_BUDGET
    return body


_COMMIT_LIMIT_NOTE = (
    "possibly the Windows commit limit (RAM + page file): Strata can exit there "
    "with an NTSTATUS code and no message"
)


def _commit_note(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> str:
    """The commit-limit explanation of a death, with what GlobalMemoryStatusEx read.

    `before` is the last reading the supervisor took (its `at` is monotonic),
    `after` one taken now. Without either it is `_COMMIT_LIMIT_NOTE` and the
    words that nothing was measured; the numbers are stated, never judged."""
    if before is None and after is None:
        return f"{_COMMIT_LIMIT_NOTE}; {describe_free_commit(None)}"
    parts = []
    if before is not None:
        ago = max(0.0, time.monotonic() - float(before.get("at") or time.monotonic()))
        parts.append(f"{describe_free_commit(before)} {ago:.0f} s before the death")
    if after is not None:
        parts.append(f"{describe_free_commit(after)} after it")
    return (
        "the Windows commit limit (RAM + page file) is where Strata can exit with an "
        "NTSTATUS code and no message: " + "; ".join(parts)
    )

# Strata's own words for an engine that died (serve/server.py: EngineDied,
# engine_exit_words, and the restart line).
_EXIT_CODE_RE = re.compile(r"exit code\s+(-?0x[0-9A-Fa-f]+|-?\d+|None)(?::\s*([^)\n;]+))?")


def _drop_flag(args: List[str], is_target) -> List[str]:
    """`args` without each flag `is_target` accepts, and the values that follow it.

    A value is every token up to the next `--` flag: `--control-vector-layer-range
    4 44` takes two, the others one."""
    out: List[str] = []
    i = 0
    while i < len(args):
        a = str(args[i])
        if a.startswith("--") and is_target(a):
            i += 1
            while i < len(args) and not str(args[i]).startswith("--"):
                i += 1
            continue
        out.append(a)
        i += 1
    return out


def _set_flag(args: List[str], flag: str, value: Any) -> List[str]:
    """`flag value` in `args`: the existing value replaced, or the pair appended."""
    out = [str(a) for a in args]
    if flag in out:
        i = out.index(flag)
        if i + 1 < len(out) and not out[i + 1].startswith("--"):
            out[i + 1] = str(value)
        else:
            out.insert(i + 1, str(value))
        return out
    return out + [flag, str(value)]


def _is_control_vector_flag(flag: str) -> bool:
    return flag.startswith("--control-vector") or flag.startswith("--cvec-")


def _smoke_error(step: str, elapsed: float, e: BaseException) -> str:
    """A smoke step's exception as the failure names it, the launcher's body included."""
    detail = getattr(getattr(e, "response", None), "text", None)
    return (f"{step + ' ' if step else ''}after {elapsed:.1f}s: {type(e).__name__}: {e}"
            + (f" — {detail[:300]}" if detail else ""))


def _smoke_tokens(reply: Any) -> str:
    """What a smoke reply spent, from its `usage`: completion tokens, and the
    reasoning tokens when the reply carries the split."""
    usage = (reply or {}).get("usage") if isinstance(reply, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    words = f"{usage.get('completion_tokens', 'unreported')} completion tokens"
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if reasoning is None:
        reasoning = usage.get("reasoning_tokens")
    return words + (f" ({reasoning} reasoning)" if reasoning is not None else "")


def exit_code_words(text: str) -> Optional[str]:
    """`exit code ...` as Strata wrote it in a 503 body or a log line, or None."""
    found = _EXIT_CODE_RE.findall(text or "")
    if not found:
        return None
    code, words = found[-1]
    out = f"exit code {code}"
    # The mid-request 503 carries the raw status (3221226505), the launcher's
    # own words the NTSTATUS form (0xC0000409); a reader looks the latter up.
    if code.lstrip("-").isdigit():
        status = int(code) & 0xFFFFFFFF
        if status >= 0xC0000000:
            out += f" (0x{status:08X})"
    return out + (f": {words.strip()}" if words.strip() else "")


# Every line the engine writes when its conversation cache gave a conversation up or
# could not use one (src/program/generate.cpp, the park and restore paths of --serve;
# the formats are copied below). First match wins,
# so the specific "skip parking" reasons stand before the generic one. A line is not a
# loss when it says parked / restored / borrowed / dropped N superseded copies.
#   skip parking (%zu tokens, below the %lld minimum; parked=%zu)
#   skip parking (stage CUDA%d: %s)                               one stage failed on the device
#   skip parking (%s)                                             the size of the snapshot
#   skip parking (snapshot %zu MiB exceeds available budget)
#   skip parking (physical RAM admission; need %zu MiB plus %lld MiB floor, %s; evicted %zu, %zu still parked)
#   skip parking (RAM floor after capture: %lld MiB needed, %s)
#   allocation failed; skip parking
#   evicted %zu parked conversation%s to admit this snapshot (%zu MiB plus %lld MiB floor)
#   %s %zu tokens in %.1f ms; parked=... with %s = "skipped" (the cache refused the stored image)
#   discard snapshot (stage count)  /  discard invalid [stage ]snapshot (%s)     at restore
#   cannot borrow from a parked conversation (%s)  /  no room beside the parked conversation ...
#   the state at %lld tokens has this prompt's tokens but picture %zu ... cannot be reused
_CACHE_LOSS_KINDS = (
    ("below min tokens", re.compile(r"skip parking \(\d+ tokens, below the")),
    ("over budget", re.compile(r"skip parking \(snapshot \d+ MiB exceeds available budget")),
    ("RAM admission", re.compile(r"skip parking \(physical RAM admission")),
    ("RAM floor", re.compile(r"skip parking \(RAM floor after capture")),
    ("allocation failed", re.compile(r"allocation failed; skip parking")),
    ("evicted", re.compile(r"evicted \d+ parked conversations? to admit this snapshot")),
    ("not stored", re.compile(r"conversation cache: skipped \d+ tokens")),
    ("discarded at restore", re.compile(r"conversation cache: discard (?:invalid )?(?:stage )?snapshot")),
    ("borrow refused", re.compile(r"cannot borrow from a parked conversation|no room beside the parked conversation")),
    ("picture changed", re.compile(r"has this prompt's tokens but picture \d+ .*cannot be reused")),
    ("stage CUDA failed", re.compile(r"skip parking \(stage CUDA\d+:")),
    ("snapshot size failed", re.compile(r"skip parking \(")),
)
_CACHE_LOSS_SHOWN = 3


def cache_loss_kind(line: str) -> Optional[str]:
    """The reason class of an engine-log line where the cache lost a conversation, or None."""
    for kind, pattern in _CACHE_LOSS_KINDS:
        if pattern.search(line):
            return kind
    return None


def _positive(name: str, value: Any) -> int:
    try:
        return _base._positive_int(name, value)
    except LlamaServerError as e:
        raise LlamaServerError(f"strata: {e}") from None


class StrataSupervisor(LlamaServerSupervisor):
    """Owns the Strata launcher process for one alias."""

    DEFAULTS = STRATA_DEFAULTS
    ENGINE = "strata"
    # Strata takes most of the card, so no other alias shares it (ADR-044 point 4).
    EXCLUSIVE_ON_THE_CARD = True

    def on_the_card(self) -> bool:
        return True

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", alias).strip("._") or "alias"
        self._safe_alias = safe
        self._log_path = _base.DPC_HOME / "logs" / f"strata-{safe}.log"
        # The engine's own log (the start config's "log"): the launcher opens
        # it itself, so it cannot share the file this process captures.
        self._engine_log_path = _base.DPC_HOME / "logs" / f"strata-{safe}-engine.log"
        # Where the cache-loss scan stopped in that file; None until primed.
        self._engine_scan_offset: Optional[int] = None
        # The launcher's cumulative conversation_cache.evictions as the watcher
        # last read it, and as the last INFO record had it. The engine counts
        # every eviction, the budget and slot ones too, which have no log line.
        self._cache_evictions_polled: Optional[int] = None
        self._cache_evictions_logged = 0
        # A recorded engine death. While set, ensure_running refuses: DPC
        # never relaunches after it stopped the launcher on a death.
        self.failure: Optional[Dict[str, Any]] = None
        # What the running engine says it is, read after /health.
        self.engine_version: Optional[str] = None
        # The last stop's VRAM verdict: True released, False not, None unknown.
        self.vram_released: Optional[bool] = None
        self.last_stop_vram: Dict[str, Any] = {}
        self._vram_free_before: Optional[int] = None
        # The last GlobalMemoryStatusEx reading (commit_memory) with `at`
        # (monotonic): taken at the start and on every engine-watch poll, so a
        # death is explained with what was free just before it.
        self.last_free_commit: Optional[Dict[str, Any]] = None
        self._watch_task: Optional[asyncio.Task] = None
        # The start config the last launch used; the UI shows it (point 6).
        self.start_config_used: Optional[Path] = None
        # The owner's calls on this launcher since it started, the ones running
        # now, and when the last one ended (monotonic). Peer calls are not
        # counted: the peer policy asks whether anything parked can be ours.
        self.owner_calls = 0
        self.owner_in_flight = 0
        self.owner_last_end: Optional[float] = None
        # The last /metrics read the peer policy made: (monotonic time, body).
        self.peer_metrics_read: Optional[tuple] = None
        # `owner_calls` as it stood when the peer policy last let a peer in;
        # a peer's slot checks it again (`peer_slot_refusal`).
        self.peer_cleared_at_owner_calls: Optional[int] = None
        # The background start a peer's request began (`start_for_peer`); while
        # it runs, no second one begins.
        self.peer_start_task: Optional[asyncio.Task] = None
        # (key, answer) of the last `start_reads_images`; the key holds the
        # base config's mtime and size, so an edit there is seen.
        self._start_vision_cache: Optional[tuple] = None
        # The `chat_template_kwargs` the provider sends on a tools call of
        # this alias, or None: set by StrataProvider, so the smoke's thinking
        # call runs the agents' effort without a second copy of its logic.
        self.smoke_effort: Optional[Callable[[], Optional[Dict[str, Any]]]] = None

    # --- paths ----------------------------------------------------------------

    def install_dir(self) -> Path:
        value = self.config.get("strata_install_dir")
        if not value:
            raise LlamaServerError(
                f"strata[{self.alias}] needs strata_install_dir: the Strata root "
                "holding serve/server.py and .venv"
            )
        return Path(str(value))

    def python_path(self) -> Path:
        venv = self.install_dir() / ".venv"
        if sys.platform == "win32":
            return venv / "Scripts" / "python.exe"
        return venv / "bin" / "python"

    def server_script(self) -> Path:
        return self.install_dir() / "serve" / "server.py"

    def base_config_path(self) -> Path:
        value = self.config.get("strata_base_config")
        if not value:
            raise LlamaServerError(
                f"strata[{self.alias}] needs strata_base_config: an existing Strata "
                "run config (e.g. strata-<model>.json) to take the engine, the "
                "tokenizer and the flags from"
            )
        path = Path(str(value))
        return path if path.is_absolute() else self.install_dir() / path

    def start_config_path(self) -> Path:
        """DPC's own start config for this alias, under ~/.dpc, never in the install."""
        return _base.DPC_HOME / "strata" / f"{self._safe_alias}.json"

    # --- the start config (ADR-044 point 6) -------------------------------------

    def build_start_config(self, port: int) -> Dict[str, Any]:
        """The base config with the typed fields applied. Pure apart from reading the base."""
        c = self.config
        if c.get("conversation_cache_mib") is None:
            raise LlamaServerError(
                f"strata[{self.alias}]: conversation_cache_mib is not set. There is no "
                "default — the parking budget is bounded by the Windows commit limit "
                "(RAM + page file) and must be a value measured on this machine; set "
                "it on the alias before starting"
            )
        base_path = self.base_config_path()
        try:
            cfg = read_base_config(base_path)
        except ValueError as e:
            raise LlamaServerError(f"strata[{self.alias}]: {e}") from None
        args = [str(a) for a in cfg["args"]]

        # Model files: each set key replaces the base's flag value; unset keeps it.
        base_native = _flag_value(args, "--native")
        for key, flag in MODEL_FILE_FLAGS.items():
            if c.get(key):
                args = _set_flag(args, flag, c[key])
        block = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else None
        if block is not None and (c.get("native_gguf") or c.get("mmproj")):
            block = dict(block)
            # The encoder reads the same weights the engine does; it follows
            # the first shard only where the base had it naming that shard.
            if c.get("native_gguf") and base_native and block.get("model") == base_native:
                block["model"] = str(c["native_gguf"])
            if c.get("mmproj"):
                block["mmproj"] = str(c["mmproj"])
            cfg["vision"] = block
        elif c.get("mmproj") and c.get("vision") is not False:
            raise LlamaServerError(
                f"strata[{self.alias}]: mmproj is set but the base config {base_path} has "
                "no \"vision\" block (encoder exe) to put it in"
            )

        args = _set_flag(args, "--conversation-cache-mib",
                         _positive("conversation_cache_mib", c["conversation_cache_mib"]))
        if c.get("conversation_cache_slots") is not None:
            args = _set_flag(args, "--conversation-cache-slots",
                             _positive("conversation_cache_slots", c["conversation_cache_slots"]))
        # Always written, the Strata default included, over whatever the base says.
        for key, (flag, _default, _hi) in CACHE_ADMISSION_FLAGS.items():
            refusal = admission_error(key, c.get(key))
            if refusal:
                raise LlamaServerError(f"strata[{self.alias}]: {refusal}")
        for key, value in cache_admission(c).items():
            args = _set_flag(args, CACHE_ADMISSION_FLAGS[key][0], value)
        if c.get("max_context") is not None:
            args = _set_flag(args, "--max-context", _positive("max_context", c["max_context"]))

        vision = c.get("vision")
        if vision is False:
            args = _drop_flag(args, lambda f: f == "--vision")
            cfg.pop("vision", None)
        elif vision is True:
            if not isinstance(cfg.get("vision"), dict):
                raise LlamaServerError(
                    f"strata[{self.alias}]: vision is on but the base config {base_path} "
                    "has no \"vision\" block (encoder exe and mmproj) to start it with"
                )
            if "--vision" not in args:
                args.append("--vision")

        control_vector = c.get("control_vector")
        if control_vector is False:
            args = _drop_flag(args, _is_control_vector_flag)
        elif c.get("control_vector_file"):
            current = _flag_value(args, "--control-vector-scaled")
            if current is None:
                raise LlamaServerError(
                    f"strata[{self.alias}]: control_vector_file is set but the base config "
                    f"{base_path} has no --control-vector-scaled to take the scale, layer "
                    "range and mode from; add the flags through extra_args instead"
                )
            _file, scale = split_control_vector(current)
            new = str(c["control_vector_file"])
            args = _set_flag(args, "--control-vector-scaled", f"{new}:{scale}" if scale else new)
        elif control_vector is True and not any(_is_control_vector_flag(a) for a in args):
            logger.warning(
                "strata[%s]: control_vector is on but the base config %s carries no "
                "--control-vector flag; the engine starts without one",
                self.alias, base_path,
            )

        params = c.get("model_params") or {}
        if not isinstance(params, dict):
            raise LlamaServerError(f"strata[{self.alias}]: model_params must be an object, got {params!r}")
        for key, value in params.items():
            if key in _STRUCTURAL_KEYS:
                raise LlamaServerError(
                    f"strata[{self.alias}]: model_params may not set {key!r} — the start "
                    "config takes it from the base config and the alias's typed fields"
                )
            refusal = model_param_error(key, value)
            if refusal:
                raise LlamaServerError(f"strata[{self.alias}]: model_params: {refusal}")
            if key == "aliases" and isinstance(value, str):
                value = [x.strip() for x in value.split(",") if x.strip()] or None
            if key.startswith("sampling."):
                sampling = dict(cfg.get("sampling") or {}) if isinstance(cfg.get("sampling"), dict) else {}
                sub = key.split(".", 1)[1]
                if value is None:
                    sampling.pop(sub, None)
                else:
                    sampling[sub] = value
                if sampling:
                    cfg["sampling"] = sampling
                else:
                    cfg.pop("sampling", None)
            elif key in _ARG_PARAMS:
                flag = _ARG_PARAMS[key]
                args = _drop_flag(args, lambda f, _f=flag: f == _f) if value is None else _set_flag(args, flag, value)
            elif value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
        # The typed fields win over model_params.vram_reserve_mib, which older
        # aliases may carry: set, they replace the base's value; unset, the
        # base's (or model_params') value stays.
        for key, (flag, _whole, _lo) in BUDGET_FLAGS.items():
            value = c.get(key)
            if value is None:
                continue
            refusal = budget_error(key, value)
            if refusal:
                raise LlamaServerError(f"strata[{self.alias}]: {refusal}")
            args = _set_flag(args, flag, value)
        if cfg.get("lazy_load") is True and cfg.get("vision"):
            raise LlamaServerError(
                f"strata[{self.alias}]: lazy_load is text-only and this start reads images "
                "(\"vision\"); turn one of them off"
            )

        args += [str(a) for a in (c.get("extra_args") or [])]
        cfg["args"] = args
        cfg.setdefault("cwd", str(self.install_dir()))
        # The base config's log sits in the install; DPC's start writes its own.
        cfg["log"] = str(self._engine_log_path)
        cfg["port"] = int(port)
        cfg["host"] = "127.0.0.1"
        cfg["open_browser"] = False
        cfg["_dpc"] = {
            "written_by": "DPC Messenger (ADR-044)",
            "alias": self.alias,
            "base_config": str(base_path),
            "tested_version": c.get("tested_version"),
            "tested_version_date": c.get("tested_version_date"),
        }
        return cfg

    def start_reads_images(self) -> bool:
        """Whether a start now would read images — `build_start_config`'s own
        result, so this answer and the next start cannot disagree.

        False when no start config can be built (the start would fail too).
        Cached on the base config file's mtime and size and the alias config."""
        try:
            base = self.base_config_path()
            st = base.stat()
        except (LlamaServerError, OSError):
            return False
        key = (str(base), st.st_mtime_ns, st.st_size,
               json.dumps(self.config, sort_keys=True, default=str))
        cached = self._start_vision_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            answer = reads_images(self.build_start_config(0))
        except LlamaServerError as e:
            logger.info("strata[%s]: no start config to read vision from: %s", self.alias, e)
            answer = False
        self._start_vision_cache = (key, answer)
        return answer

    def write_start_config(self, port: int) -> Path:
        """Write DPC's start config whole (temp file, then replace) and return its path."""
        cfg = self.build_start_config(port)
        path = self.start_config_path()
        install = self.install_dir().resolve()
        target = path.resolve()
        if target == install or install in target.parents:
            raise LlamaServerError(
                f"strata[{self.alias}]: refusing to write the start config inside the "
                f"Strata install ({install})"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        self.start_config_used = path
        return path

    def _unload_expected(self) -> bool:
        """Whether an `unloaded` engine is a configured state rather than a death.

        Read off the start config as written: the launcher's idle unload and
        lazy load are config keys (`idle_unload_s`, `lazy_load`)."""
        path = self.start_config_used
        cfg: Dict[str, Any] = {}
        if path is not None:
            try:
                cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cfg = {}
        params = self.config.get("model_params") or {}
        idle = cfg.get("idle_unload_s", params.get("idle_unload_s"))
        lazy = cfg.get("lazy_load", params.get("lazy_load"))
        try:
            idle_on = bool(idle) and float(idle) > 0
        except (TypeError, ValueError):
            idle_on = False
        return idle_on or lazy is True

    # --- command assembly (ADR-044 point 1) -------------------------------------

    def build_command(self, binary: Path, port: int, cache_type: Optional[str] = None) -> List[str]:
        """The venv's python, the launcher script, the engine, DPC's config, the port.

        `binary` is the python to run; `cache_type` belongs to llama-server's
        KV ladder and means nothing here."""
        return [
            str(binary), str(self.server_script()),
            "--engine", "strata",
            "--config", str(self.start_config_path()),
            "--port", str(port),
            "--host", "127.0.0.1",
        ]

    def build_env(self, binary: Path) -> Dict[str, str]:
        env = dict(os.environ)
        # The launcher's stdout is a file here, not a console: unbuffered so a
        # death is in the log when it is read, UTF-8 so a non-ASCII line does
        # not end the launcher on a cp1252 encode error.
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    async def _spawn(self, cmd: List[str], env: Dict[str, str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=str(self.install_dir()),
            stdout=self._log_fh,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=_base._CREATION_NEW_PROCESS_GROUP,
        )

    def _sample_free_commit(self) -> Optional[Dict[str, Any]]:
        reading = read_free_commit()
        if reading is not None:
            self.last_free_commit = {**reading, "at": time.monotonic()}
        return reading

    def start_cache_flags(self) -> Optional[Dict[str, Any]]:
        """The conversation cache flags in the start config the last launch used,
        keyed like the alias fields; None before a launch or when the file is unreadable.

        The slots stay None when neither the alias nor the base set them: the
        engine's own default (4) applies and the file names none."""
        path = self.start_config_used
        if path is None:
            return None
        try:
            args = json.loads(Path(path).read_text(encoding="utf-8")).get("args")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(args, list):
            return None
        flags = {"conversation_cache_mib": "--conversation-cache-mib",
                 "conversation_cache_slots": "--conversation-cache-slots",
                 **{k: v[0] for k, v in CACHE_ADMISSION_FLAGS.items()}}
        return {key: _number_or_raw(_flag_value(args, flag)) for key, flag in flags.items()}

    def log_start(self, cache_type, env, binary=None) -> None:
        c = self.config
        admission = cache_admission(c)
        logger.info(
            "strata[%s] starting on :%s (python=%s, config=%s, base=%s, "
            "conversation_cache_mib=%s beside %s, conversation_cache_slots=%s, "
            "conversation_cache_min_free_mib=%s (physical RAM floor), conversation_cache_min_tokens=%s, "
            "conversation_cache_park_gain=%s, max_context=%s, "
            "context_window=%s, vision=%s, control_vector=%s, tested_version=%s%s)",
            self.alias, self.port, binary, self.start_config_used,
            c.get("strata_base_config"), c.get("conversation_cache_mib"),
            describe_free_commit(self._sample_free_commit()),
            _base._fmt_knob(c.get("conversation_cache_slots")),
            admission["conversation_cache_min_free_mib"], admission["conversation_cache_min_tokens"],
            admission["conversation_cache_park_gain"],
            _base._fmt_knob(c.get("max_context")), _base._fmt_knob(c.get("context_window")),
            "base config" if c.get("vision") is None else c.get("vision"),
            "base config" if c.get("control_vector") is None else c.get("control_vector"),
            c.get("tested_version") or "unset",
            f" of {c['tested_version_date']}" if c.get("tested_version_date") else "",
        )

    # --- lifecycle ----------------------------------------------------------------

    def _refusal(self) -> LlamaServerError:
        f = self.failure or {}
        return LlamaServerError(
            f"strata[{self.alias}] was stopped and DPC will not start it again: "
            f"{f.get('reason', 'reason not recorded')}. Check the machine and the install, "
            "then reset it explicitly",
            f.get("log_tail"),
        )

    async def ensure_running(self) -> Dict[str, Any]:
        """The launcher is up and its /props are returned; started if needed.

        Overrides the inherited one, which relaunches whenever the child's
        returncode is set: after an engine death DPC stops the launcher and a
        person, not this method, decides when it runs again (ADR-044 point 3).
        A launcher that exited on its own is recorded as such a death too —
        DPC's own stops clear `_proc`, so a dead `_proc` here was not asked to go."""
        async with self._start_lock:
            if self.failure is not None:
                raise self._refusal()
            if self._draining:
                raise LlamaServerError(f"strata[{self.alias}] is draining; new calls refused")
            proc = self._proc
            if proc is not None and proc.returncode is None and self.props:
                return self.props
            if proc is not None and proc.returncode is not None:
                await self._fail_closed(
                    f"the launcher exited on its own with {proc.returncode}", body=None, stop=True,
                )
                raise self._refusal()
            # A peer's call never starts the owner's engine (ADR-044 point 9):
            # this is the one place a start begins, so the rule holds whichever
            # door or retry the call came through. The peer policy's own start
            # (`start_for_peer`) runs unmarked, as the owner's call would.
            peer = peer_being_served()
            if peer is not None:
                raise PeerStartRefused(
                    f"strata[{self.alias}] is not running, and a call served to peer "
                    f"{peer} does not start it"
                )
            await self._await_predecessor()
            self._claim_card()
            try:
                python = self.python_path()
                script = self.server_script()
                for needed in (python, script):
                    if not needed.exists():
                        raise LlamaServerError(
                            f"strata[{self.alias}]: {needed} does not exist — is "
                            f"{self.install_dir()} a Strata install with its venv set up?"
                        )
                self.port = self.port or _free_port()
                return await self._launch(python)
            except BaseException:
                self._release_card_unless_live()
                raise

    def start_for_peer(self, peer_id: Optional[str]) -> bool:
        """Begin the owner's own start (`ensure_running`) in the background for a
        peer's request, without the peer mark; False when one is already under
        way (ADR-044 point 9, Mike's call 2026-10-20).

        The check and the task are made with no await between them, so
        concurrent peer requests begin one start. The peer is not made to wait:
        the policy answers it "loading", and the parking rule applies once the
        launcher is up."""
        task = self.peer_start_task
        if task is not None and not task.done():
            return False
        ctx = contextvars.copy_context()
        ctx.run(_PEER_CALL.set, None)
        self.peer_start_task = asyncio.get_running_loop().create_task(
            self._start_for_peer(peer_id), context=ctx,
        )
        return True

    async def _start_for_peer(self, peer_id: Optional[str]) -> None:
        try:
            await self.ensure_running()
        except Exception as e:
            # Nobody awaits this task: the failure is recorded here, and the
            # next peer request meets the state it left (failed, or stopped).
            logger.warning(
                "strata[%s]: the start begun for peer %s failed: %s",
                self.alias, peer_id or "unknown", e,
            )

    async def _launch(self, binary: Path, cache_type: Optional[str] = None) -> Dict[str, Any]:
        self._claim_card()
        self.write_start_config(self.port)
        self.vram_released = None
        self.engine_version = None
        # A new launcher starts with an empty conversation cache.
        self.owner_calls = 0
        self.owner_last_end = None
        self.peer_metrics_read = None
        self.peer_cleared_at_owner_calls = None
        try:
            self._vram_free_before = await asyncio.to_thread(_base._free_vram_mib)
        except Exception:
            self._vram_free_before = None
        props = await super()._launch(binary)
        if await self._check_version() is None:
            # ADR-044 point 5: the smoke counts only after the version check,
            # because the mock engine answers /health and /props like a real
            # one. No version is refused like an engine death: stopped, and
            # not started again until a person resets it.
            await self._fail_closed(NO_ENGINE_VERSION, note=_NO_VERSION_NOTE)
            raise self._refusal()
        failed = await self._smoke()
        if failed is not None:
            body = failed if exit_code_words(failed) else None
            await self._fail_closed(
                f"{SMOKE_FAILED}: {failed}", body=body, note=None if body else _SMOKE_NOTE,
            )
            raise self._refusal()
        self._start_watch()
        return self.props or props

    async def _check_version(self) -> Optional[str]:
        """The real engine's version against the tested one (ADR-044 point 5).

        Returns the version, or None when the launcher reports none — the
        caller fails closed on that. A mismatch with the tested version only
        warns."""
        version: Optional[str] = None
        try:
            metrics = await self._get("/metrics")
            engine = (metrics or {}).get("engine") or {}
            if isinstance(engine, dict) and engine.get("version"):
                version = str(engine["version"])
        except Exception as e:
            logger.debug("strata[%s]: /metrics unreadable for the version: %s", self.alias, e)
        if version is None:
            build = (self.props or {}).get("build_info")
            if isinstance(build, str) and build.strip():
                version = build.strip()
                if version.lower().startswith("strata "):
                    version = version[len("strata "):].strip()
        self.engine_version = version
        tested = self.config.get("tested_version")
        if version is None:
            logger.error(
                "strata[%s]: the launcher reports no engine version (/metrics engine.version "
                "and /props build_info are both absent) — this is what the mock engine "
                "looks like, and everything else it answers would pass; check that "
                "--engine strata reached it and that the engine is a real build",
                self.alias,
            )
            return None
        if tested and str(tested).strip() != version:
            logger.warning(
                "strata[%s]: engine version %s differs from the tested version %s%s — "
                "starting anyway; re-test and update the alias's tested version",
                self.alias, version, tested,
                f" (tested {self.config['tested_version_date']})"
                if self.config.get("tested_version_date") else "",
            )
        else:
            logger.info("strata[%s]: engine version %s (tested %s)", self.alias, version, tested or "unset")
        return version

    async def _smoke(self) -> Optional[str]:
        """The start smoke of ADR-044 point 5, run by `_launch` once per start
        and only after the version check.

        `/health`, `/props`, then two tool calls forced by name with no
        conversation checkpoint: thinking off, then thinking on at the effort
        the provider sends on a tools call of this alias (skipped when that
        effort is off, or no provider has wired it). Returns why it failed —
        /health not 200, /props not an object, an HTTP error, a malformed
        reply or no tool call at all — or None.

        Only warns, and serves: a call that ran past `smoke_timeout_s` (/health
        answers before the model is loaded, so the first call is the request
        that loads it, and a slow disk is not a broken engine), and a call
        that came back with another name or other arguments (the engine
        answered with a call, which is what the smoke asks of it), and a
        thinking call with no tool call and finish_reason `length` (the
        thinking spent the window; Ark's and Zcode's review). Every verdict on
        a missing call names the finish_reason and the tokens spent, every
        verdict names its call (thinking off, thinking on and its effort), and
        every call that ran carries its seconds: the thinking call is the only
        thinking-on call DPC makes to Strata, so its time is the price of
        thinking (Ark)."""
        t0 = time.monotonic()
        step = "/health"
        try:
            if not await self._health_ok():
                return "/health did not answer 200"
            step = "/props"
            props = await self._get("/props")
            if not isinstance(props, dict):
                return f"/props answered {type(props).__name__}, not an object"
            self.props = props
        except Exception as e:
            return _smoke_error(step, time.monotonic() - t0, e)
        model = str(self.config.get("model") or "strata")
        results = [("thinking off", await self._smoke_call(smoke_request(model)))]
        if results[0][1][0] == "failed":
            return f"the thinking-off call {results[0][1][1]}"
        effort = self._smoke_effort_kwargs()
        if isinstance(effort, str):
            results.append((f"thinking on: {effort}", None))
        else:
            label = f"thinking on ({(effort or {}).get('reasoning_effort') or 'template default'})"
            results.append((label, await self._smoke_call(smoke_request(model, True, effort), thinking=True)))
            if results[1][1][0] == "failed":
                return f"the {label} call {results[1][1][1]}"
        warn = any(r is not None and r[0] != "ok" for _label, r in results)
        (logger.warning if warn else logger.info)(
            "strata[%s]: start smoke passed in %.1fs (/health, /props); %s%s",
            self.alias, time.monotonic() - t0,
            "; ".join(f"{label}: {r[1] if r else 'not run'}" for label, r in results),
            " — serving anyway" if warn else "",
        )
        return None

    def _smoke_effort_kwargs(self) -> Any:
        """The thinking call's `chat_template_kwargs` (None: the template's
        default), or a str saying why that call is not run."""
        if self.smoke_effort is None:
            return "no provider wired its effort"
        try:
            kwargs = self.smoke_effort()
        except Exception as e:
            logger.warning("strata[%s]: the smoke could not read the alias's effort: %s", self.alias, e)
            return f"the alias's effort unreadable ({type(e).__name__})"
        if (kwargs or {}).get("enable_thinking") is False:
            return "the alias's effort is off"
        return kwargs

    async def _smoke_call(self, body: Dict[str, Any], thinking: bool = False) -> tuple:
        """One forced tool call of the smoke -> (verdict, words).

        `ok`, `surprise` (another name or other arguments), `timeout` and
        `window` serve; `failed` (an HTTP error, any other exception, no tool
        call) does not. `window` is the thinking call's only: no tool call
        with finish_reason `length`, the thinking having spent the window
        before the call's opening was written — a budget question, not an
        engine that cannot call. With any other finish_reason, or on the
        thinking-off call (64 tokens and nothing to think), no call fails."""
        import httpx

        t0 = time.monotonic()
        timeout = float(self.config.get("smoke_timeout_s") or 120.0)
        try:
            reply = await self._post("/v1/chat/completions", body, timeout)
            choice = ((reply or {}).get("choices") or [{}])[0] or {}
            calls = (choice.get("message") or {}).get("tool_calls")
        except httpx.TimeoutException as e:
            # The time it took, not only the limit: a connect timeout comes
            # back long before the limit, and the passed line carries each
            # call's latency (Ark: the thinking call's is the price of thinking).
            return "timeout", (
                f"no answer within {timeout:.0f}s ({type(e).__name__} after "
                f"{time.monotonic() - t0:.1f}s) — the first request after "
                "/health loads the model, so this may be a slow load, not a broken engine"
            )
        except Exception as e:
            return "failed", _smoke_error("", time.monotonic() - t0, e)
        elapsed = time.monotonic() - t0
        if not calls:
            finish = choice.get("finish_reason")
            spent = f"finish_reason {finish or 'absent'}, {_smoke_tokens(reply)}, {elapsed:.1f}s"
            if thinking and finish == "length":
                return "window", (
                    f"no tool call ({spent}) — the window ran out while thinking, before the "
                    "call was written; the budget or max_tokens is short, not the engine"
                )
            return "failed", f"came back with no tool call ({spent})"
        fn = (calls[0] or {}).get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = fn.get("arguments")
        if fn.get("name") != SMOKE_TOOL or args != {"status": "ok"}:
            return "surprise", (
                f"a surprise in {elapsed:.1f}s, the call came back as {fn.get('name')}"
                f"({json.dumps(args, default=str)[:200]}), not {SMOKE_TOOL}(status=\"ok\")"
            )
        return "ok", f"{SMOKE_TOOL} in {elapsed:.1f}s"

    async def _post(self, path: str, body: Dict[str, Any], timeout: float) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"http://127.0.0.1:{self.port}{path}", json=body)
            resp.raise_for_status()
            return resp.json()

    def _start_watch(self) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            return
        self._watch_task = asyncio.get_running_loop().create_task(self._watch_engine())

    async def _watch_engine(self) -> None:
        """Poll the launcher's /metrics while it runs; fail closed on an engine death."""
        interval = float(self.config.get("engine_watch_interval_s") or 5.0)
        while True:
            await asyncio.sleep(interval)
            proc = self._proc
            if proc is None:
                return
            if proc.returncode is not None:
                await self._fail_closed(f"the launcher exited on its own with {proc.returncode}")
                return
            self._sample_free_commit()
            try:
                metrics = await self._get("/metrics")
            except Exception as e:
                logger.debug("strata[%s]: /metrics read failed: %s", self.alias, e)
                continue
            self._note_cache_evictions(metrics)
            state = ((metrics or {}).get("live") or {}).get("state")
            if state == "unloaded" and not self._unload_expected():
                await self._fail_closed(
                    "the launcher reports the engine unloaded (/metrics live.state) and "
                    "no idle unload or lazy load is configured"
                )
                return

    def note_request_error(self, error: BaseException) -> Optional[str]:
        """The engine death a request's error names, or None.

        A 503 whose body names the engine's exit code is the launcher saying
        the engine died under that request (`EngineDied`); the next request
        would restart it."""
        code = _base_status_code(error)
        text = str(error)
        body = getattr(error, "body", None)
        if body is not None:
            text = f"{text} {body}"
        if code != 503:
            return None
        if "exit code" not in text and "stopped unexpectedly" not in text:
            return None
        return text

    async def _fail_closed(
        self, observed: str, body: Optional[str] = None, stop: bool = True,
        note: Optional[str] = None,
    ) -> None:
        """Record an engine death with its reason and stop the launcher.

        `note` replaces the exit-code words and the commit-limit explanation,
        for a refusal that is not a death (no engine version reported)."""
        if self.failure is not None:
            return
        tail = self.tail_log(20) + self._tail_engine_log(20)
        if note is not None:
            words = None
            reason = f"{observed}; {note}"
        else:
            words = exit_code_words(body or "") or next(
                (w for w in (exit_code_words(line) for line in reversed(tail)) if w), None
            )
            before, after = self.last_free_commit, read_free_commit()
            reason = f"{observed}; {words or 'no exit code reported'}; {_commit_note(before, after)}"
        self.failure = {
            "reason": reason,
            "exit_code": words,
            "free_commit": None if note is not None else {"before": before, "after": after},
            "observed": observed,
            "at": time.time(),
            "log_tail": tail[-12:],
        }
        logger.error(
            "strata[%s]: %s — %s. Stopping the launcher; DPC will not restart "
            "it until the failure is reset",
            self.alias, "engine death" if note is None else "start refused", reason,
        )
        if stop:
            await self.stop()

    def reset_failure(self) -> Optional[Dict[str, Any]]:
        """Clear a recorded engine death so the next call may start the launcher.

        The only way out of the failed state; a person calls it."""
        previous, self.failure = self.failure, None
        if previous is not None:
            logger.warning("strata[%s]: engine-death failure reset by hand (was: %s)",
                           self.alias, previous.get("reason"))
        return previous

    def _note_cache_evictions(self, metrics: Any) -> None:
        """Keep the cache's cumulative eviction count from a /metrics body."""
        cache = metrics.get("conversation_cache") if isinstance(metrics, dict) else None
        value = cache.get("evictions") if isinstance(cache, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            self._cache_evictions_polled = value

    def _new_cache_evictions(self) -> int:
        """Evictions the engine counted since the last INFO record, as of the last poll.

        The poll runs every `engine_watch_interval_s`, so an eviction can be
        reported one call late. A smaller count than the one logged means the
        engine started over with an empty cache; that is said once, not rebased silently.

        This counter is the authoritative count: Strata's `evict_oldest()`, which
        `make_room()` calls (include/strata/core/conversation_cache.hpp), increments
        `evictions_` and prints nothing, so the text lines of `_CACHE_LOSS_KINDS`
        only name a reason for some of the evictions.
        """
        polled = self._cache_evictions_polled
        if polled is None:
            return 0
        if polled < self._cache_evictions_logged:
            logger.info("strata[%s]: engine eviction counter went from %d to %d; "
                        "the engine's counter started over",
                        self.alias, self._cache_evictions_logged, polled)
            self._cache_evictions_logged = 0
        delta = polled - self._cache_evictions_logged
        self._cache_evictions_logged = polled
        return delta

    def _tail_engine_log(self, n: int = 12) -> List[str]:
        try:
            with open(self._engine_log_path, "rb") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    # --- the conversation cache, read where Strata says it ----------------------

    def prime_restore_scan(self) -> None:
        """Watch the engine log from here on; the launcher log is primed beside it.

        The base primes `_log_path`, which for Strata is the launcher's stdout
        and never carries a cache event. Called at launch by the base `_launch`.
        """
        super().prime_restore_scan()
        self._cache_evictions_polled = None
        self._cache_evictions_logged = 0
        try:
            self._engine_scan_offset = self._engine_log_path.stat().st_size
        except OSError:
            self._engine_scan_offset = 0

    def new_cache_losses(self) -> List[str]:
        """Engine-log lines since the last scan where the cache gave a conversation up.

        Each line is one of the engine's own reasons (`_CACHE_LOSS_KINDS`): a
        parked conversation evicted, a snapshot not parked, a parked image
        discarded or not borrowable at restore. Each ends in a re-prefill on a
        later call. `borrowed`, `parked`, `restored` and `dropped ... superseded
        copy` lines are the
        cache working, not losing, and are left out. Only whole lines are taken:
        the cursor stops after the last newline, so a line the engine is still
        writing is read next time, complete.
        """
        offset = self._engine_scan_offset
        if offset is None:
            self.prime_restore_scan()
            return []
        try:
            with open(self._engine_log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # The file shrank — rotated or truncated under us: watch what is
                # there now, including its first byte.
                start = 0 if offset > size else offset
                f.seek(start)
                data = f.read(self._SCAN_CHUNK)
        except OSError:
            return []
        whole = data.rfind(b"\n") + 1
        if whole == 0 and len(data) == self._SCAN_CHUNK:
            whole = len(data)  # one line longer than the cap must not stall the cursor
        self._engine_scan_offset = start + whole
        return [
            line.strip()
            for line in data[:whole].decode("utf-8", errors="replace").splitlines()
            if cache_loss_kind(line) is not None
        ]

    def log_restore_refusals(self) -> int:
        """Say what the conversation cache gave up since the last call; INFO.

        Keeps the base name because the provider asks for it by that name after
        every call; the count is the number of events, not of calls.
        """
        lost = self.new_cache_losses()
        evictions = self._new_cache_evictions()
        if lost:
            shown = [line[:300] for line in lost[:_CACHE_LOSS_SHOWN]]
            more = len(lost) - len(shown)
            kinds = Counter(cache_loss_kind(line) for line in lost)
            classes = ", ".join(f"{n} {kind}" for kind, n in kinds.most_common())
            logger.info(
                "strata[%s]: %d conversation-cache loss(es) since the last call (%s): %s%s%s",
                self.alias, len(lost), classes, " | ".join(shown),
                f" (+{more} more in the engine log)" if more > 0 else "",
                f"; engine eviction counter +{evictions}" if evictions else "",
            )
        elif evictions:
            # Slot and budget evictions leave no log line; only the counter has them.
            logger.info(
                "strata[%s]: engine eviction counter +%d since the last call, no loss line in the engine log",
                self.alias, evictions,
            )
        return len(lost)

    def peer_slot_refusal(self) -> Optional[str]:
        """The peer policy's synchronous half, asked again at the call boundary.

        The policy (`StrataProvider.peer_refusal`) is asked by the P2P door
        before the call, and the owner does not queue behind peers, so an owner
        call can begin between that answer and this slot. Whatever an owner
        call does, it raises `owner_calls` at its own slot, so a count that
        moved since the policy let this peer in — or an owner call running —
        refuses the peer here. No /metrics read: the slot is synchronous.
        An owner call that begins after this slot is taken runs beside the
        peer's; the policy cannot stop a call already running."""
        if self.owner_in_flight > 0:
            return f"'{self.alias}' began serving this node's owner while the peer's call was being prepared"
        if self.peer_cleared_at_owner_calls is None:
            return f"'{self.alias}': no peer policy decision stands for this call"
        if self.owner_calls != self.peer_cleared_at_owner_calls:
            return f"'{self.alias}' served this node's owner after the peer policy let the call in"
        return None

    def call_slot(self):
        if self._draining:
            raise LlamaServerError(f"strata[{self.alias}] is draining; new calls refused")
        owner = peer_being_served() is None
        if not owner:
            refusal = self.peer_slot_refusal()
            if refusal:
                raise PeerCallRefused(f"strata[{self.alias}]: {refusal}")
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        if owner:
            self.owner_calls += 1
            self.owner_in_flight += 1
        return self._StrataSlot(self, owner)

    class _StrataSlot:
        """The base slot, and the one place every request's error passes through."""

        def __init__(self, supervisor: "StrataSupervisor", owner: bool = True):
            self._supervisor = supervisor
            self._owner = owner

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._supervisor._in_flight -= 1
            if self._owner:
                self._supervisor.owner_in_flight -= 1
                self._supervisor.owner_last_end = time.monotonic()
            if exc is not None:
                body = self._supervisor.note_request_error(exc)
                if body is not None:
                    await self._supervisor._fail_closed(
                        "a request got a 503 naming the engine's exit code", body=body,
                    )
            return False

    async def _stop_child(self) -> None:
        """Stop the launcher as the base does, then check that the card let go.

        Inside the base `stop`, so the card stays claimed until the check ends."""
        task, self._watch_task = self._watch_task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        had_child = self._proc is not None and self._proc.returncode is None
        await super()._stop_child()
        if had_child or self._vram_free_before is not None:
            await self._verify_vram_released()

    async def _verify_vram_released(self) -> Optional[bool]:
        """Wait, bounded, for free VRAM to return to its pre-start level (ADR-044 point 2)."""
        before = self._vram_free_before
        self._vram_free_before = None
        if before is None:
            self.vram_released = None
            self.last_stop_vram = {"before_mib": None, "after_mib": None, "released": None}
            logger.warning(
                "strata[%s]: no pre-start VRAM reading, so whether the card was released "
                "after the stop is unknown", self.alias,
            )
            return None
        tolerance = int(self.config.get("vram_release_tolerance_mib") or 0)
        deadline = time.monotonic() + float(self.config.get("vram_release_timeout_s") or 0)
        after: Optional[int] = None
        while True:
            try:
                after = await asyncio.to_thread(_base._free_vram_mib)
            except Exception:
                after = None
            if after is not None and after >= before - tolerance:
                self.vram_released = True
                break
            if time.monotonic() >= deadline:
                self.vram_released = False if after is not None else None
                break
            await asyncio.sleep(1.0)
        self.last_stop_vram = {"before_mib": before, "after_mib": after, "released": self.vram_released}
        if self.vram_released is False:
            logger.error(
                "strata[%s]: the launcher exited but the card's free VRAM is %s MiB against "
                "%s MiB before the start — a strata.exe likely outlived it (the job object "
                "may not have applied); the card is NOT free until that process is gone",
                self.alias, after, before,
            )
        elif self.vram_released is None:
            logger.warning("strata[%s]: VRAM unreadable after the stop; release unknown", self.alias)
        return self.vram_released


def _base_status_code(error: BaseException) -> Optional[int]:
    code = getattr(error, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None) if response is not None else None
    return code if isinstance(code, int) else None
