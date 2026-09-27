"""Gmail tools. All of them use *ADC* credentials plus the quota-project header."""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, ClassVar

from .auth import ADC_TOKEN, WorkspaceError
from .http import GMAIL_BASE, get_json, gmail_headers, quote, send_json

# Headers kept by the "text" format.
TEXT_HEADERS = ("From", "To", "Cc", "Date", "Subject")

# Bodies longer than this are cut, so one newsletter can't flood the context.
MAX_BODY_CHARS = 50_000

_RAW_FORMATS = {"metadata", "minimal", "full", "raw"}
_ALL_FORMATS = _RAW_FORMATS | {"text"}


# ------------------------------ MIME decoding ------------------------------


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class _HTMLText(HTMLParser):
    """Collect the visible text of an HTML document."""

    _BLOCK: ClassVar[set[str]] = {
        "br",
        "p",
        "div",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
    }
    _SKIP: ClassVar[set[str]] = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in joined.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return parser.text()


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a Gmail payload tree into a list of leaf parts."""
    children = part.get("parts") or []
    if not children:
        return [part]
    leaves: list[dict[str, Any]] = []
    for child in children:
        leaves.extend(_walk(child))
    return leaves


def _part_text(part: dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    text = _b64url_decode(data).decode("utf-8", "replace")
    return text.replace("\r\n", "\n")


def extract_body(payload: dict[str, Any]) -> str:
    """Return the message body as text: text/plain first, else stripped text/html."""
    leaves = [p for p in _walk(payload) if not p.get("filename")]
    for mime_type in ("text/plain", "text/html"):
        texts = [_part_text(p) for p in leaves if p.get("mimeType") == mime_type]
        texts = [t for t in texts if t.strip()]
        if texts:
            body = "\n\n".join(texts)
            if mime_type == "text/html":
                body = html_to_text(body)
            body = body.strip()
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + f"\n[... truncated at {MAX_BODY_CHARS} chars]"
            return body
    return ""


def simplify_message(message: dict[str, Any]) -> dict[str, Any]:
    """Reduce a `format=full` Gmail message to headers, body and attachment names."""
    payload = message.get("payload") or {}
    headers = {
        h["name"]: h["value"] for h in payload.get("headers", []) if h.get("name") in TEXT_HEADERS
    }
    attachments = [
        {
            "filename": p["filename"],
            "mimeType": p.get("mimeType"),
            "size": (p.get("body") or {}).get("size"),
        }
        for p in _walk(payload)
        if p.get("filename")
    ]
    result: dict[str, Any] = {
        "id": message.get("id"),
        "threadId": message.get("threadId"),
        "labelIds": message.get("labelIds", []),
        "headers": {name: headers[name] for name in TEXT_HEADERS if name in headers},
        "body": extract_body(payload),
    }
    if attachments:
        result["attachments"] = attachments
    return result


# ---------------------------------- reads ----------------------------------


def _check_format(format: str) -> str:
    if format not in _ALL_FORMATS:
        raise WorkspaceError(
            f"Unsupported format {format!r}. Allowed: {', '.join(sorted(_ALL_FORMATS))}."
        )
    return "full" if format == "text" else format


def _gmail_get(path: str, params: dict[str, Any] | list[tuple[str, str]]) -> Any:
    return get_json(
        f"{GMAIL_BASE}/users/me/{path}?{urllib.parse.urlencode(params)}",
        token_cache=ADC_TOKEN,
        extra_headers=gmail_headers(),
    )


def gmail_search(query: str, max_results: int = 20, page_token: str = "") -> str:
    """Search Gmail messages using Gmail search syntax.

    Uses *ADC* credentials and sends the mandatory quota-project header.

    `resultSizeEstimate` is only an estimate and stops counting at 201. To
    count matches, page through the results with `page_token` instead.

    Args:
        query: A Gmail search expression, e.g. "from:alice newer_than:7d".
        max_results: Maximum number of message ids to return (1-500).
        page_token: The `nextPageToken` from a previous call, to fetch the next page.

    Returns:
        A JSON object `{messages: [{id, threadId}], nextPageToken, resultSizeEstimate}`.
        `nextPageToken` is null on the last page.
    """
    params = {"q": query, "maxResults": max(1, min(int(max_results), 500))}
    if page_token:
        params["pageToken"] = page_token
    data = _gmail_get("messages", params)
    return json.dumps(
        {
            "messages": data.get("messages", []),
            "nextPageToken": data.get("nextPageToken"),
            "resultSizeEstimate": data.get("resultSizeEstimate"),
        },
        indent=2,
    )


def gmail_get_message(message_id: str, format: str = "text") -> str:
    """Fetch a single Gmail message.

    Uses *ADC* credentials and sends the mandatory quota-project header.

    Args:
        message_id: The Gmail message id.
        format: "text" (default) returns `{id, threadId, labelIds, headers, body,
            attachments}` with the body decoded to plain text (HTML-only mail is
            stripped of tags). "metadata", "minimal", "full" and "raw" return the
            Gmail message resource unchanged.

    Returns:
        The message as JSON.
    """
    api_format = _check_format(format)
    data = _gmail_get(f"messages/{quote(message_id)}", {"format": api_format})
    if format == "text":
        data = simplify_message(data)
    return json.dumps(data, indent=2)


def gmail_get_thread(thread_id: str, format: str = "text") -> str:
    """Fetch a whole Gmail thread, oldest message first.

    Uses *ADC* credentials and sends the mandatory quota-project header.

    Args:
        thread_id: The Gmail thread id (the `threadId` of any of its messages).
        format: "text" (default) decodes every message like `gmail_get_message`;
            "metadata", "minimal" and "full" return the raw Gmail resources.

    Returns:
        A JSON object `{id, messages: [...]}`.
    """
    if format == "raw":
        raise WorkspaceError("format 'raw' is not supported for threads.")
    api_format = _check_format(format)
    data = _gmail_get(f"threads/{quote(thread_id)}", {"format": api_format})
    messages = data.get("messages", [])
    if format == "text":
        messages = [simplify_message(m) for m in messages]
    return json.dumps({"id": data.get("id"), "messages": messages}, indent=2)


# ---------------------------------- writes ---------------------------------


def _reply_headers(thread_id: str) -> dict[str, str]:
    """Return In-Reply-To/References so a draft threads correctly in other clients."""
    params = [("format", "metadata")] + [
        ("metadataHeaders", name) for name in ("Message-ID", "References")
    ]
    thread = _gmail_get(f"threads/{quote(thread_id)}", params)
    messages = thread.get("messages") or []
    if not messages:
        return {}
    headers = {
        h["name"].lower(): h["value"]
        for h in (messages[-1].get("payload") or {}).get("headers", [])
    }
    message_id = headers.get("message-id")
    if not message_id:
        return {}
    references = f"{headers['references']} {message_id}" if "references" in headers else message_id
    return {"In-Reply-To": message_id, "References": references}


def gmail_create_draft(to: str, subject: str, body: str, cc: str = "", thread_id: str = "") -> str:
    """Create a Gmail draft. Nothing is sent; a human reviews and sends it.

    Uses *ADC* credentials; the ADC login must include the
    `https://www.googleapis.com/auth/gmail.compose` scope.

    Args:
        to: Recipient address(es), comma-separated.
        subject: Subject line. For a reply, keep the thread's subject (with "Re: ").
        body: Plain-text message body.
        cc: Optional Cc address(es), comma-separated.
        thread_id: Optional thread id to file the draft as a reply in that thread.

    Returns:
        A JSON object `{draftId, messageId, threadId}`.
    """
    for name, value in (("to", to), ("cc", cc), ("subject", subject)):
        if "\r" in value or "\n" in value:
            raise WorkspaceError(f"{name!r} must not contain line breaks.")
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if thread_id:
        for name, value in _reply_headers(thread_id).items():
            msg[name] = value
    msg.set_content(body)

    message: dict[str, str] = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if thread_id:
        message["threadId"] = thread_id
    result = send_json(
        f"{GMAIL_BASE}/users/me/drafts",
        token_cache=ADC_TOKEN,
        data=json.dumps({"message": message}).encode("utf-8"),
        extra_headers=gmail_headers(),
    )
    created = result.get("message") or {}
    return json.dumps(
        {
            "draftId": result.get("id"),
            "messageId": created.get("id"),
            "threadId": created.get("threadId"),
        },
        indent=2,
    )
