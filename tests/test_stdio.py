"""Start the real server process over stdio, with a fake `gcloud` on PATH."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from gcloud_workspace_mcp import server

FAKE_GCLOUD = """#!{python}
import sys
sys.stderr.write("fake gcloud: reauthentication required\\n")
sys.exit(1)
"""


@pytest.fixture
def fake_gcloud_path(tmp_path: Path) -> str:
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.format(python=sys.executable))
    gcloud.chmod(0o755)
    return f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"


def _run(env: dict[str, str], call: tuple[str, dict] | None = None):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "gcloud_workspace_mcp.server"], env=env
    )

    async def go():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool(*call) if call else None
            return tools, result

    return asyncio.run(go())


def test_stdio_lists_read_tools_only(fake_gcloud_path: str) -> None:
    tools, _ = _run({"PATH": fake_gcloud_path})
    assert tools == {fn.__name__ for fn in server.READ_TOOLS}


def test_stdio_lists_write_tools_when_enabled(fake_gcloud_path: str) -> None:
    tools, _ = _run({"PATH": fake_gcloud_path, server.ENABLE_WRITES_ENV: "1"})
    assert tools == {fn.__name__ for fn in server.READ_TOOLS + server.WRITE_TOOLS}


def test_stdio_gcloud_failure_reaches_client(fake_gcloud_path: str) -> None:
    _, result = _run({"PATH": fake_gcloud_path}, ("drive_search", {"query": "x"}))
    assert result.is_error
    text = "".join(getattr(part, "text", "") for part in result.content)
    assert "gcloud failed to mint an access token (exit 1)" in text
    assert "reauthentication required" in text
