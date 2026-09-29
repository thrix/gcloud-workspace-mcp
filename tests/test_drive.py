from __future__ import annotations

import json
import os
import stat
import urllib.parse
from pathlib import Path

import pytest
from conftest import FakeGoogle, FakeResponse

from gcloud_workspace_mcp import downloads, drive
from gcloud_workspace_mcp.auth import WorkspaceError


def _query(req) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


def test_drive_search_paginates(google: FakeGoogle) -> None:
    google.queue_json({"files": [{"id": "a"}], "nextPageToken": "NEXT"})
    result = json.loads(drive.drive_search("name contains 'x'", page_size=500, page_token="P1"))
    assert result == {"files": [{"id": "a"}], "nextPageToken": "NEXT", "incompleteSearch": False}
    q = _query(google.requests[0])
    assert q["corpora"] == ["allDrives"]
    assert q["pageToken"] == ["P1"]
    assert q["pageSize"] == ["100"]
    assert q["includeItemsFromAllDrives"] == ["true"]


def test_drive_search_reports_incomplete_search(google: FakeGoogle) -> None:
    google.queue_json({"files": [], "incompleteSearch": True})
    assert json.loads(drive.drive_search("x"))["incompleteSearch"] is True
    assert "incompleteSearch" in _query(google.requests[0])["fields"][0]


def test_drive_search_last_page(google: FakeGoogle) -> None:
    google.queue_json({"files": []})
    assert json.loads(drive.drive_search("x"))["nextPageToken"] is None
    assert "pageToken" not in _query(google.requests[0])


def test_docs_export_rejects_unknown_type() -> None:
    with pytest.raises(WorkspaceError, match="Unsupported export"):
        drive.docs_export("id", "application/zip")


def test_docs_export_markdown(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"# Title\n", "text/markdown"))
    assert drive.docs_export("doc-1") == "# Title\n"
    assert "/files/doc-1/export" in google.requests[0].full_url


@pytest.mark.parametrize(
    "call",
    [
        lambda: drive.drive_get_metadata(".."),
        lambda: drive.docs_export(".."),
        lambda: drive.drive_download("a/b"),
        lambda: drive.docs_replace_content("..", "x"),
        lambda: drive.docs_append_text(".", "x"),
    ],
)
def test_drive_tools_refuse_bad_ids(call, google: FakeGoogle) -> None:
    with pytest.raises(WorkspaceError, match=r"file_id .* is not a valid id"):
        call()
    assert google.requests == []


def test_docs_export_csv_for_sheets(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"a,b\n1,2\n", "text/csv"))
    assert drive.docs_export("sheet", "text/csv") == "a,b\n1,2\n"
    assert _query(google.requests[0])["mimeType"] == ["text/csv"]


def test_drive_download_text(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "a.txt", "mimeType": "text/plain", "size": "5"})
    google.queue(FakeResponse(b"hello", "text/plain"))
    assert drive.drive_download("f") == "hello"
    assert _query(google.requests[1])["alt"] == ["media"]


def test_drive_download_binary_is_saved(google: FakeGoogle, download_dir: Path) -> None:
    google.queue_json({"id": "f", "name": "a.pdf", "mimeType": "application/pdf", "size": "3"})
    google.queue(FakeResponse(b"%PD", "application/pdf"))
    out = json.loads(drive.drive_download("f"))
    path = download_dir / "f.pdf"
    assert out == {"path": str(path), "name": "a.pdf", "mimeType": "application/pdf", "bytes": 3}
    assert path.read_bytes() == b"%PD"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(download_dir.stat().st_mode) == 0o700
    # Only the finished file is left behind.
    assert os.listdir(download_dir) == ["f.pdf"]


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("report.xyz9", ".xyz9"),
        ("model.safetensors", ""),
        ("no-suffix", ""),
        ("x.../../etc", ""),
        ("", ""),
    ],
)
def test_saved_file_suffix(google: FakeGoogle, download_dir: Path, name: str, suffix: str) -> None:
    mime_type = "application/x-unknown-to-mimetypes"
    google.queue_json({"id": "f", "name": name, "mimeType": mime_type})
    google.queue(FakeResponse(b"data", mime_type))
    out = json.loads(drive.drive_download("f"))
    assert out["path"] == str(download_dir / f"f{suffix}")
    assert out["name"] == (name or None)


def test_save_failure_is_reported(
    google: FakeGoogle, download_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(downloads.os, "replace", refuse)
    google.queue(FakeResponse(b"%PDF", "application/pdf"))
    with pytest.raises(WorkspaceError, match=r"No space left on device.*DOWNLOAD_DIR"):
        drive.docs_export("d", "application/pdf")
    # The temporary file is gone.
    assert os.listdir(download_dir) == []


def test_drive_download_refuses_native(google: FakeGoogle) -> None:
    google.queue_json({"id": "f", "name": "Doc", "mimeType": drive.GOOGLE_DOC_MIME})
    with pytest.raises(WorkspaceError, match="use docs_export"):
        drive.drive_download("f")
    assert len(google.requests) == 1


LIMIT = downloads.MAX_DOWNLOAD_BYTES


@pytest.mark.parametrize("mime_type", ["application/pdf", "text/plain"])
def test_drive_download_caps_body_without_size(google: FakeGoogle, mime_type: str) -> None:
    google.queue_json({"id": "f", "name": "a", "mimeType": mime_type})
    google.queue(FakeResponse(b"x" * (LIMIT + 1), mime_type))
    with pytest.raises(WorkspaceError, match=f"over the {LIMIT}-byte limit"):
        drive.drive_download("f")


@pytest.mark.parametrize("mime_type", ["application/pdf", "text/plain"])
def test_drive_download_size_cap(google: FakeGoogle, mime_type: str) -> None:
    size = str(LIMIT + 1)
    google.queue_json({"id": "f", "name": "big", "mimeType": mime_type, "size": size})
    with pytest.raises(WorkspaceError, match=f"over the {LIMIT}-byte download limit"):
        drive.drive_download("f")
    assert len(google.requests) == 1


def test_drive_download_large_text_is_windowed(google: FakeGoogle) -> None:
    body = b"x" * (drive.MAX_TEXT_CHARS * 3)
    google.queue_json(
        {"id": "f", "name": "a.txt", "mimeType": "text/plain", "size": str(len(body))}
    )
    google.queue(FakeResponse(body, "text/plain"))
    out = drive.drive_download("f")
    assert out.startswith("x" * drive.MAX_TEXT_CHARS + "\n[... truncated")


def test_drive_download_text_offset(google: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive, "MAX_TEXT_CHARS", 4)
    google.queue_json({"id": "f", "name": "a.txt", "mimeType": "text/plain"})
    google.queue(FakeResponse(b"0123456789", "text/plain"))
    assert drive.drive_download("f", offset=4) == (
        "4567\n[... truncated: chars 4-8 of 10; call again with offset=8 for more]"
    )


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
    google.queue_json(
        {"id": "d", "name": "T", "mimeType": drive.GOOGLE_DOC_MIME},
        {"id": "d", "name": "T", "modifiedTime": "now"},
    )
    drive.docs_replace_content("d", "new body")
    assert _query(google.requests[0])["fields"] == ["id,name,mimeType"]
    req = google.requests[1]
    assert req.get_method() == "PATCH"
    assert "/upload/drive/v3/files/d?" in req.full_url
    assert _query(req)["uploadType"] == ["media"]
    assert req.data == b"new body"


@pytest.mark.parametrize("mime_type", ["application/pdf", "text/plain", None])
def test_docs_replace_content_refuses_non_docs(google: FakeGoogle, mime_type: str | None) -> None:
    meta = {"id": "f", "name": "report.pdf"}
    if mime_type:
        meta["mimeType"] = mime_type
    google.queue_json(meta)
    with pytest.raises(WorkspaceError, match="not a Google Doc"):
        drive.docs_replace_content("f", "x")
    # Only the metadata lookup went out; the file was never touched.
    assert len(google.requests) == 1


def test_docs_append_text(google: FakeGoogle) -> None:
    google.queue_json({"documentId": "d", "replies": [{}]})
    assert json.loads(drive.docs_append_text("d", "more\n")) == {"documentId": "d"}
    req = google.requests[0]
    assert req.full_url == "https://docs.googleapis.com/v1/documents/d:batchUpdate"
    assert json.loads(req.data) == {
        "requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "more\n"}}]
    }


def test_drive_and_docs_calls_never_send_quota_project(google: FakeGoogle) -> None:
    # The quota project is for Gmail. Docs calls work on the Cloud SDK's own
    # project, and a quota project without the Docs API would break them.
    google.queue_json({"files": []}, {"id": "f"}, {"documentId": "d"})
    drive.drive_search("x")
    drive.drive_get_metadata("f")
    drive.docs_append_text("d", "more")
    assert all(r.get_header("X-goog-user-project") is None for r in google.requests)


def test_docs_export_pdf_is_saved(google: FakeGoogle, download_dir: Path) -> None:
    google.queue(FakeResponse(b"%PDF-1.7", "application/pdf"))
    out = json.loads(drive.docs_export("d", "application/pdf"))
    assert out == {
        "path": str(download_dir / "d.pdf"),
        "name": None,
        "mimeType": "application/pdf",
        "bytes": 8,
    }
    assert (download_dir / "d.pdf").read_bytes() == b"%PDF-1.7"
    assert _query(google.requests[0])["mimeType"] == ["application/pdf"]


def test_docs_export_pdf_cap(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"x" * (LIMIT + 1), "application/pdf"))
    with pytest.raises(WorkspaceError, match="byte limit"):
        drive.docs_export("d", "application/pdf")


def test_docs_export_text_is_windowed(google: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive, "MAX_TEXT_CHARS", 4)
    google.queue(FakeResponse(b"0123456789", "text/plain"), FakeResponse(b"0123456789"))
    first = drive.docs_export("d", "text/plain")
    assert first.startswith("0123\n[... truncated: chars 0-4 of 10; call again with offset=4")
    # The last window carries no marker.
    assert drive.docs_export("d", "text/plain", offset=8) == "89"


def test_docs_export_text_cap(google: FakeGoogle) -> None:
    google.queue(FakeResponse(b"x" * (downloads.MAX_DOWNLOAD_BYTES + 1), "text/plain"))
    with pytest.raises(WorkspaceError, match="byte limit"):
        drive.docs_export("d", "text/plain")


def test_text_window_offset_past_end() -> None:
    with pytest.raises(WorkspaceError, match="past the end"):
        drive._text_window(b"abc", 3)


@pytest.mark.parametrize(("body", "offset"), [(b"", 5), (b"abc", -2)])
def test_text_window_edges(body: bytes, offset: int) -> None:
    assert drive._text_window(body, offset) == body.decode()


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
