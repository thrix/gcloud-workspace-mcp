"""Google Drive and Docs tools. All of them use the *user* credentials."""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .auth import USER_TOKEN, WorkspaceError
from .downloads import MAX_DOWNLOAD_BYTES, save_file
from .http import (
    DOCS_BASE,
    DRIVE_BASE,
    DRIVE_UPLOAD_BASE,
    get_json,
    request,
    resource_id,
    send_json,
)

# Export MIME types for Google-native files. Docs take all but text/csv,
# Sheets take only text/csv (first sheet) and PDF, Slides take text/plain and PDF.
ALLOWED_EXPORT_TYPES = {
    "text/markdown",
    "text/plain",
    "text/html",
    "text/csv",
    "application/pdf",
}

# Text goes to the client in windows of this many characters; `offset` picks one.
MAX_TEXT_CHARS = 50_000

# A file-name suffix from Drive is kept only when it looks like an extension.
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
}


def _is_text(mime_type: str) -> bool:
    return mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES


def _save_binary(file_id: str, mime_type: str, body: bytes, name: str = "") -> str:
    """Save ``body`` under the download directory and describe the file as JSON.

    The file name is the Drive id plus an extension, so a file name chosen by
    whoever shared the file never becomes part of a local path.
    """
    suffix = mimetypes.guess_extension(mime_type) or ""
    if not suffix and _SUFFIX.fullmatch(Path(name).suffix):
        suffix = Path(name).suffix
    path = save_file(f"{file_id}{suffix}", body)
    return json.dumps(
        {"path": str(path), "name": name or None, "mimeType": mime_type, "bytes": len(body)},
        indent=2,
    )


def _text_window(body: bytes, offset: int) -> str:
    """Decode ``body`` and return ``MAX_TEXT_CHARS`` characters from ``offset``."""
    text = body.decode("utf-8", "replace")
    offset = max(0, int(offset))
    if offset >= len(text) > 0:
        raise WorkspaceError(f"offset {offset} is past the end ({len(text)} chars).")
    end = offset + MAX_TEXT_CHARS
    window = text[offset:end]
    if end < len(text):
        window += (
            f"\n[... truncated: chars {offset}-{end} of {len(text)}; "
            f"call again with offset={end} for more]"
        )
    return window


# ---------------------------------- reads ----------------------------------


def drive_search(query: str, page_size: int = 20, page_token: str = "") -> str:
    """Search Google Drive files using a raw Drive `q` query string.

    Uses *user* credentials (`gcloud auth print-access-token`). Searches My
    Drive, files shared with you, and every shared drive you belong to.

    Args:
        query: A raw Drive v3 `q` expression, e.g.
            "name contains 'design' and mimeType='application/vnd.google-apps.document'".
            See https://developers.google.com/drive/api/guides/search-files
        page_size: Maximum number of files to return (1-100).
        page_token: The `nextPageToken` from a previous call, to fetch the next page.

    Returns:
        A JSON object `{files: [{id, name, mimeType, modifiedTime}], nextPageToken,
        incompleteSearch}`. `nextPageToken` is null on the last page.
        `incompleteSearch` is true when Drive skipped some shared drives; narrow
        the query and search again.
    """
    params = {
        "q": query,
        "pageSize": max(1, min(int(page_size), 100)),
        "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime)",
        # The default `user` corpus holds only files you created, opened or got
        # shared directly, so shared-drive files you never touched would be missing.
        "corpora": "allDrives",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    data = get_json(f"{DRIVE_BASE}/files?{urllib.parse.urlencode(params)}", token_cache=USER_TOKEN)
    return json.dumps(
        {
            "files": data.get("files", []),
            "nextPageToken": data.get("nextPageToken"),
            "incompleteSearch": data.get("incompleteSearch", False),
        },
        indent=2,
    )


def _metadata(file_id: str, fields: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"fields": fields, "supportsAllDrives": "true"})
    data: dict[str, Any] = get_json(
        f"{DRIVE_BASE}/files/{resource_id(file_id, name='file_id')}?{params}",
        token_cache=USER_TOKEN,
    )
    return data


def drive_get_metadata(file_id: str) -> str:
    """Return metadata for a single Drive file.

    Uses *user* credentials.

    Args:
        file_id: The Drive file id.

    Returns:
        A JSON object with common metadata fields.
    """
    fields = (
        "id,name,mimeType,modifiedTime,createdTime,size,owners(displayName,emailAddress),"
        "webViewLink,parents,shared,trashed"
    )
    return json.dumps(_metadata(file_id, fields), indent=2)


def docs_export(file_id: str, mime_type: str = "text/markdown", offset: int = 0) -> str:
    """Export a Google Doc (or other Google-native file) to text.

    Uses *user* credentials. Allowed export types: text/markdown, text/plain,
    text/html, text/csv, application/pdf. Docs export to all but text/csv.
    Sheets export to text/csv (first sheet only) or PDF, and Slides to
    text/plain or PDF. A PDF is saved to a local file, and the result gives
    its path; open that file to read it. Text comes back 50,000 characters
    at a time; a note at the end gives the `offset` of the next window. The
    Drive API refuses exports larger than 10 MB.

    For files that are not Google-native (uploaded PDFs, xlsx, txt, ...) use
    `drive_download` instead.

    Args:
        file_id: The Drive file id of a Google-native document.
        mime_type: Target export MIME type (see allowed list above).
        offset: Character offset to start the text from. Ignored for PDF.

    Returns:
        The exported document as text, or for PDF a JSON object
        `{path, name, mimeType, bytes}` describing the saved file.
    """
    if mime_type not in ALLOWED_EXPORT_TYPES:
        raise WorkspaceError(
            f"Unsupported export mime_type {mime_type!r}. Allowed: "
            f"{', '.join(sorted(ALLOWED_EXPORT_TYPES))}."
        )
    params = urllib.parse.urlencode({"mimeType": mime_type})
    body, _ = request(
        f"{DRIVE_BASE}/files/{resource_id(file_id, name='file_id')}/export?{params}",
        token_cache=USER_TOKEN,
        max_bytes=MAX_DOWNLOAD_BYTES,
    )
    if mime_type == "application/pdf":
        return _save_binary(file_id, mime_type, body)
    return _text_window(body, offset)


def drive_download(file_id: str, offset: int = 0) -> str:
    """Download the content of a non-Google-native Drive file.

    Uses *user* credentials. Works for uploaded files such as PDF, xlsx, txt or
    images, up to 10 MB. Text files come back 50,000 characters at a time; a
    note at the end gives the `offset` of the next window. Anything else is
    saved to a local file, and the result gives its path; open that file to
    read it.

    For Google Docs, Sheets and Slides use `docs_export` instead.

    Args:
        file_id: The Drive file id.
        offset: Character offset to start the text from. Ignored for binary files.

    Returns:
        The file content as text, or for binary files a JSON object
        `{path, name, mimeType, bytes}` describing the saved file.
    """
    meta = _metadata(file_id, "id,name,mimeType,size")
    mime_type = meta.get("mimeType", "application/octet-stream")
    if mime_type.startswith("application/vnd.google-apps."):
        raise WorkspaceError(
            f"{meta.get('name', file_id)!r} is a Google-native file ({mime_type}); "
            "use docs_export instead."
        )
    is_text = _is_text(mime_type)
    size = int(meta.get("size") or 0)
    if size > MAX_DOWNLOAD_BYTES:
        raise WorkspaceError(
            f"File is {size} bytes, over the {MAX_DOWNLOAD_BYTES}-byte download limit."
        )
    params = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    # `size` can be missing from the metadata, so the body is capped as well.
    body, _ = request(
        f"{DRIVE_BASE}/files/{resource_id(file_id, name='file_id')}?{params}",
        token_cache=USER_TOKEN,
        max_bytes=MAX_DOWNLOAD_BYTES,
    )
    if is_text:
        return _text_window(body, offset)
    return _save_binary(file_id, mime_type, body, meta.get("name", ""))


# ---------------------------------- writes ---------------------------------


def _multipart_related(
    metadata: dict[str, Any], media: bytes, media_type: str
) -> tuple[bytes, str]:
    boundary = f"gcloud-workspace-mcp-{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
            json.dumps(metadata).encode(),
            f"\r\n--{boundary}\r\nContent-Type: {media_type}\r\n\r\n".encode(),
            media,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/related; boundary={boundary}"


def docs_create_from_markdown(title: str, markdown: str, parent_id: str = "") -> str:
    """Create a new Google Doc from markdown.

    Uses *user* credentials. Drive converts the markdown into a native Google
    Doc (headings, lists, tables, links, emphasis).

    Args:
        title: Name of the new document.
        markdown: Document content as markdown.
        parent_id: Optional Drive folder id to create the document in. Defaults
            to the root of My Drive.

    Returns:
        A JSON object `{id, name, webViewLink}` for the new document.
    """
    metadata: dict[str, Any] = {"name": title, "mimeType": GOOGLE_DOC_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    data, content_type = _multipart_related(metadata, markdown.encode("utf-8"), "text/markdown")
    params = urllib.parse.urlencode(
        {"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,name,webViewLink"}
    )
    result = send_json(
        f"{DRIVE_UPLOAD_BASE}/files?{params}",
        token_cache=USER_TOKEN,
        data=data,
        content_type=content_type,
    )
    return json.dumps(result, indent=2)


def docs_replace_content(file_id: str, markdown: str) -> str:
    """Replace the whole content of an existing Google Doc with markdown.

    Uses *user* credentials. Only native Google Docs are accepted. Their
    previous content stays reachable through the version history in the Drive
    UI; Drive purges old revisions of uploaded files, so those are refused.

    Args:
        file_id: The Drive file id of an existing Google Doc.
        markdown: The new document content as markdown.

    Returns:
        A JSON object `{id, name, modifiedTime}`.
    """
    meta = _metadata(file_id, "id,name,mimeType")
    mime_type = meta.get("mimeType")
    if mime_type != GOOGLE_DOC_MIME:
        raise WorkspaceError(
            f"{meta.get('name', file_id)!r} is {mime_type}, not a Google Doc; "
            "docs_replace_content only overwrites Google Docs."
        )
    params = urllib.parse.urlencode(
        {"uploadType": "media", "supportsAllDrives": "true", "fields": "id,name,modifiedTime"}
    )
    result = send_json(
        f"{DRIVE_UPLOAD_BASE}/files/{resource_id(file_id, name='file_id')}?{params}",
        token_cache=USER_TOKEN,
        method="PATCH",
        data=markdown.encode("utf-8"),
        content_type="text/markdown; charset=UTF-8",
    )
    return json.dumps(result, indent=2)


def docs_append_text(file_id: str, text: str) -> str:
    """Append plain text to the end of a Google Doc.

    Uses *user* credentials and the Docs API. The text is inserted as-is (no
    markdown conversion); use "\\n" for line breaks.

    Args:
        file_id: The Google Doc id.
        text: Plain text to append.

    Returns:
        A JSON object `{documentId}`.
    """
    payload = {"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]}
    result = send_json(
        f"{DOCS_BASE}/documents/{resource_id(file_id, name='file_id')}:batchUpdate",
        token_cache=USER_TOKEN,
        data=json.dumps(payload).encode("utf-8"),
    )
    return json.dumps({"documentId": result.get("documentId", file_id)}, indent=2)
