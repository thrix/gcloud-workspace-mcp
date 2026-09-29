from __future__ import annotations

import base64
import email
import json
import urllib.parse
from pathlib import Path

import pytest
from conftest import FakeGoogle

from gcloud_workspace_mcp import gmail
from gcloud_workspace_mcp.auth import WorkspaceError
from gcloud_workspace_mcp.http import ResponseTooLarge


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


def test_reply_skips_earlier_drafts(google: FakeGoogle) -> None:
    thread = {
        "messages": [
            {"labelIds": ["INBOX"], "payload": {"headers": headers(**{"Message-ID": "<a@x>"})}},
            {"labelIds": ["DRAFT"], "payload": {"headers": headers(**{"Message-ID": "<d@x>"})}},
        ]
    }
    google.queue_json(thread, {"id": "d1", "message": {"id": "m9", "threadId": "t1"}})
    gmail.gmail_create_draft("b@x", "Re: Hi", "ok", thread_id="t1")
    _, mime = _sent_mime(google)
    assert mime["In-Reply-To"] == "<a@x>"
    assert mime["References"] == "<a@x>"


@pytest.mark.parametrize("field", ["to", "cc", "subject"])
def test_gmail_create_draft_rejects_header_injection(field: str) -> None:
    args = {"to": "b@x", "subject": "s", "body": "b", "cc": ""}
    args[field] = "x\r\nBcc: evil@x"
    with pytest.raises(WorkspaceError, match="line breaks"):
        gmail.gmail_create_draft(**args)


def test_part_without_inline_data_is_skipped() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"attachmentId": "big", "size": 90000}},
            {"mimeType": "text/html", "body": {"data": b64url("<p>fallback</p>")}},
        ],
    }
    assert gmail.extract_body(payload) == "fallback"


def test_html_nested_skip_tags_and_stray_end_tags() -> None:
    html = "</style><head><title>T</title><style>x</style></head><p>kept</p><br>line"
    assert gmail.html_to_text(html) == "kept\n\nline"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("line1<br>line2", "line1\nline2"),
        ("line1<br/>line2<br />line3", "line1\nline2\nline3"),
        ("<p>a</p><p/>b", "a\n\nb"),
        ('go <a href="https://x.test/"/>now', "go <https://x.test/>now"),
        ("<td/>x", "x"),
        ("<script/>shown", "shown"),
    ],
)
def test_html_self_closing_tags(html: str, expected: str) -> None:
    assert gmail.html_to_text(html) == expected


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda: gmail.gmail_get_message(".."), "message_id"),
        (lambda: gmail.gmail_get_thread(".."), "thread_id"),
        (lambda: gmail.gmail_create_draft("a@x", "s", "b", thread_id="t/1"), "thread_id"),
    ],
)
def test_gmail_tools_refuse_bad_ids(call, name: str, google: FakeGoogle) -> None:
    with pytest.raises(WorkspaceError, match=rf"{name} .* is not a valid id"):
        call()
    assert google.requests == []


def test_simplify_message_without_payload() -> None:
    assert gmail.simplify_message({"id": "m"}) == {
        "id": "m",
        "threadId": None,
        "labelIds": [],
        "headers": {},
        "body": "",
    }


def test_gmail_get_thread_raw_formats_pass_through(google: FakeGoogle) -> None:
    google.queue_json({"id": "t1", "messages": [{"id": "m1", "snippet": "s"}]})
    out = json.loads(gmail.gmail_get_thread("t1", "metadata"))
    assert out == {"id": "t1", "messages": [{"id": "m1", "snippet": "s"}]}
    assert "format=metadata" in google.requests[0].full_url


def test_gmail_search_clamps_max_results(google: FakeGoogle) -> None:
    google.queue_json({})
    out = json.loads(gmail.gmail_search("x", max_results=0))
    assert out == {"messages": [], "nextPageToken": None, "resultSizeEstimate": None}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(google.requests[0].full_url).query)
    assert query["maxResults"] == ["1"]
    assert "pageToken" not in query


@pytest.mark.parametrize(
    "thread",
    [
        {},
        {"messages": []},
        {"messages": [{"payload": {"headers": headers(Subject="no id")}}]},
        {"messages": [{}]},
        {
            "messages": [
                {"labelIds": ["DRAFT"], "payload": {"headers": headers(**{"Message-ID": "<d@x>"})}}
            ]
        },
    ],
)
def test_reply_without_message_id_still_threads(google: FakeGoogle, thread: dict) -> None:
    google.queue_json(thread, {"id": "d1", "message": {"id": "m9", "threadId": "t1"}})
    gmail.gmail_create_draft("b@x", "Re: Hi", "ok", thread_id="t1")
    payload, mime = _sent_mime(google)
    assert payload["message"]["threadId"] == "t1"
    assert mime["In-Reply-To"] is None
    assert mime["References"] is None


def test_reply_headers_request_only_threading_headers(google: FakeGoogle) -> None:
    google.queue_json({"messages": []})
    gmail._reply_headers("t1")
    url = google.requests[0].full_url
    assert "/threads/t1?" in url
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query == {"format": ["metadata"], "metadataHeaders": ["Message-ID", "References"]}


def test_gmail_create_draft_empty_response(google: FakeGoogle) -> None:
    google.queue_json({})
    out = json.loads(gmail.gmail_create_draft("b@x", "s", "b"))
    assert out == {"draftId": None, "messageId": None, "threadId": None}


def _part(raw: bytes, content_type: str | None) -> dict:
    part: dict = {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(raw).decode().rstrip("=")},
    }
    if content_type is not None:
        part["headers"] = headers(**{"Content-Type": content_type})
    return part


@pytest.mark.parametrize(
    ("raw", "content_type", "expected"),
    [
        ("Žluťoučký kůň".encode("iso-8859-2"), "text/plain; charset=ISO-8859-2", "Žluťoučký kůň"),
        ("Děkuji".encode("cp1250"), 'text/plain; charset="windows-1250"; format=flowed', "Děkuji"),
        ("ü".encode(), "text/plain; charset=unknown-8bit", "ü"),
        ("ü".encode(), "text/plain", "ü"),
        ("ü".encode(), None, "ü"),
    ],
)
def test_body_uses_declared_charset(raw: bytes, content_type: str | None, expected: str) -> None:
    assert gmail.extract_body(_part(raw, content_type)) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<table><tr><td>Assignee</td><td>Jane</td></tr><tr><th>Status</th><td>Open</td></tr>"
            "</table>",
            "Assignee Jane\n\nStatus Open",
        ),
        ('<p>See <a href="https://x/y">the MR</a></p>', "See the MR <https://x/y>"),
        ('<a href="https://x/y">https://x/y</a>', "https://x/y"),
        ('<a href="mailto:a@x">a@x</a> or <a href="mailto:a@x">me</a>', "a@x or me <mailto:a@x>"),
        ('<a href="#top">top</a> <a href="javascript:x()">js</a> <a>bare</a>', "top js bare"),
        ('<head><a href="https://hidden">t</a></head>kept</a>', "kept"),
    ],
)
def test_html_tables_and_links(html: str, expected: str) -> None:
    assert gmail.html_to_text(html) == expected


@pytest.mark.parametrize("fmt", ["raw", "full", "metadata", "minimal"])
def test_gmail_get_message_resource_cap(google: FakeGoogle, fmt: str) -> None:
    google.queue(ResponseTooLarge("Response is over the 1048576-byte limit."))
    with pytest.raises(WorkspaceError, match=r"byte limit\. Use format='text'"):
        gmail.gmail_get_message("m1", fmt)


def test_gmail_resource_formats_request_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int | None] = []

    def fake_get(path: str, params: dict, max_bytes: int | None = None) -> dict:
        seen.append(max_bytes)
        return {"id": "t1", "messages": []}

    monkeypatch.setattr(gmail, "_gmail_get", fake_get)
    gmail.gmail_get_message("m1", "raw")
    gmail.gmail_get_thread("t1", "full")
    gmail.gmail_get_thread("t1")
    assert seen == [gmail.MAX_RAW_BYTES, gmail.MAX_RESOURCE_BYTES, None]


def test_gmail_get_message_raw_is_saved(google: FakeGoogle, download_dir: Path) -> None:
    eml = b"From: a@x\r\nSubject: Hi\r\n\r\nbody\r\n"
    raw = base64.urlsafe_b64encode(eml).decode().rstrip("=")
    google.queue_json({"id": "m1", "threadId": "t1", "sizeEstimate": 99, "raw": raw})
    out = json.loads(gmail.gmail_get_message("m1", "raw"))
    path = download_dir / "m1.eml"
    assert out == {
        "id": "m1",
        "threadId": "t1",
        "sizeEstimate": 99,
        "path": str(path),
        "mimeType": "message/rfc822",
        "bytes": len(eml),
    }
    assert path.read_bytes() == eml
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(google.requests[0].full_url).query)
    assert query["format"] == ["raw"]


def test_gmail_resource_other_errors_pass_through(google: FakeGoogle) -> None:
    google.queue(WorkspaceError("boom"))
    with pytest.raises(WorkspaceError, match=r"^boom$"):
        gmail.gmail_get_thread("t1", "full")


def test_simplify_matches_header_names_in_any_case() -> None:
    # Outlook writes "CC"; Gmail passes the sender's spelling through.
    message = {"payload": {"headers": headers(CC="c@x", SUBJECT="Hi", received="junk")}}
    assert gmail.simplify_message(message)["headers"] == {"Cc": "c@x", "Subject": "Hi"}


def _plain(message_id: str, body: str) -> dict:
    return {
        "id": message_id,
        "payload": {"mimeType": "text/plain", "body": {"data": b64url(body)}},
    }


def test_gmail_get_thread_drops_oldest_bodies(
    google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gmail, "MAX_THREAD_CHARS", 10)
    # m1 would still fit on its own; everything older than the first miss goes.
    thread = [_plain("m1", ""), _plain("m2", "aaa"), _plain("m3", "bbbbbb"), _plain("m4", "cccc")]
    google.queue_json({"id": "t1", "messages": thread})
    out = json.loads(gmail.gmail_get_thread("t1"))
    assert [m["body"] for m in out["messages"]] == [None, None, "bbbbbb", "cccc"]
    assert out["omittedBodies"] == ["m1", "m2"]


def test_gmail_get_thread_within_budget_omits_nothing(google: FakeGoogle) -> None:
    google.queue_json({"id": "t1", "messages": [_plain("m1", "a"), _plain("m2", "b")]})
    assert "omittedBodies" not in json.loads(gmail.gmail_get_thread("t1"))
