# dpc-client/core/run_service.py

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os

# HF fast-transfer guard — must run BEFORE any import that pulls in
# huggingface_hub. The env var is read at huggingface_hub import time, not at
# use time, so per-consumer guards (memory.py, whisper_provider.py, etc.) are
# too late if the parent module already imported huggingface_hub. Single
# source of truth — every downstream HF consumer (BGE-M3, GLiNER, Whisper)
# inherits the corrected value automatically.
if os.environ.get("HF_HUB_ENABLE_HF_TRANSFER") == "1":
    try:
        import hf_transfer  # noqa: F401
    except ImportError:
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        # Logging not configured yet at this stage — use print.
        print(
            "[startup] Disabled HF_HUB_ENABLE_HF_TRANSFER "
            "(hf_transfer package not installed)"
        )

# S144 — HF offline mode guard. Same constraint as the fast-transfer block
# above: must run BEFORE any import that pulls in huggingface_hub, since
# HF_HUB_OFFLINE is read at huggingface_hub import time.
#
# When [hf] offline_mode = true in ~/.dpc/config.ini, set HF_HUB_OFFLINE=1
# so transformers / sentence-transformers / gliner read cached models from
# disk without ETag-refresh HEAD requests to huggingface.co. Default off
# so first-time users still download models normally; flip to true after
# the BGE-M3 / GLiNER / Whisper models are cached locally if the per-startup
# HF HEAD-request noise in dpc-client.log bothers you.
#
# Risk when on: a missing-cache load raises OSError("We couldn't connect to
# huggingface.co") instead of silently downloading. Acceptable for the
# explicit-opt-in use case (Mike's machine has all needed models cached
# at S144 time — bge-m3 4.4GB, gliner_multi-v2.1 2.2GB, whisper-large-v3
# -turbo 1.5GB). Per-call-site local_files_only=True wrappers stay as
# defense-in-depth where they exist (whisper_provider, token_count_manager).
import configparser as _hf_configparser
from pathlib import Path as _HFPath
_hf_cfg = _hf_configparser.ConfigParser(inline_comment_prefixes=('#',))
_hf_cfg_path = _HFPath.home() / ".dpc" / "config.ini"
if _hf_cfg_path.exists():
    try:
        _hf_cfg.read(_hf_cfg_path, encoding="utf-8")
    except _hf_configparser.Error:
        pass  # malformed config — proceed without the offline opt-in

def _hf_required_models() -> set:
    """Model ids this install loads through the HF cache.

    Read from where each one is actually defined, never copied here. The
    ordering constraint above bans importing huggingface_hub before the env
    var is set — it does not ban importing our own modules, and none of
    these pulls it in (asserted by test_hf_offline_autodetect, so the day
    one of them grows a top-level `import huggingface_hub` the ban is not
    quietly broken).

    A source that cannot be read contributes nothing rather than raising:
    a model that goes unlisted only keeps the process online, which is the
    safe direction.
    """
    import json as _hf_json

    wanted = set()

    try:
        from dpc_client_core.dpc_agent.memory_config import MemoryConfig
        wanted.add(MemoryConfig.embedding_model)
    except Exception:
        pass

    try:
        from dpc_client_core.dpc_agent.knowledge_graph import GLINER_MODEL_NAME
        wanted.add(GLINER_MODEL_NAME)
    except Exception:
        pass

    # Agents may point memory at a different embedding model than the default.
    agents_dir = _HFPath.home() / ".dpc" / "agents"
    if agents_dir.is_dir():
        for cfg in agents_dir.glob("*/config.json"):
            try:
                data = _hf_json.loads(cfg.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            model = (data.get("memory") or {}).get("embedding_model")
            if isinstance(model, str) and model:
                wanted.add(model)

    # The tokenizers the token counter loads for local models. They were the one
    # source missing here, and the omission had a shape: everything else was
    # cached, so the process went offline, and then the counter could not fetch
    # the tokenizer it wanted even though the network was there. Falling back to
    # one-token-per-four-characters costs about a fifth of the count on Russian
    # (measured 315 against 382 on one page), so this is not cosmetic.
    try:
        from dpc_client_core.managers.token_count_manager import TokenCountManager

        providers_path = _HFPath.home() / ".dpc" / "providers.json"
        providers = _hf_json.loads(providers_path.read_text(encoding="utf-8"))
        for entry in providers.get("providers", []):
            if entry.get("type") != "ollama":
                continue
            family = str(entry.get("model") or "").split(":")[0].lower()
            for prefix, repo in TokenCountManager.OLLAMA_TOKENIZER_MAP.items():
                if family.startswith(prefix):
                    wanted.add(repo)
                    break
    except Exception:
        pass

    # Whisper has no module constant — the id lives in providers.json, which
    # is also the only place that knows whether this install has one at all.
    try:
        from dpc_client_core.dpc_agent.tools.transcribe import _WHISPER_PROVIDER_TYPE

        providers_path = _HFPath.home() / ".dpc" / "providers.json"
        providers = _hf_json.loads(providers_path.read_text(encoding="utf-8"))

        def _walk(node):
            if isinstance(node, dict):
                if node.get("type") == _WHISPER_PROVIDER_TYPE:
                    model = node.get("model")
                    if isinstance(model, str) and model:
                        yield model
                for value in node.values():
                    yield from _walk(value)
            elif isinstance(node, list):
                for item in node:
                    yield from _walk(item)

        wanted.update(_walk(providers))
    except Exception:
        pass

    return wanted


def _hf_cache_root() -> "_HFPath":
    """Where huggingface_hub keeps model snapshots, honouring its own env vars."""
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return _HFPath(explicit)
    home = os.environ.get("HF_HOME")
    if home:
        return _HFPath(home) / "hub"
    return _HFPath.home() / ".cache" / "huggingface" / "hub"


def _hf_model_fully_cached(model_id: str, root: "_HFPath") -> bool:
    """True when this model can be loaded with no network at all.

    Presence of the directory is not the question — an interrupted download
    leaves one behind that looks complete from the outside. What decides it
    is a snapshot with files in it and no *.incomplete blob still parked in
    the cache.
    """
    model_dir = root / ("models--" + model_id.replace("/", "--"))
    snapshots = model_dir / "snapshots"
    if not snapshots.is_dir():
        return False
    if not any(any(rev.iterdir()) for rev in snapshots.iterdir() if rev.is_dir()):
        return False
    blobs = model_dir / "blobs"
    if blobs.is_dir() and any(blobs.glob("*.incomplete")):
        return False
    return True


# Held for setup_logging() to repeat into the log — nothing here can log yet.
_HF_STARTUP_NOTE = ""


def _hf_announce(note: str) -> None:
    global _HF_STARTUP_NOTE
    _HF_STARTUP_NOTE = note
    print("[startup] " + note)


try:
    _hf_offline_set = _hf_cfg.has_option("hf", "offline_mode")
    _hf_offline = _hf_cfg.getboolean("hf", "offline_mode", fallback=False)
except (ValueError, _hf_configparser.Error):
    _hf_offline_set, _hf_offline = False, False

if _hf_offline:
    # setdefault so an explicit HF_HUB_OFFLINE env var still wins.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    _hf_announce(
        "HF_HUB_OFFLINE=1 (from [hf] offline_mode=true) — "
        "transformers/sentence-transformers/gliner will skip HF Hub HEAD "
        "requests and read directly from ~/.cache/huggingface/"
    )
elif not _hf_offline_set:
    # No explicit answer in the config, so ask the disk instead of asking the
    # user to remember. Everything needed already cached means a HEAD request
    # to huggingface.co can only confirm what we have, so skip the network for
    # this run. Anything missing and we stay online and it downloads normally
    # — which is also what makes this safe to leave on: adding a model does not
    # need anyone to go flip a flag back.
    #
    # Not covered: a model nothing declares — a provider added to a fallback
    # chain but absent from providers.json, say. If one is ever needed and is
    # not cached, offline turns a download into an error. The startup line
    # below names exactly what was checked so that decision is never silent.
    try:
        _hf_root = _hf_cache_root()
        _hf_wanted = sorted(_hf_required_models())
        _hf_missing = [m for m in _hf_wanted if not _hf_model_fully_cached(m, _hf_root)]
        if not _hf_wanted:
            # Nothing could be sourced, so nothing was verified. An empty set
            # trivially satisfies "all present" — going offline on it would be
            # a decision made on no evidence.
            _hf_announce(
                "HF Hub stays online — could not determine which models this "
                "install needs"
            )
        elif _hf_missing:
            _hf_announce(
                "HF Hub stays online — not fully cached: " + ", ".join(_hf_missing)
            )
        else:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            _hf_announce(
                "HF_HUB_OFFLINE=1 (auto) — all startup models cached locally, "
                "skipping HF Hub HEAD requests: " + ", ".join(_hf_wanted)
            )
    except OSError as _hf_exc:
        # An unreadable cache is not grounds to cut the network.
        _hf_announce(f"HF cache check skipped ({_hf_exc}) — staying online")

import argparse
import platform  # Import the platform module to check the OS
import subprocess
import sys
import re
import traceback
from pathlib import Path
from dpc_client_core.service import CoreService
from dpc_client_core.dpc_agent.loop import shutdown_shared_executor
from dpc_client_core.__version__ import __version__
from dpc_client_core import single_instance

logger = logging.getLogger(__name__)


class Base64TruncatingFilter(logging.Filter):
    """
    Logging filter that truncates base64 encoded data in log messages.

    This prevents huge base64 strings (like images) from cluttering logs.
    """

    def __init__(self, max_length=30):
        super().__init__()
        self.max_length = max_length
        # Multiple patterns to match base64 data in various formats:
        # Pattern 1: 'data': 'iVBORw0KG...'  (Anthropic SDK format)
        # Pattern 2: "data": "iVBORw0KG..."  (double quotes)
        # Pattern 3: 'image_base64': 'data:image/png;base64,iVBORw0KG...'
        self.patterns = [
            re.compile(r"('data'\s*:\s*')([A-Za-z0-9+/=]{30,})"),  # Single quotes, data field
            re.compile(r'("data"\s*:\s*")([A-Za-z0-9+/=]{30,})'),  # Double quotes, data field
            re.compile(r"('image_base64'\s*:\s*')([A-Za-z0-9+/:;=,]{30,})"),  # Single quotes, image_base64 field
            re.compile(r'("image_base64"\s*:\s*")([A-Za-z0-9+/:;=,]{30,})'),  # Double quotes, image_base64 field
        ]

    def filter(self, record):
        """Truncate base64 data in the log message."""
        # Apply patterns to the format string
        if isinstance(record.msg, str):
            for pattern in self.patterns:
                record.msg = pattern.sub(
                    lambda m: f"{m.group(1)}{m.group(2)[:self.max_length]}... (base64 truncated, ~{len(m.group(2))} chars)",
                    record.msg
                )

        # Apply patterns to arguments (only if they're string or dict - leave other types alone)
        if record.args:
            if isinstance(record.args, dict):
                # Single dict arg - convert to string and truncate
                arg_str = str(record.args)
                for pattern in self.patterns:
                    arg_str = pattern.sub(
                        lambda m: f"{m.group(1)}{m.group(2)[:self.max_length]}... (base64 truncated, ~{len(m.group(2))} chars)",
                        arg_str
                    )
                record.args = (arg_str,)
            elif isinstance(record.args, tuple):
                # Tuple of args - only process if it contains exactly one dict
                if len(record.args) == 1 and isinstance(record.args[0], dict):
                    arg_str = str(record.args[0])
                    for pattern in self.patterns:
                        arg_str = pattern.sub(
                            lambda m: f"{m.group(1)}{m.group(2)[:self.max_length]}... (base64 truncated, ~{len(m.group(2))} chars)",
                            arg_str
                        )
                    record.args = (arg_str,)
                # Otherwise, leave tuple unchanged (e.g., websocket.remote_address tuple)

        return True


def setup_logging(settings):
    """Configure logging based on settings."""
    # Create log directory
    log_file = settings.get_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.get_log_level()))

    # Create base64 truncating filter (applies to all handlers)
    base64_filter = Base64TruncatingFilter(max_length=30)

    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.get_log_max_bytes(),
        backupCount=settings.get_log_backup_count(),
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    file_handler.addFilter(base64_filter)  # Add base64 truncating filter
    root_logger.addHandler(file_handler)

    # Console handler (optional)
    if settings.get_log_console():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, settings.get_log_console_level()))
        console_handler.setFormatter(logging.Formatter(
            '%(levelname)-8s %(message)s'
        ))
        console_handler.addFilter(base64_filter)  # Add base64 truncating filter
        root_logger.addHandler(console_handler)

    # Per-module overrides
    for module_name, level in settings.get_module_log_levels().items():
        logging.getLogger(module_name).setLevel(getattr(logging, level))

    # Separate UI (frontend) logger → ~/.dpc/logs/ui.log
    ui_log_file = log_file.parent / "ui.log"
    ui_file_handler = RotatingFileHandler(
        ui_log_file,
        maxBytes=settings.get_log_max_bytes(),
        backupCount=settings.get_log_backup_count(),
        encoding='utf-8'
    )
    ui_file_handler.setLevel(logging.DEBUG)
    ui_file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    ui_logger = logging.getLogger("dpc_ui")
    ui_logger.setLevel(logging.DEBUG)
    ui_logger.addHandler(ui_file_handler)
    ui_logger.propagate = False  # Don't bubble up to root (keep backend log clean)

    # Suppress verbose websocket debug logs (> TEXT, < TEXT, PING, PONG)
    # These come from the websockets library's internal logging when DEBUG is enabled
    for ws_logger in ['websockets.server', 'websockets.protocol', 'websockets']:
        logging.getLogger(ws_logger).setLevel(logging.WARNING)

    # The HF decision is taken at import time, before any of the above exists,
    # so it could only be printed. Repeat it here: which mode a run started in
    # is exactly the thing you want when reading the log days later.
    if _HF_STARTUP_NOTE:
        logger.info(_HF_STARTUP_NOTE)


def dependency_setup():
    """Check GPU/torch status at startup (ADR-012).

    CUDA/ROCm torch is configured via platform markers in pyproject.toml
    [tool.uv.sources]. This function only verifies the result and warns
    if there's a mismatch.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                gpu_name = result.stdout.strip()
                print(f"\n[DPC] NVIDIA GPU detected ({gpu_name}) but torch lacks CUDA.")
                print("[DPC] Try: rm -rf .venv && uv sync\n")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    except ImportError:
        pass


def _parse_args():
    """Parse CLI flags. Env var DPC_SKIP_KNOWLEDGE_INDEX=1 is also honored."""
    parser = argparse.ArgumentParser(
        prog="run_service.py",
        description="D-PC Messenger Core Service",
    )
    parser.add_argument(
        "--skip-knowledge-index",
        action="store_true",
        default=os.environ.get("DPC_SKIP_KNOWLEDGE_INDEX") == "1",
        help=(
            "Skip eager agent memory index rebuild on startup. "
            "Useful for rapid dev restarts — first prompt to any agent will "
            "trigger lazy index init (~2 min for agents with large knowledge "
            "bases). Default: off. Env var: DPC_SKIP_KNOWLEDGE_INDEX=1."
        ),
    )
    return parser.parse_args()


async def main():
    """Main entrypoint to start and run the Core Service."""
    args = _parse_args()

    # GPU status check (ADR-012)
    dependency_setup()

    service = CoreService(skip_knowledge_index=args.skip_knowledge_index)

    # Setup logging infrastructure before service starts
    setup_logging(service.settings)

    # --- THE CORE FIX: Cross-platform shutdown logic ---

    # Create a future that will be used to signal shutdown
    shutdown_future = asyncio.Future()

    # Get event loop and set up exception handler to suppress aioice warnings
    loop = asyncio.get_running_loop()

    # Custom exception handler to suppress known warnings during shutdown
    def exception_handler(loop, context):
        exception = context.get('exception')

        # Suppress CancelledError during shutdown (expected behavior)
        if isinstance(exception, asyncio.CancelledError):
            # This is normal during shutdown - tasks are cancelled
            return

        # Suppress aioice STUN transaction InvalidStateError (known bug in aioice)
        if isinstance(exception, asyncio.exceptions.InvalidStateError):
            message = context.get('message', '')
            if 'Transaction.__retry' in message or 'stun.py' in str(context.get('source_traceback', '')):
                # Silently ignore this known aioice race condition
                return

        # For all other exceptions, use default behavior
        loop.default_exception_handler(context)

    loop.set_exception_handler(exception_handler)

    # On Windows, signal handlers are not supported. We rely on KeyboardInterrupt.
    # On other OSes, we can set up a more graceful signal handler.
    if platform.system() != "Windows":
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: shutdown_future.set_result(s))

    # ----------------------------------------------------

    service_task = asyncio.create_task(service.start())

    try:
        # Wait for either the service to finish (it shouldn't) or for a shutdown signal
        await asyncio.wait([service_task, shutdown_future], return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass # This is expected on shutdown
    finally:
        global _shutting_down
        _shutting_down = True
        logger.info("Shutdown initiated")
        await service.stop()
        # Ensure the main service task is also cancelled
        service_task.cancel()
        try:
            await service_task
        except asyncio.CancelledError:
            pass # Expected
        # Same disease as the default pool below, different pool: every agent
        # tool call runs on the shared tool executor, which nobody stopped
        # either. Bounded here rather than in the helper, so a tool that
        # outran its own timeout cannot decide when the process exits — the
        # thread dump names it instead.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(shutdown_shared_executor),
                timeout=_TOOL_EXECUTOR_SHUTDOWN_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Agent tool executor still had a tool running after %.0fs — "
                "see the thread dump below for which one",
                _TOOL_EXECUTOR_SHUTDOWN_SEC,
            )
        except Exception as e:
            logger.warning("Agent tool executor did not shut down cleanly: %s", e)
        # run_in_executor(None, ...) parks work on asyncio's default pool,
        # whose workers are non-daemon: nobody ever asked them to stop, so
        # nine of them once held the interpreter open after every component
        # had reported clean. Bounded, because the point is to exit.
        try:
            await loop.shutdown_default_executor(
                timeout=_DEFAULT_EXECUTOR_SHUTDOWN_SEC,
            )
        except Exception as e:
            logger.warning("Default executor did not shut down cleanly: %s", e)
        # Named before the loop closes: after this point a stuck overlapped
        # op parks the process inside IocpProactor.close and nothing else
        # reaches the log.
        log_live_non_daemon_threads()

# Set by the shutdown path so the proactor hook below can tell the final
# loop close from the short-lived per-call loops agent tools open and close
# constantly. Same facts either way — only the log level differs.
_shutting_down = False


# Enough of each stuck thread's stack to name the blocking call and who
# asked for it, without turning a shutdown into a core dump.
_STACK_FRAMES_PER_THREAD = 4

# How long the default executor gets to drain before we stop waiting on it.
# Long enough for a hash or an index write to land, short enough that a
# thread parked on a two-minute wait does not decide when we exit.
_DEFAULT_EXECUTOR_SHUTDOWN_SEC = 5.0

# Same for the agent tool pool. Kept short deliberately: anything still
# running here has already blown through its own tool timeout, so waiting
# on it longer buys nothing that the thread dump does not report better.
_TOOL_EXECUTOR_SHUTDOWN_SEC = 5.0


def log_live_non_daemon_threads():
    """Name the non-daemon threads that will hold the interpreter open.

    The other half of a hung exit: even with every overlapped op settled,
    Python waits at exit for each non-daemon thread to return. Daemon
    threads are excluded because they are not what keeps it alive.
    """
    import threading

    main = threading.main_thread()
    alive = [
        t for t in threading.enumerate()
        if t.is_alive() and not t.daemon and t is not main
    ]
    if not alive:
        return
    logger.warning(
        "Shutdown: %d non-daemon thread(s) still alive — the interpreter "
        "waits for each before exiting", len(alive),
    )
    # Names alone say which pool is holding the process, never where it is
    # stuck, so every such exit ended in a guess. The innermost frames say it.
    frames = sys._current_frames()
    for t in alive:
        logger.warning("  thread %r (ident=%s)", t.name, t.ident)
        frame = frames.get(t.ident)
        if frame is None:
            continue
        for line in traceback.format_stack(frame)[-_STACK_FRAMES_PER_THREAD:]:
            logger.warning("    %s", line.rstrip())


def _should_report_pending(pending: int, shutting_down: bool) -> bool:
    """Whether a loop closing with `pending` overlapped ops is worth a line.

    Every proactor loop closes with exactly one outstanding: its own
    self-pipe read, cancelled by close() a moment earlier and not yet
    reaped. Agent tools open and close a loop per call, so reporting that
    one is a line per tool call saying the same nothing.

    Anything past it is the self-pipe plus something real. At shutdown
    everything is named regardless — that is the close that can hang, and
    there the self-pipe is a suspect like any other.
    """
    return pending > 0 and (shutting_down or pending > 1)


# How long the drain is left alone before we hand it the packets it waits
# for, and how long before we stop being careful about which. Both are
# generous: a healthy close drains in milliseconds.
_DRAIN_NUDGE_SECONDS = float(os.environ.get("DPC_SHUTDOWN_DRAIN_NUDGE", "5"))
_DRAIN_GIVE_UP_SECONDS = float(os.environ.get("DPC_SHUTDOWN_DRAIN_GIVE_UP", "20"))


def _post_missing_completions(proactor, done_only: bool) -> int:
    """Hand `IocpProactor.close()` the completion packets it is waiting for.

    Its drain is `while self._cache: self._poll(1)` (`windows_events.py:857`)
    with no timeout at all — the comment above it says «don't exit with
    running overlapped to prevent a crash». An entry leaves that cache only
    when `_poll` dequeues a packet for its address, so one packet that never
    arrives is a process that never exits.

    Measured 2026-08-24: every component reported clean stop in 4.2 s, and
    the loop then spun for the six minutes until Mike ended it by hand,
    holding two entries whose futures read `finished result=548` and `549`
    on already-closed sockets.

    Those two are the shape `_register` documents at `:723-738` — an
    operation that completes synchronously has its result set immediately
    and is *still* cached, because «Even if GetOverlappedResult() was
    called, we have to wait for the notification of the completion in
    GetQueuedCompletionStatus()». Posting that notification ourselves is
    not a trick played on the loop: `_poll` pops the entry and then skips
    the callback, since its guard at `:801` is `elif not f.done()`. Nothing
    is fabricated for a future still waiting on a real answer — that is what
    `done_only` protects, and only the give-up pass drops it.

    Returns the number of packets posted.
    """
    import _overlapped  # Windows-only C module; imported where it is used.

    iocp = getattr(proactor, "_iocp", None)
    cache = getattr(proactor, "_cache", None)
    if iocp is None or not cache:
        return 0

    posted = 0
    for address, entry in list(cache.items()):
        fut = entry[0]
        try:
            settled = fut.done()
        except Exception:  # a stub or a half-torn-down future
            settled = False
        if done_only and not settled:
            continue
        try:
            # key=0 on purpose: _poll's KeyError branch closes a non-zero key
            # as a pipe handle, and there is no handle here to close.
            _overlapped.PostQueuedCompletionStatus(iocp, 0, 0, address)
        except OSError as exc:
            logger.debug("drain nudge failed for overlapped %s: %s", address, exc)
        else:
            posted += 1
    return posted


def _start_drain_watchdog(proactor):
    """Let a loop close finish even when a completion packet never comes.

    Runs beside `IocpProactor.close()`, which is blocking the calling thread
    inside its own drain. `PostQueuedCompletionStatus` is a Win32 call on
    the port handle and is safe to make from here; the packets are dequeued
    by that same blocked `_poll`, so the close finishes through CPython's
    own path rather than by force.
    """
    import threading
    import time

    def run():
        started = time.monotonic()
        nudge_at = started + _DRAIN_NUDGE_SECONDS
        give_up_at = started + max(_DRAIN_GIVE_UP_SECONDS, _DRAIN_NUDGE_SECONDS)
        nudged = False
        while True:
            if getattr(proactor, "_iocp", None) is None:
                return  # close() returned on its own
            cache = getattr(proactor, "_cache", None)
            if not cache:
                return
            now = time.monotonic()
            if not nudged and now >= nudge_at:
                count = _post_missing_completions(proactor, done_only=True)
                nudged = True
                if count:
                    logger.warning(
                        "Shutdown: %d overlapped op(s) had already finished and "
                        "their completion never arrived — posting it so the loop "
                        "can close (waited %.0fs)", count, _DRAIN_NUDGE_SECONDS,
                    )
            elif now >= give_up_at:
                count = _post_missing_completions(proactor, done_only=False)
                if count:
                    logger.warning(
                        "Shutdown: %d overlapped op(s) still unfinished after "
                        "%.0fs — abandoning the wait for them so the process can "
                        "exit. Re-run with DPC_DEBUG_SHUTDOWN=1 to see where they "
                        "were registered.", count, _DRAIN_GIVE_UP_SECONDS,
                    )
                return
            time.sleep(0.25)

    thread = threading.Thread(
        target=run, name="shutdown-drain-watchdog", daemon=True,
    )
    thread.start()
    return thread


def _install_shutdown_diagnostics():
    """Name whatever still holds a Windows overlapped op when a loop closes.

    IocpProactor.close() spins in `while self._cache` until every overlapped
    completes, printing "is running after closing for N seconds" once a
    second. That message says one op is stuck and never which, so every fix
    so far has been aimed at a guess.

    Two halves with very different costs, so they are gated separately:

    * listing what is pending runs once per loop close and costs nothing —
      always on;
    * recording where each op was registered means a formatted stack per
      overlapped registration, i.e. per socket read and write in the
      process — opt-in via DPC_DEBUG_SHUTDOWN=1.

    Without the second half the object alone rarely identifies the op: a
    stuck one leaves a closed socket (fd=-1) behind. The stack has to be
    taken at registration — _OverlappedFuture._source_traceback exists only
    under asyncio debug mode, and a cancelled-but-stuck future is pruned by
    a done-callback before close() ever sees it.
    """
    if platform.system() != "Windows":
        return
    try:
        from asyncio.windows_events import IocpProactor
    except ImportError:
        return

    if getattr(IocpProactor.close, "_dpc_shutdown_diagnostics", False):
        return

    capture_origins = os.environ.get("DPC_DEBUG_SHUTDOWN") == "1"
    origins: dict[int, list[str]] = {}
    original_close = IocpProactor.close

    if capture_origins:
        original_register = IocpProactor._register

        def register_with_origin(self, ov, obj, callback):
            fut = original_register(self, ov, obj, callback)
            address = getattr(ov, "address", None)
            if address is not None:
                origins[address] = traceback.format_stack(limit=14)[:-1]
                if len(origins) > 1000:
                    # Addresses gone from _cache have completed; their
                    # stacks are dead weight.
                    for stale in origins.keys() - self._cache.keys():
                        del origins[stale]
            return fut

        IocpProactor._register = register_with_origin

    def close_with_trace(self):
        cache = getattr(self, "_cache", None)
        worth_reporting = bool(cache) and _should_report_pending(
            len(cache), _shutting_down
        )
        if worth_reporting:
            level = logging.WARNING if _shutting_down else logging.DEBUG
            logger.log(level, "%d overlapped op(s) still pending at loop close", len(cache))
            for address, entry in list(cache.items()):
                # windows_events.py — _cache[ov.address] = (fut, ov, obj, callback)
                fut, _ov, obj, callback = entry
                logger.log(
                    level,
                    "  pending overlapped %s: fut=%r cancelled=%s obj=%r callback=%r",
                    address,
                    fut,
                    fut.cancelled(),
                    obj,
                    getattr(callback, "__qualname__", callback),
                )
                for line in origins.get(address, []):
                    logger.log(level, "    origin: %s", line.rstrip())
            if not capture_origins and _shutting_down:
                logger.warning(
                    "  (start with DPC_DEBUG_SHUTDOWN=1 to also record where "
                    "each op was registered)"
                )
        # The same predicate arms the watchdog: exactly the closes that can
        # spin. A healthy one carries the self-pipe read alone and drains
        # before the watchdog's first tick, so the thread costs one wakeup.
        watchdog = _start_drain_watchdog(self) if worth_reporting else None
        try:
            return original_close(self)
        finally:
            if watchdog is not None:
                # It exits on its own as soon as the cache empties; this only
                # keeps a finished thread from outliving the call in the dump.
                watchdog.join(timeout=0.5)

    # Idempotent: a second install would wrap the wrapper, so every close
    # would run the drain logic twice and a traceback would show the frame
    # twice. Production calls this once; tests call it per case.
    close_with_trace._dpc_shutdown_diagnostics = True
    IocpProactor.close = close_with_trace


if __name__ == "__main__":
    try:
        single_instance.acquire()  # exits if another backend is already running
        _install_shutdown_diagnostics()
        print(f"D-PC Messenger v{__version__} - Starting Core Service (press Ctrl+C to stop)")
        asyncio.run(main())
    except KeyboardInterrupt:
        # This is the primary shutdown mechanism on Windows
        print("Shutdown requested by user (KeyboardInterrupt)")
    finally:
        single_instance.release()
