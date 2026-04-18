# dpc-client/core/run_service.py

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import platform  # Import the platform module to check the OS
import subprocess
import sys
import re
from pathlib import Path
from dpc_client_core.service import CoreService
from dpc_client_core.__version__ import __version__

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
    """Generate .env with UV_EXTRA_INDEX_URL based on device_context.json (ADR-012)."""
    import hashlib
    import json

    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    context_path = Path.home() / ".dpc" / "device_context.json"

    if not context_path.exists():
        return

    try:
        context_bytes = context_path.read_bytes()
        context_hash = hashlib.sha256(context_bytes).hexdigest()[:12]

        if env_path.exists():
            first_line = env_path.read_text(encoding="utf-8").split("\n", 1)[0]
            if f"device_context_hash: {context_hash}" in first_line:
                return  # .env is current

        context = json.loads(context_bytes)
        gpu = context.get("hardware", {}).get("gpu", {})
        gpu_type = gpu.get("type", "")
        cuda_version = gpu.get("cuda_version", "")

        index_url = None
        if gpu_type == "nvidia" and cuda_version:
            major, minor = map(int, cuda_version.split(".")[:2])
            if major == 11:
                index_url = "https://download.pytorch.org/whl/cu118"
            elif major == 12 and minor < 4:
                index_url = "https://download.pytorch.org/whl/cu121"
            elif major >= 12:
                index_url = "https://download.pytorch.org/whl/cu124"
        elif gpu_type == "amd":
            index_url = "https://download.pytorch.org/whl/rocm6.2"

        lines = [f"# device_context_hash: {context_hash}"]
        if index_url:
            lines.append(f"UV_EXTRA_INDEX_URL={index_url}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if index_url:
            print(f"\n[DPC] GPU detected ({gpu_type}). .env updated with {index_url}")
            print("[DPC] Run 'uv sync' to install GPU-accelerated torch.\n")

    except Exception:
        pass


async def main():
    """Main entrypoint to start and run the Core Service."""
    # Generate .env with GPU-specific torch index if needed (ADR-012)
    dependency_setup()

    service = CoreService()

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
        print(f"D-PC Messenger v{__version__} - Starting Core Service (press Ctrl+C to stop)")
        asyncio.run(main())
    except KeyboardInterrupt:
        # This is the primary shutdown mechanism on Windows
        print("Shutdown requested by user (KeyboardInterrupt)")
