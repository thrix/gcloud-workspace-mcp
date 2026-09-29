"""Authenticated HTTPS calls to the Google REST APIs (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from typing import Any

from .auth import TokenCache, WorkspaceError

DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DOCS_BASE = "https://docs.googleapis.com/v1"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

_ALLOWED_HOST_SUFFIX = ".googleapis.com"

# Gmail calls must bill this header's project and Docs calls bill it when it is set.
# Drive calls never send it.
QUOTA_PROJECT_HEADER = "x-goog-user-project"
QUOTA_PROJECT_ENV = "GCLOUD_QUOTA_PROJECT"

# Drive file ids and Gmail message and thread ids use only these characters.
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]+")

# Give up on a Google request that stalls for this long.
REQUEST_TIMEOUT_SECONDS = 60

# Error reasons Google returns when the API is disabled for the billed project.
_QUOTA_PROJECT_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}


class ResponseTooLarge(WorkspaceError):
    """The response body went over the caller's ``max_bytes``."""


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


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when `check_url` allows the target.

    urllib copies the ``Authorization`` header onto the redirected request, so
    an unchecked redirect would hand the token to whatever host Google names.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            check_url(newurl)
        except WorkspaceError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirectHandler)


def request(
    url: str,
    *,
    token_cache: TokenCache,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    """Perform an authenticated request and return ``(body, content_type)``.

    Adds ``Authorization: Bearer <token>`` from ``token_cache``. On a 401 the
    token is force-refreshed and the request retried exactly once. HTTP errors
    are surfaced with Google's status + message but never the token. A body
    longer than ``max_bytes`` is refused.
    """
    # Checked before the token is even fetched.
    check_url(url)
    extra_headers = extra_headers or {}
    sent_quota_project = any(key.lower() == QUOTA_PROJECT_HEADER for key in extra_headers)

    def _do(force_refresh: bool) -> tuple[bytes, str]:
        token = token_cache.get(force_refresh=force_refresh)
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        # The token is attached to the outbound Google request only.
        req.add_header("Authorization", f"Bearer {token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        for key, value in extra_headers.items():
            req.add_header(key, value)
        # URL checked above; redirects are checked by `_GuardedRedirectHandler`.
        with _OPENER.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if max_bytes is None:
                body: bytes = resp.read()
            else:
                body = resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ResponseTooLarge(f"Response is over the {max_bytes}-byte limit.")
            return body, resp.headers.get_content_type()

    try:
        try:
            return _do(force_refresh=False)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            # Token likely expired earlier than assumed — refresh and retry once.
            return _do(force_refresh=True)
    except urllib.error.HTTPError as exc:
        raise http_error(exc, sent_quota_project=sent_quota_project) from None
    except urllib.error.URLError as exc:
        raise WorkspaceError(f"Network error contacting Google: {exc.reason}") from None
    except TimeoutError:
        raise WorkspaceError(
            f"Google did not respond within {REQUEST_TIMEOUT_SECONDS} seconds."
        ) from None
    except (OSError, HTTPException) as exc:
        # A connection reset or a truncated body while reading the response.
        raise WorkspaceError(
            f"Network error contacting Google: {type(exc).__name__}: {exc}"
        ) from None


def http_error(exc: urllib.error.HTTPError, *, sent_quota_project: bool) -> WorkspaceError:
    """Turn an HTTPError into a WorkspaceError, extracting Google's message.

    The response body from googleapis.com contains an error description but
    never the request token, so it is safe to include. ``sent_quota_project``
    says whether the request carried the quota-project header, which decides
    the hint for a disabled API.
    """
    detail = ""
    reasons: set[str] = set()
    try:
        error = json.loads(exc.read().decode("utf-8", "replace")).get("error", {})
        detail = error.get("message", "") or ""
        # `errors[].reason` (v1 style) and `details[].reason` (ErrorInfo) name the cause.
        reasons = {
            item.get("reason", "")
            for item in (error.get("errors") or []) + (error.get("details") or [])
        }
    except Exception:  # noqa: BLE001 — best-effort extraction
        reasons = set()
    hint = ""
    api_disabled = bool(reasons & _QUOTA_PROJECT_REASONS) or "accessNotConfigured" in detail
    if exc.code == 403 and api_disabled and sent_quota_project:
        hint = (
            " (hint: enable the API in the GCLOUD_QUOTA_PROJECT project and run "
            "`gcloud auth application-default set-quota-project <project>`)"
        )
    elif exc.code == 403 and api_disabled:
        hint = (
            " (hint: Drive and Docs calls use the gcloud user credentials and bill "
            "the Cloud SDK's own project, so GCLOUD_QUOTA_PROJECT does not apply; "
            "that project does not allow this API)"
        )
    elif exc.code == 403:
        hint = (
            " (hint: a 403 often means the wrong credential store or a missing "
            "scope — Docs/Drive use user creds, Gmail uses ADC)"
        )
    return WorkspaceError(f"Google API error {exc.code}: {detail or exc.reason}{hint}")


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        # A proxy or captive portal can answer 200 with an HTML page.
        raise WorkspaceError("Google returned a response that is not JSON.") from None


def get_json(
    url: str,
    *,
    token_cache: TokenCache,
    extra_headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> Any:
    body, _ = request(
        url, token_cache=token_cache, extra_headers=extra_headers, max_bytes=max_bytes
    )
    return _decode_json(body)


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
    return _decode_json(body) if body else {}


def _quota_project() -> str:
    return os.environ.get(QUOTA_PROJECT_ENV, "").strip()


def gmail_headers() -> dict[str, str]:
    """Return the mandatory quota-project header for Gmail/ADC calls."""
    quota_project = _quota_project()
    if not quota_project:
        raise WorkspaceError(
            f"{QUOTA_PROJECT_ENV} is not set. Gmail calls require it; set it in "
            "the MCP server env to your ADC quota project."
        )
    return {QUOTA_PROJECT_HEADER: quota_project}


def resource_id(value: str, *, name: str = "id") -> str:
    """Check that ``value`` is a bare Drive or Gmail id and return it.

    Percent-encoding would still let ``.`` and ``..`` through, and Google
    redirects those to the parent path on the same host, token included.
    """
    if not _RESOURCE_ID.fullmatch(value):
        raise WorkspaceError(
            f"{name} {value!r} is not a valid id; it may only contain letters, "
            "digits, '-' and '_'."
        )
    return value
