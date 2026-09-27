from __future__ import annotations

import urllib.error

import pytest
from conftest import SENTINEL_TOKEN, FakeGcloud, FakeGoogle, FakeResponse, http_error

from gcloud_workspace_mcp import auth, http
from gcloud_workspace_mcp.auth import WorkspaceError

URL = "https://www.googleapis.com/drive/v3/files"


def test_attaches_bearer_token(google: FakeGoogle) -> None:
    google.queue_json({"ok": True})
    assert http.get_json(URL, token_cache=auth.USER_TOKEN) == {"ok": True}
    assert google.requests[0].get_header("Authorization") == f"Bearer {SENTINEL_TOKEN}"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.googleapis.com/drive/v3/files",
        "https://evil.example.com/drive/v3/files",
        "https://googleapis.com.evil.example.com/x",
        "https://evilgoogleapis.com/x",
        "https://www.googleapis.com@evil.example.com/x",
        "file:///etc/passwd",
    ],
)
def test_host_guard_refuses_non_google(url: str, google: FakeGoogle, gcloud: FakeGcloud) -> None:
    with pytest.raises(WorkspaceError, match="Refusing to send credentials"):
        http.request(url, token_cache=auth.USER_TOKEN)
    assert google.requests == []
    # The guard runs before a token is even minted.
    assert gcloud.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.googleapis.com/drive/v3/files",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        "https://docs.googleapis.com/v1/documents/x:batchUpdate",
        "https://WWW.GOOGLEAPIS.COM/drive/v3/files",
    ],
)
def test_host_guard_allows_google(url: str) -> None:
    http.check_url(url)


def test_401_refreshes_and_retries_once(google: FakeGoogle, gcloud: FakeGcloud) -> None:
    gcloud.tokens = ["stale", "fresh"]
    google.queue(http_error(URL, 401), FakeResponse(b'{"ok": 1}'))
    assert http.get_json(URL, token_cache=auth.USER_TOKEN) == {"ok": 1}
    assert [r.get_header("Authorization") for r in google.requests] == [
        "Bearer stale",
        "Bearer fresh",
    ]


def test_second_401_is_an_error(google: FakeGoogle) -> None:
    google.queue(http_error(URL, 401, "Invalid Credentials"), http_error(URL, 401, "Nope"))
    with pytest.raises(WorkspaceError, match="401: Nope"):
        http.request(URL, token_cache=auth.USER_TOKEN)
    assert len(google.requests) == 2


def test_403_quota_hint(google: FakeGoogle) -> None:
    google.queue(http_error(URL, 403, "Gmail API has not been used ... accessNotConfigured"))
    with pytest.raises(WorkspaceError, match="GCLOUD_QUOTA_PROJECT"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_403_store_hint(google: FakeGoogle) -> None:
    google.queue(http_error(URL, 403, "Request had insufficient authentication scopes."))
    with pytest.raises(WorkspaceError, match="wrong credential store"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_network_error(google: FakeGoogle) -> None:
    google.queue(urllib.error.URLError("Name or service not known"))
    with pytest.raises(WorkspaceError, match="Network error"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_send_json_sets_method_and_body(google: FakeGoogle) -> None:
    google.queue_json({"id": "1"})
    http.send_json(URL, token_cache=auth.USER_TOKEN, method="PATCH", data=b"{}")
    req = google.requests[0]
    assert req.get_method() == "PATCH"
    assert req.data == b"{}"
    assert req.get_header("Content-type") == "application/json; charset=UTF-8"


def test_gmail_headers_require_quota_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCLOUD_QUOTA_PROJECT", raising=False)
    with pytest.raises(WorkspaceError, match="GCLOUD_QUOTA_PROJECT is not set"):
        http.gmail_headers()
    monkeypatch.setenv("GCLOUD_QUOTA_PROJECT", "proj")
    assert http.gmail_headers() == {"x-goog-user-project": "proj"}
