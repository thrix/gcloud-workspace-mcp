"""Authenticated HTTPS calls to the Google REST APIs (stdlib only)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .auth import TokenCache, WorkspaceError

DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DOCS_BASE = "https://docs.googleapis.com/v1"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

_ALLOWED_HOST_SUFFIX = ".googleapis.com"


def check_url(url: str) -> None:
    """Refuse any URL a token must not be attached to.

    Only ``https`` URLs whose host ends in ``.googleapis.com`` pass. This turns
    the "tokens are never sent to a third party" promise into a hard check.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(_ALLOWED_HOST_SUFFIX):
        raise WorkspaceError(
            f"Refusing to send credentials to {parts.scheme}://{host}: only "
            f"https://*{_ALLOWED_HOST_SUFFIX} is allowed."
        )


def request(
    url: str,
    *,
    token_cache: TokenCache,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Perform an authenticated request and return ``(body, content_type)``.

    Adds ``Authorization: Bearer <token>`` from ``token_cache``. On a 401 the
    token is force-refreshed and the request retried exactly once. HTTP errors
    are surfaced with Google's status + message but never the token.
    """
    # Checked before the token is even fetched.
    check_url(url)
    extra_headers = extra_headers or {}

    def _do(force_refresh: bool) -> tuple[bytes, str]:
        token = token_cache.get(force_refresh=force_refresh)
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        # The token is attached to the outbound Google request only.
        req.add_header("Authorization", f"Bearer {token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        for key, value in extra_headers.items():
            req.add_header(key, value)
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — URL checked above
            return resp.read(), resp.headers.get_content_type()

    try:
        return _do(force_refresh=False)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Token likely expired earlier than assumed — refresh and retry once.
            try:
                return _do(force_refresh=True)
            except urllib.error.HTTPError as exc2:
                raise http_error(exc2) from None
            except urllib.error.URLError as exc2:
                raise WorkspaceError(f"Network error contacting Google: {exc2.reason}") from None
        raise http_error(exc) from None
    except urllib.error.URLError as exc:
        raise WorkspaceError(f"Network error contacting Google: {exc.reason}") from None


def http_error(exc: urllib.error.HTTPError) -> WorkspaceError:
    """Turn an HTTPError into a WorkspaceError, extracting Google's message.

    The response body from googleapis.com contains an error description but
    never the request token, so it is safe to include.
    """
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        detail = payload.get("error", {}).get("message", "") or ""
    except Exception:  # noqa: BLE001 — best-effort extraction
        detail = ""
    hint = ""
    if exc.code == 403 and "accessNotConfigured" in detail:
        hint = (
            " (hint: set GCLOUD_QUOTA_PROJECT and run "
            "`gcloud auth application-default set-quota-project <project>`)"
        )
    elif exc.code == 403:
        hint = (
            " (hint: a 403 often means the wrong credential store or a missing "
            "scope — Docs/Drive use user creds, Gmail uses ADC)"
        )
    return WorkspaceError(f"Google API error {exc.code}: {detail or exc.reason}{hint}")


def get_json(
    url: str, *, token_cache: TokenCache, extra_headers: dict[str, str] | None = None
) -> Any:
    body, _ = request(url, token_cache=token_cache, extra_headers=extra_headers)
    return json.loads(body.decode("utf-8", "replace"))


def send_json(
    url: str,
    *,
    token_cache: TokenCache,
    method: str = "POST",
    data: bytes,
    content_type: str = "application/json; charset=UTF-8",
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """Send a request body (POST/PATCH/upload) and decode the JSON response."""
    body, _ = request(
        url,
        token_cache=token_cache,
        method=method,
        data=data,
        content_type=content_type,
        extra_headers=extra_headers,
    )
    return json.loads(body.decode("utf-8", "replace")) if body else {}


def gmail_headers() -> dict[str, str]:
    """Return the mandatory quota-project header for Gmail/ADC calls."""
    quota_project = os.environ.get("GCLOUD_QUOTA_PROJECT", "").strip()
    if not quota_project:
        raise WorkspaceError(
            "GCLOUD_QUOTA_PROJECT is not set. Gmail calls require it; set it in "
            "the MCP server env to your ADC quota project."
        )
    return {"x-goog-user-project": quota_project}


def quote(value: str) -> str:
    """Percent-encode a path segment such as a file or message id."""
    return urllib.parse.quote(value, safe="")
