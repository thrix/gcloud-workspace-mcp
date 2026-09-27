from __future__ import annotations

import base64
import json
import urllib.parse

import pytest
from conftest import FakeGoogle, FakeResponse

from gcloud_workspace_mcp import drive
from gcloud_workspace_mcp.auth import WorkspaceError


def _query(req) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


def test_drive_search_paginates(google: FakeGoogle) -> None:
    google.queue_json({"files": [{"id": "a"}], "nextPageToken": "NEXT"})
    result = json.loads(drive.drive_search("name contains 'x'", page_size=500, page_token="P1"))
    assert result == {"files": [{"id": "a"}], "nextPageToken": "NEXT"}
    q = _query(google.requests[0])
    assert q["pageToken"] == ["P1"]
    assert q["pageSize"] == ["100"]
    assert q["includeItemsFromAllDrives"] == ["true"]


def test_drive_search_last_page(google: FakeGoogle) -> None:
    google.queue_json({"files": []})
    assert json.loads(drive.drive_search("x"))["nextPageToken"] is None
    assert "pageToken" not in _query(google.requests[0])


def test_docs_export_rejects_unknown_type() -> None:
    with pytest.raises(WorkspaceError, match="Unsupported export"):
        drive.docs_export("id", "application/zip")


def test_docs_export_markdown(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"# Title\n", "text/markdown"))
    assert drive.docs_export("doc/1") == "# Title\n"
    assert "/files/doc%2F1/export" in google.requests[0].full_url


def test_drive_download_text(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "a.txt", "mimeType": "text/plain", "size": "5"})
    google.queue(FakeResponse(b"hello", "text/plain"))
    assert drive.drive_download("f") == "hello"
    assert _query(google.requests[1])["alt"] == ["media"]


def test_drive_download_binary(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "a.pdf", "mimeType": "application/pdf", "size": "3"})
    google.queue(FakeResponse(b"%PD", "application/pdf"))
    out = drive.drive_download("f")
    header, encoded = out.split("\n", 1)
    assert "application/pdf" in header
    assert base64.b64decode(encoded) == b"%PD"


def test_drive_download_refuses_native(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "Doc", "mimeType": drive.GOOGLE_DOC_MIME})
    with pytest.raises(WorkspaceError, match="use docs_export"):
        drive.drive_download("f")
    assert len(google.requests) == 1


def test_drive_download_size_cap(google: FakeGoogle) -> None:
    size = str(drive.MAX_DOWNLOAD_BYTES + 1)
    google.queue_json({"id": "f", "name": "big", "mimeType": "application/pdf", "size": size})
    with pytest.raises(WorkspaceError, match="download limit"):
        drive.drive_download("f")
    assert len(google.requests) == 1


def test_docs_create_from_markdown(google: FakeGoogle) -> None:
    google.queue_json({"id": "new", "name": "T", "webViewLink": "https://docs.google.com/x"})
    out = json.loads(drive.docs_create_from_markdown("T", "# Hi", parent_id="folder"))
    assert out["id"] == "new"
    req = google.requests[0]
    assert req.full_url.startswith("https://www.googleapis.com/upload/drive/v3/files?")
    assert _query(req)["uploadType"] == ["multipart"]
    content_type = req.get_header("Content-type")
    assert content_type.startswith("multipart/related; boundary=")
    boundary = content_type.split("boundary=")[1]
    parts = req.data.split(f"--{boundary}".encode())
    metadata = json.loads(parts[1].split(b"\r\n\r\n", 1)[1])
    assert metadata == {"name": "T", "mimeType": drive.GOOGLE_DOC_MIME, "parents": ["folder"]}
    assert b"Content-Type: text/markdown\r\n\r\n# Hi\r\n" in parts[2]


def test_docs_replace_content(google: FakeGoogle) -> None:
    google.queue_json({"id": "d", "name": "T", "modifiedTime": "now"})
    drive.docs_replace_content("d", "new body")
    req = google.requests[0]
    assert req.get_method() == "PATCH"
    assert "/upload/drive/v3/files/d?" in req.full_url
    assert _query(req)["uploadType"] == ["media"]
    assert req.data == b"new body"


def test_docs_append_text(google: FakeGoogle) -> None:
    google.queue_json({"documentId": "d", "replies": [{}]})
    assert json.loads(drive.docs_append_text("d", "more\n")) == {"documentId": "d"}
    req = google.requests[0]
    assert req.full_url == "https://docs.googleapis.com/v1/documents/d:batchUpdate"
    assert json.loads(req.data) == {
        "requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "more\n"}}]
    }


def test_docs_export_pdf_is_base64(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"%PDF-1.7", "application/pdf"))
    header, encoded = drive.docs_export("d", "application/pdf").split("\n", 1)
    assert header == "[binary application/pdf, base64-encoded below; 8 bytes]"
    assert base64.b64decode(encoded) == b"%PDF-1.7"
    assert _query(google.requests[0])["mimeType"] == ["application/pdf"]


def test_drive_get_metadata(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "n"})
    assert json.loads(drive.drive_get_metadata("f")) == {"id": "f", "name": "n"}
    q = _query(google.requests[0])
    assert "owners(displayName,emailAddress)" in q["fields"][0]
    assert q["supportsAllDrives"] == ["true"]


def test_drive_download_text_like_application_types(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "a.json", "mimeType": "application/json"})
    google.queue(FakeResponse(b'{"a": 1}', "application/json"))
    assert drive.drive_download("f") == '{"a": 1}'


def test_docs_create_from_markdown_without_parent(google: FakeGoogle) -> None:
    google.queue_json({"id": "new"})
    drive.docs_create_from_markdown("T", "x")
    body = google.requests[0].data
    assert b'"parents"' not in body
