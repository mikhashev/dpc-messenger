# dpc-client/core/dpc_client_core/file_server.py

import asyncio
import logging
from pathlib import Path
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer
import threading
from urllib.parse import unquote
import mimetypes

logger = logging.getLogger(__name__)


class FileServerHandler(SimpleHTTPRequestHandler):
    """HTTP handler for serving conversation files to browser."""

    def __init__(self, *args, dpc_home_dir: Path = None, **kwargs):
        self.dpc_home_dir = dpc_home_dir
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Handle GET requests for files."""
        # Parse path: /files/{peer_id}/{filename}
        path_parts = unquote(self.path).strip('/').split('/')

        if len(path_parts) != 3 or path_parts[0] != 'files':
            self.send_error(404, "Invalid path format. Use /files/{peer_id}/{filename}")
            return

        _, peer_id, filename = path_parts

        # Construct file path
        file_path = self.dpc_home_dir / "conversations" / peer_id / "files" / filename

        # Security: Verify path is within allowed directory
        try:
            file_path = file_path.resolve()
            conversations_dir = (self.dpc_home_dir / "conversations").resolve()
            if not str(file_path).startswith(str(conversations_dir)):
                logger.warning(f"Path traversal attempt blocked: {file_path}")
                self.send_error(403, "Access denied")
                return
        except Exception as e:
            logger.error(f"Path resolution error: {e}")
            self.send_error(400, "Invalid path")
            return

        # Check if file exists
        if not file_path.exists():
            logger.debug(f"File not found: {file_path}")
            self.send_error(404, "File not found")
            return

        # Serve file
        try:
            # Determine MIME type
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if mime_type is None:
                mime_type = 'application/octet-stream'

            # Read file
            with open(file_path, 'rb') as f:
                content = f.read()

            # Send response
            self.send_response(200)
            self.send_header('Content-Type', mime_type)
            self.send_header('Content-Length', len(content))
            self.send_header('Access-Control-Allow-Origin', '*')  # Allow CORS for local dev
            self.send_header('Cache-Control', 'public, max-age=31536000')  # Cache for 1 year
            self.end_headers()
            self.wfile.write(content)

            logger.debug(f"Served file: {filename} ({len(content)} bytes, {mime_type})")

        except Exception as e:
            logger.error(f"Error serving file {file_path}: {e}")
            self.send_error(500, f"Error serving file: {e}")

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        logger.debug(f"{self.address_string()} - {format % args}")


class FileServer:
    """Simple HTTP server for serving conversation files to browser."""

    def __init__(self, dpc_home_dir: Path, host: str = "127.0.0.1", port: int = 9998):
        self.dpc_home_dir = dpc_home_dir
        self.host = host
        self.port = port
        self.server = None
        self.server_thread = None

    def start(self):
        """Start the file server in a background thread."""
        def handler(*args, **kwargs):
            return FileServerHandler(*args, dpc_home_dir=self.dpc_home_dir, **kwargs)

        try:
            self.server = TCPServer((self.host, self.port), handler)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.server_thread.start()
            logger.info(f"File server started on http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start file server: {e}")
            raise

    def stop(self):
        """Stop the file server."""
        if self.server:
            logger.info("Stopping file server...")
            self.server.shutdown()
            self.server.server_close()
            if self.server_thread:
                self.server_thread.join(timeout=5)
            logger.info("File server stopped")
