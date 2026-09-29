"""Save tool output to disk when it is too big or too binary for the tool result."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .auth import WorkspaceError

# Files go to this directory instead of into the tool result, where base64 or
# a huge JSON resource would blow through the client's output limit. The
# client can read the saved file itself; Claude Code's Read opens PDFs and images.
DOWNLOAD_DIR_ENV = "GCLOUD_WORKSPACE_DOWNLOAD_DIR"

# Refuse to download anything larger than this.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024


def download_dir() -> Path:
    """Return the directory files are saved to."""
    configured = os.environ.get(DOWNLOAD_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME", "").strip() or "~/.cache"
    return Path(cache).expanduser() / "gcloud-workspace-mcp" / "downloads"


def save_file(file_name: str, body: bytes) -> Path:
    """Write ``body`` to ``file_name`` in the download directory and return its path.

    ``file_name`` must be built from validated ids, never from a name a sender
    or a file's owner chose.
    """
    directory = download_dir()
    path = directory / file_name
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Written to a private temporary file first, so a reader never sees a
        # half-written file.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".download-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(body)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as exc:
        raise WorkspaceError(
            f"Could not save the file to {directory}: {exc.strerror or exc}. "
            f"Set {DOWNLOAD_DIR_ENV} to a writable directory."
        ) from None
    return path
