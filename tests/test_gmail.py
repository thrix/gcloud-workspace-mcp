from __future__ import annotations

import base64
import email
import json
import urllib.parse

import pytest
from conftest import FakeGoogle

from gcloud_workspace_mcp import gmail
from gcloud_workspace_mcp.auth import WorkspaceError


def b64url(text: str) -> str:
    # Gmail omits padding; the decoder must cope with that.
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def headers(**values: str) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in values.items()]


MULTIPART_ALTERNATIVE = {
    "id": "m1",
    "threadId": "t1",
    "labelIds": ["INBOX"],
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": headers(From="a@x", To="b@x", Subject="Hi", Date="today", Received="junk"),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64url("Plain body ü?")}},
                    {"mimeType": "text/html", "body": {"data": b64url("<p>HTML body</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "att", "size": 1234},
            },
        ],
    },
}

HTML_ONLY = {
    "id": "m2",
    "threadId": "t1",
    "payload": {
        "mimeType": "text/html",
        "headers": headers(From="c@x"),
        "body": {
            "data": b64url(
                "<html><head><style>p{color:red}</style></head><body>"
                "<p>Hello&nbsp;<b>world</b> &amp; friends</p><script>evil()</script>"
                "<div>Second line</div></body></html>"
            )
        },
    },
}


def test_b64url_decode_without_padding() -> None:
    assert gmail._b64url_decode(b64url("a")) == b"a"
    assert gmail._b64url_decode(b64url("ab")) == b"ab"


def test_simplify_prefers_plain_text() -> None:
    out = gmail.simplify_message(MULTIPART_ALTERNATIVE)
    assert out["body"] == "Plain body ü?"
    assert out["headers"] == {"From": "a@x", "To": "b@x", "Date": "today", "Subject": "Hi"}
    assert out["attachments"] == [
        {"filename": "report.pdf", "mimeType": "application/pdf", "size": 1234}
    ]


def test_simplify_html_fallback() -> None:
    out = gmail.simplify_message(HTML_ONLY)
    assert out["body"] == "Hello world & friends\n\nSecond line"
    assert "attachments" not in out


def test_body_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gmail, "MAX_BODY_CHARS", 5)
    payload = {"mimeType": "text/plain", "body": {"data": b64url("0123456789")}}
    assert gmail.extract_body(payload).startswith("01234\n[... truncated")


def test_crlf_normalized() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": b64url("a\r\nb\r\n")}}
    assert gmail.extract_body(payload) == "a\nb"


def test_empty_body() -> None:
    assert gmail.extract_body({"mimeType": "multipart/mixed", "parts": []}) == ""


def test_gmail_search_paginates(google: FakeGoogle) -> None:
    google.queue_json({"messages": [{"id": "1"}], "nextPageToken": "N", "resultSizeEstimate": 201})
    out = json.loads(gmail.gmail_search("newer_than:1d", page_token="P"))
    assert out["nextPageToken"] == "N"
    req = google.requests[0]
    assert req.get_header("X-goog-user-project") == "test-quota-project"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["pageToken"] == ["P"]


def test_gmail_get_message_text(google: FakeGoogle) -> None:
    google.queue_json(MULTIPART_ALTERNATIVE)
    out = json.loads(gmail.gmail_get_message("m1"))
    assert out["body"] == "Plain body ü?"
    assert "format=full" in google.requests[0].full_url


def test_gmail_get_message_raw_formats_pass_through(google: FakeGoogle) -> None:
    google.queue_json({"id": "m1", "snippet": "s"})
    assert json.loads(gmail.gmail_get_message("m1", "metadata")) == {"id": "m1", "snippet": "s"}
    assert "format=metadata" in google.requests[0].full_url


def test_gmail_get_message_bad_format() -> None:
    with pytest.raises(WorkspaceError, match="Unsupported format"):
        gmail.gmail_get_message("m1", "html")


def test_gmail_get_thread(google: FakeGoogle) -> None:
    google.queue_json({"id": "t1", "messages": [MULTIPART_ALTERNATIVE, HTML_ONLY]})
    out = json.loads(gmail.gmail_get_thread("t1"))
    assert out["id"] == "t1"
    assert [m["body"] for m in out["messages"]] == [
        "Plain body ü?",
        "Hello world & friends\n\nSecond line",
    ]


def test_gmail_get_thread_rejects_raw() -> None:
    with pytest.raises(WorkspaceError, match="raw"):
        gmail.gmail_get_thread("t1", "raw")


def _sent_mime(google: FakeGoogle) -> tuple[dict, email.message.Message]:
    payload = json.loads(google.requests[-1].data)
    raw = base64.urlsafe_b64decode(payload["message"]["raw"])
    return payload, email.message_from_bytes(raw)


def test_gmail_create_draft(google: FakeGoogle) -> None:
    google.queue_json({"id": "d1", "message": {"id": "m9", "threadId": "t9"}})
    out = json.loads(gmail.gmail_create_draft("b@x", "Subj", "Body text", cc="c@x"))
    assert out == {"draftId": "d1", "messageId": "m9", "threadId": "t9"}
    req = google.requests[0]
    assert req.full_url == "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    assert req.get_method() == "POST"
    payload, mime = _sent_mime(google)
    assert "threadId" not in payload["message"]
    assert (mime["To"], mime["Cc"], mime["Subject"]) == ("b@x", "c@x", "Subj")
    assert mime.get_payload(decode=True).decode().strip() == "Body text"


def test_gmail_create_draft_reply_threads(google: FakeGoogle) -> None:
    thread = {
        "messages": [
            {"payload": {"headers": headers(**{"Message-ID": "<a@x>"})}},
            {"payload": {"headers": headers(**{"Message-Id": "<b@x>", "References": "<a@x>"})}},
        ]
    }
    google.queue_json(thread, {"id": "d1", "message": {"id": "m9", "threadId": "t1"}})
    gmail.gmail_create_draft("b@x", "Re: Hi", "ok", thread_id="t1")
    payload, mime = _sent_mime(google)
    assert payload["message"]["threadId"] == "t1"
    assert mime["In-Reply-To"] == "<b@x>"
    assert mime["References"] == "<a@x> <b@x>"


@pytest.mark.parametrize("field", ["to", "cc", "subject"])
def test_gmail_create_draft_rejects_header_injection(field: str) -> None:
    args = {"to": "b@x", "subject": "s", "body": "b", "cc": ""}
    args[field] = "x\r\nBcc: evil@x"
    with pytest.raises(WorkspaceError, match="line breaks"):
        gmail.gmail_create_draft(**args)
