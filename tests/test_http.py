from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from conftest import SENTINEL_TOKEN, FakeGcloud, FakeGoogle, FakeResponse, http_error

from gcloud_workspace_mcp import auth, http
from gcloud_workspace_mcp.auth import WorkspaceError

URL = "https://www.googleapis.com/drive/v3/files"
QUOTA = {"x-goog-user-project": "test-quota-project"}


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
    with pytest.raises(WorkspaceError, match="enable the API in the GCLOUD_QUOTA_PROJECT"):
        http.request(URL, token_cache=auth.ADC_TOKEN, extra_headers=QUOTA)


def test_403_disabled_api_without_quota_header(google: FakeGoogle) -> None:
    google.queue(
        http_error(URL, 403, "Docs API ... disabled", errors=[{"reason": "SERVICE_DISABLED"}])
    )
    with pytest.raises(WorkspaceError, match="GCLOUD_QUOTA_PROJECT does not apply") as info:
        http.request(URL, token_cache=auth.USER_TOKEN)
    assert "set-quota-project" not in str(info.value)


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


def test_network_error_on_retry(google: FakeGoogle) -> None:
    google.queue(http_error(URL, 401), urllib.error.URLError("connection reset"))
    with pytest.raises(WorkspaceError, match="Network error contacting Google: connection reset"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_error_body_not_json_falls_back_to_reason(google: FakeGoogle) -> None:
    import io
    from email.message import Message

    google.queue(urllib.error.HTTPError(URL, 502, "Bad Gateway", Message(), io.BytesIO(b"<html>")))
    with pytest.raises(WorkspaceError, match=r"^Google API error 502: Bad Gateway$"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_error_without_message_falls_back_to_reason(google: FakeGoogle) -> None:
    google.queue(http_error(URL, 404, "", reason="Not Found"))
    with pytest.raises(WorkspaceError, match=r"^Google API error 404: Not Found$"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_extra_headers_and_content_type(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"", "text/plain"))
    body, content_type = http.request(
        URL,
        token_cache=auth.USER_TOKEN,
        method="POST",
        data=b"x",
        content_type="text/plain",
        extra_headers={"X-Test": "1"},
    )
    assert (body, content_type) == (b"", "text/plain")
    req = google.requests[0]
    assert req.get_header("X-test") == "1"
    assert req.get_header("Content-type") == "text/plain"


def test_send_json_empty_body(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b""))
    assert http.send_json(URL, token_cache=auth.USER_TOKEN, data=b"{}") == {}


@pytest.mark.parametrize("value", ["1AbC-_x9", "18c2f3a4b5d6e7f8"])
def test_resource_id_accepts_ids(value: str) -> None:
    assert http.resource_id(value) == value


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a?b", "a#b", "a%2Fb", "a b", "a\n"])
def test_resource_id_refuses_other_values(value: str) -> None:
    with pytest.raises(WorkspaceError, match=r"file_id .* is not a valid id"):
        http.resource_id(value, name="file_id")


def _redirect(new_url: str) -> urllib.request.Request | None:
    import io
    from email.message import Message

    req = urllib.request.Request(URL)
    req.add_header("Authorization", f"Bearer {SENTINEL_TOKEN}")
    handler = http._GuardedRedirectHandler()
    return handler.redirect_request(req, io.BytesIO(), 302, "Found", Message(), new_url)


@pytest.mark.parametrize(
    "url", ["https://evil.example.com/x", "http://www.googleapis.com/drive/v3/files"]
)
def test_redirect_to_non_google_is_refused(url: str) -> None:
    with pytest.raises(WorkspaceError, match="Refusing to send credentials"):
        _redirect(url)


def test_redirect_to_google_is_followed() -> None:
    new = _redirect("https://www.googleapis.com/drive/v3/files/moved")
    assert new is not None
    assert new.full_url == "https://www.googleapis.com/drive/v3/files/moved"


def test_opener_uses_guarded_redirects() -> None:
    assert any(isinstance(h, http._GuardedRedirectHandler) for h in http._OPENER.handlers)


def test_request_passes_timeout(google: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def urlopen(req: urllib.request.Request, *args: object, **kwargs: object) -> FakeResponse:
        seen.append(kwargs.get("timeout"))
        return FakeResponse(b"{}")

    monkeypatch.setattr(http._OPENER, "open", urlopen)
    http.request(URL, token_cache=auth.USER_TOKEN)
    assert seen == [http.REQUEST_TIMEOUT_SECONDS]


def test_timeout_is_reported(google: FakeGoogle) -> None:
    google.queue(TimeoutError("timed out"))
    with pytest.raises(WorkspaceError, match="did not respond within 60 seconds"):
        http.request(URL, token_cache=auth.USER_TOKEN)


def test_max_bytes(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"12345"), FakeResponse(b"123456"))
    assert http.request(URL, token_cache=auth.USER_TOKEN, max_bytes=5)[0] == b"12345"
    with pytest.raises(WorkspaceError, match="over the 5-byte limit"):
        http.request(URL, token_cache=auth.USER_TOKEN, max_bytes=5)


@pytest.mark.parametrize(
    "error",
    [
        {"errors": [{"domain": "usageLimits", "reason": "accessNotConfigured"}]},
        {
            "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}
            ]
        },
    ],
)
def test_403_quota_hint_from_reason(google: FakeGoogle, error: dict[str, object]) -> None:
    message = "Gmail API has not been used in project 123 before or it is disabled."
    google.queue(http_error(URL, 403, message, **error))
    with pytest.raises(WorkspaceError, match="enable the API in the GCLOUD_QUOTA_PROJECT"):
        http.request(URL, token_cache=auth.ADC_TOKEN, extra_headers=QUOTA)


@pytest.mark.parametrize(
    "exc",
    [ConnectionResetError("reset by peer"), http.HTTPException("truncated")],
)
def test_read_errors_are_reported(google: FakeGoogle, exc: Exception) -> None:
    google.queue(exc)
    with pytest.raises(WorkspaceError, match="Network error contacting Google") as info:
        http.request(URL, token_cache=auth.USER_TOKEN)
    assert SENTINEL_TOKEN not in str(info.value)


@pytest.mark.parametrize("call", [http.get_json, http.send_json])
def test_non_json_body_is_reported(google: FakeGoogle, call: object) -> None:
    google.queue(FakeResponse(b"<html>portal</html>", "text/html"))
    kwargs = {"data": b"{}"} if call is http.send_json else {}
    with pytest.raises(WorkspaceError, match="not JSON"):
        call(URL, token_cache=auth.USER_TOKEN, **kwargs)  # type: ignore[operator]
