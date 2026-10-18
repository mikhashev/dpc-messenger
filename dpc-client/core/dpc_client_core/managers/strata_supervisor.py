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
- 2. `stop` waits for the card's free VRAM to come back to what it was before
  the start; if it does not, an engine outlived the launcher (`contain()` can
  fail harmlessly) and the card is not claimed free.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import llama_server_supervisor as _base
from .llama_server_supervisor import LlamaServerError, LlamaServerSupervisor, _free_port

logger = logging.getLogger(__name__)

STRATA_DEFAULTS: Dict[str, Any] = {
    "strata_install_dir": None,
    "strata_base_config": None,
    # MiB of host RAM the launcher parks conversations in. Deliberately no
    # value here: the budget is bounded by the Windows commit limit (RAM plus
    # page file), the working pair is a dated measurement stored with the
    # alias, and a start without one is refused (ADR-044 point 7).
    "conversation_cache_mib": None,
    "conversation_cache_slots": None,
    # The server's window (`--max-context`), not the agent's: that one is
    # `context_window`, a config field nothing derives.
    "max_context": None,
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
    base config is read against the install dir, as the supervisor reads it)."""
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
    return errors


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
    block = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else None
    values["vision"] = "--vision" in args and block is not None
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

_COMMIT_LIMIT_NOTE = (
    "possibly the Windows commit limit (RAM + page file): Strata can exit there "
    "with an NTSTATUS code and no message"
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
        # A recorded engine death. While set, ensure_running refuses: DPC
        # never relaunches after it stopped the launcher on a death.
        self.failure: Optional[Dict[str, Any]] = None
        # What the running engine says it is, read after /health.
        self.engine_version: Optional[str] = None
        # The last stop's VRAM verdict: True released, False not, None unknown.
        self.vram_released: Optional[bool] = None
        self.last_stop_vram: Dict[str, Any] = {}
        self._vram_free_before: Optional[int] = None
        self._watch_task: Optional[asyncio.Task] = None
        # The start config the last launch used; the UI shows it (point 6).
        self.start_config_used: Optional[Path] = None

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

    def log_start(self, cache_type, env, binary=None) -> None:
        c = self.config
        logger.info(
            "strata[%s] starting on :%s (python=%s, config=%s, base=%s, "
            "conversation_cache_mib=%s, conversation_cache_slots=%s, max_context=%s, "
            "context_window=%s, vision=%s, control_vector=%s, tested_version=%s%s)",
            self.alias, self.port, binary, self.start_config_used,
            c.get("strata_base_config"), c.get("conversation_cache_mib"),
            _base._fmt_knob(c.get("conversation_cache_slots")),
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
            f"strata[{self.alias}] was stopped after an engine death and DPC will not "
            f"start it again: {f.get('reason', 'reason not recorded')}. Check the machine, "
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

    async def _launch(self, binary: Path, cache_type: Optional[str] = None) -> Dict[str, Any]:
        self._claim_card()
        self.write_start_config(self.port)
        self.vram_released = None
        self.engine_version = None
        try:
            self._vram_free_before = await asyncio.to_thread(_base._free_vram_mib)
        except Exception:
            self._vram_free_before = None
        props = await super()._launch(binary)
        await self._check_version()
        self._start_watch()
        return props

    async def _check_version(self) -> None:
        """The real engine's version against the tested one (ADR-044 point 5)."""
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
            return
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
            try:
                metrics = await self._get("/metrics")
            except Exception as e:
                logger.debug("strata[%s]: /metrics read failed: %s", self.alias, e)
                continue
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

    async def _fail_closed(self, observed: str, body: Optional[str] = None, stop: bool = True) -> None:
        """Record an engine death with its reason and stop the launcher."""
        if self.failure is not None:
            return
        tail = self.tail_log(20) + self._tail_engine_log(20)
        words = exit_code_words(body or "") or next(
            (w for w in (exit_code_words(line) for line in reversed(tail)) if w), None
        )
        reason = f"{observed}; {words or 'no exit code reported'}; {_COMMIT_LIMIT_NOTE}"
        self.failure = {
            "reason": reason,
            "exit_code": words,
            "observed": observed,
            "at": time.time(),
            "log_tail": tail[-12:],
        }
        logger.error(
            "strata[%s]: engine death — %s. Stopping the launcher; DPC will not restart "
            "it until the failure is reset",
            self.alias, reason,
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

    def _tail_engine_log(self, n: int = 12) -> List[str]:
        try:
            with open(self._engine_log_path, "rb") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    def call_slot(self):
        if self._draining:
            raise LlamaServerError(f"strata[{self.alias}] is draining; new calls refused")
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        return self._StrataSlot(self)

    class _StrataSlot:
        """The base slot, and the one place every request's error passes through."""

        def __init__(self, supervisor: "StrataSupervisor"):
            self._supervisor = supervisor

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._supervisor._in_flight -= 1
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
