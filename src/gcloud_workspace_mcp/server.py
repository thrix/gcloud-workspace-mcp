"""MCP server wiring: tool registration, the write gate and the stdio entry point.

Some Google Workspace tenants block every third-party OAuth client. Published
Workspace MCP servers each ship their own OAuth client id, so they all fail
there. Google's own Cloud SDK client stays allowed, so this server never runs
an OAuth flow: it borrows the tokens minted by the local `gcloud` CLI and calls
the Google REST APIs directly over HTTPS.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, drive, gmail

# Set to "1" to register the write tools. Unset, the client never sees them.
ENABLE_WRITES_ENV = "GCLOUD_WORKSPACE_ENABLE_WRITES"

READ_TOOLS: list[Callable[..., str]] = [
    drive.drive_search,
    drive.drive_get_metadata,
    drive.docs_export,
    drive.drive_download,
    gmail.gmail_search,
    gmail.gmail_get_message,
    gmail.gmail_get_thread,
]

WRITE_TOOLS: list[Callable[..., str]] = [
    drive.docs_create_from_markdown,
    drive.docs_replace_content,
    drive.docs_append_text,
    gmail.gmail_create_draft,
]

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
# Replacing a document's content overwrites what was there.
_OVERWRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def writes_enabled() -> bool:
    return os.environ.get(ENABLE_WRITES_ENV, "").strip() == "1"


def build_server(*, enable_writes: bool | None = None) -> MCPServer:
    """Create the server with the read tools, plus the write tools if enabled."""
    if enable_writes is None:
        enable_writes = writes_enabled()
    server = MCPServer("gcloud-workspace-mcp", version=__version__)
    for fn in READ_TOOLS:
        server.add_tool(fn, annotations=_READ)
    if enable_writes:
        for fn in WRITE_TOOLS:
            annotations = _OVERWRITE if fn is drive.docs_replace_content else _WRITE
            server.add_tool(fn, annotations=annotations)
    return server


def main() -> None:
    """Run the MCP server over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
