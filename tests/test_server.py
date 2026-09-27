from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from conftest import SENTINEL_TOKEN, FakeGoogle, FakeResponse, http_error

from gcloud_workspace_mcp import drive, gmail, server
from gcloud_workspace_mcp.auth import WorkspaceError

WRITE_NAMES = {fn.__name__ for fn in server.WRITE_TOOLS}
READ_NAMES = {fn.__name__ for fn in server.READ_TOOLS}


def _tool_names(srv) -> set[str]:
    return {t.name for t in asyncio.run(srv.list_tools())}


def test_writes_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(server.ENABLE_WRITES_ENV, raising=False)
    assert _tool_names(server.build_server()) == READ_NAMES


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_writes_need_exactly_1(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(server.ENABLE_WRITES_ENV, value)
    assert _tool_names(server.build_server()) == READ_NAMES


def test_writes_present_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.ENABLE_WRITES_ENV, "1")
    assert _tool_names(server.build_server()) == READ_NAMES | WRITE_NAMES


def test_annotations() -> None:
    tools = {t.name: t for t in asyncio.run(server.build_server(enable_writes=True).list_tools())}
    for name in READ_NAMES:
        assert tools[name].annotations.read_only_hint is True
    for name in WRITE_NAMES:
        assert tools[name].annotations.read_only_hint is False
    assert tools["docs_replace_content"].annotations.destructive_hint is True


# Minimal arguments for every tool, used by the leak test below.
_ARGS = {
    "drive_search": {"query": "x"},
    "drive_get_metadata": {"file_id": "f"},
    "docs_export": {"file_id": "f"},
    "drive_download": {"file_id": "f"},
    "gmail_search": {"query": "x"},
    "gmail_get_message": {"message_id": "m"},
    "gmail_get_thread": {"thread_id": "t"},
    "docs_create_from_markdown": {"title": "t", "markdown": "m"},
    "docs_replace_content": {"file_id": "f", "markdown": "m"},
    "docs_append_text": {"file_id": "f", "text": "t"},
    "gmail_create_draft": {"to": "a@x", "subject": "s", "body": "b"},
}

ALL_TOOLS = server.READ_TOOLS + server.WRITE_TOOLS


def test_leak_table_covers_every_tool() -> None:
    assert set(_ARGS) == READ_NAMES | WRITE_NAMES


_GENERIC = {"id": "x", "files": [], "messages": [], "message": {}, "documentId": "x"}


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
def test_token_never_in_success_output(fn, google: FakeGoogle) -> None:
    if fn is drive.drive_download:
        google.queue_json({"id": "f", "name": "a", "mimeType": "application/pdf", "size": "1"})
    google.queue(*[FakeResponse(json.dumps(_GENERIC).encode()) for _ in range(3)])
    out = fn(**_ARGS[fn.__name__])
    assert SENTINEL_TOKEN not in out
    # Every request carried the token, so it was in play during the call.
    assert all(SENTINEL_TOKEN in r.get_header("Authorization") for r in google.requests)


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("code", [400, 401, 403, 404, 500])
def test_token_never_in_errors(fn, code: int, google: FakeGoogle) -> None:
    url = "https://www.googleapis.com/x"
    google.queue(*[http_error(url, code, f"failed {code}") for _ in range(4)])
    with pytest.raises(WorkspaceError) as info:
        fn(**_ARGS[fn.__name__])
    assert SENTINEL_TOKEN not in str(info.value)
    assert SENTINEL_TOKEN not in repr(info.value)


def test_tools_are_documented() -> None:
    for fn in ALL_TOOLS:
        doc = inspect.getdoc(fn) or ""
        assert "Args:" in doc and "Returns:" in doc, fn.__name__


def test_token_cache_repr_hides_token(google: FakeGoogle) -> None:
    from gcloud_workspace_mcp import auth

    auth.USER_TOKEN.get()
    assert SENTINEL_TOKEN not in repr(auth.USER_TOKEN)
    assert gmail.ADC_TOKEN is auth.ADC_TOKEN
