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
try:
    _hf_offline = _hf_cfg.getboolean("hf", "offline_mode", fallback=False)
except (ValueError, _hf_configparser.Error):
    _hf_offline = False
if _hf_offline:
    # setdefault so an explicit HF_HUB_OFFLINE env var still wins.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    print(
        "[startup] HF_HUB_OFFLINE=1 (from [hf] offline_mode=true) — "
        "transformers/sentence-transformers/gliner will skip HF Hub HEAD "
        "requests and read directly from ~/.cache/huggingface/"
    )

import argparse
import platform  # Import the platform module to check the OS
import subprocess
import sys
import re
from pathlib import Path
from dpc_client_core.service import CoreService
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
        logger.info("Shutdown initiated")
        await service.stop()
        # Ensure the main service task is also cancelled
        service_task.cancel()
        try:
            await service_task
        except asyncio.CancelledError:
            pass # Expected

if __name__ == "__main__":
    try:
        single_instance.acquire()  # exits if another backend is already running
        print(f"D-PC Messenger v{__version__} - Starting Core Service (press Ctrl+C to stop)")
        asyncio.run(main())
    except KeyboardInterrupt:
        # This is the primary shutdown mechanism on Windows
        print("Shutdown requested by user (KeyboardInterrupt)")
    finally:
        single_instance.release()
