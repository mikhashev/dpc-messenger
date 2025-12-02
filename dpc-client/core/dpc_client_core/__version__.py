"""D-PC Messenger version information."""
from pathlib import Path

def get_version() -> str:
    """Read version from VERSION file."""
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        return version_file.read_text().strip()
    except Exception:
        return "unknown"

__version__ = get_version()
