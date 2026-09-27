"""Google Drive and Docs tools. All of them use the *user* credentials."""

from __future__ import annotations

import base64
import json
import urllib.parse
import uuid
from typing import Any

from .auth import USER_TOKEN, WorkspaceError
from .http import DOCS_BASE, DRIVE_BASE, DRIVE_UPLOAD_BASE, get_json, quote, request, send_json

# Export MIME types Google Docs can be exported to via the Drive API.
ALLOWED_EXPORT_TYPES = {
    "text/markdown",
    "text/plain",
    "text/html",
    "application/pdf",
}

# Refuse to download files larger than this; the whole body goes to the client.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024

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


def _binary_blob(mime_type: str, body: bytes) -> str:
    encoded = base64.b64encode(body).decode("ascii")
    return f"[binary {mime_type}, base64-encoded below; {len(body)} bytes]\n{encoded}"


# ---------------------------------- reads ----------------------------------


def drive_search(query: str, page_size: int = 20, page_token: str = "") -> str:
    """Search Google Drive files using a raw Drive `q` query string.

    Uses *user* credentials (`gcloud auth print-access-token`). Searches shared
    drives too.

    Args:
        query: A raw Drive v3 `q` expression, e.g.
            "name contains 'design' and mimeType='application/vnd.google-apps.document'".
            See https://developers.google.com/drive/api/guides/search-files
        page_size: Maximum number of files to return (1-100).
        page_token: The `nextPageToken` from a previous call, to fetch the next page.

    Returns:
        A JSON object `{files: [{id, name, mimeType, modifiedTime}], nextPageToken}`.
        `nextPageToken` is null on the last page.
    """
    params = {
        "q": query,
        "pageSize": max(1, min(int(page_size), 100)),
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    data = get_json(f"{DRIVE_BASE}/files?{urllib.parse.urlencode(params)}", token_cache=USER_TOKEN)
    return json.dumps(
        {"files": data.get("files", []), "nextPageToken": data.get("nextPageToken")},
        indent=2,
    )


def _metadata(file_id: str, fields: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"fields": fields, "supportsAllDrives": "true"})
    data: dict[str, Any] = get_json(
        f"{DRIVE_BASE}/files/{quote(file_id)}?{params}", token_cache=USER_TOKEN
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


def docs_export(file_id: str, mime_type: str = "text/markdown") -> str:
    """Export a Google Doc (or other Google-native file) to text.

    Uses *user* credentials. Allowed export types: text/markdown, text/plain,
    text/html, application/pdf. PDF is binary and is returned base64-encoded
    with a note. The Drive API refuses exports larger than 10 MB.

    For files that are not Google-native (uploaded PDFs, xlsx, txt, ...) use
    `drive_download` instead.

    Args:
        file_id: The Drive file id of a Google-native document.
        mime_type: Target export MIME type (see allowed list above).

    Returns:
        The exported document as text, or a base64 blob for PDF.
    """
    if mime_type not in ALLOWED_EXPORT_TYPES:
        raise WorkspaceError(
            f"Unsupported export mime_type {mime_type!r}. Allowed: "
            f"{', '.join(sorted(ALLOWED_EXPORT_TYPES))}."
        )
    params = urllib.parse.urlencode({"mimeType": mime_type})
    body, _ = request(
        f"{DRIVE_BASE}/files/{quote(file_id)}/export?{params}", token_cache=USER_TOKEN
    )
    if mime_type == "application/pdf":
        return _binary_blob(mime_type, body)
    return body.decode("utf-8", "replace")


def drive_download(file_id: str) -> str:
    """Download the content of a non-Google-native Drive file.

    Uses *user* credentials. Works for uploaded files such as PDF, xlsx, txt or
    images. Text types are returned as text; anything else is returned
    base64-encoded with a note. Files over 10 MB are refused.

    For Google Docs, Sheets and Slides use `docs_export` instead.

    Args:
        file_id: The Drive file id.

    Returns:
        The file content as text, or a base64 blob for binary files.
    """
    meta = _metadata(file_id, "id,name,mimeType,size")
    mime_type = meta.get("mimeType", "application/octet-stream")
    if mime_type.startswith("application/vnd.google-apps."):
        raise WorkspaceError(
            f"{meta.get('name', file_id)!r} is a Google-native file ({mime_type}); "
            "use docs_export instead."
        )
    size = int(meta.get("size") or 0)
    if size > MAX_DOWNLOAD_BYTES:
        raise WorkspaceError(
            f"File is {size} bytes, over the {MAX_DOWNLOAD_BYTES}-byte download limit."
        )
    params = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    body, _ = request(f"{DRIVE_BASE}/files/{quote(file_id)}?{params}", token_cache=USER_TOKEN)
    if _is_text(mime_type):
        return body.decode("utf-8", "replace")
    return _binary_blob(mime_type, body)


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

    Uses *user* credentials. The previous content stays reachable through the
    document's version history in the Drive UI.

    Args:
        file_id: The Drive file id of an existing Google Doc.
        markdown: The new document content as markdown.

    Returns:
        A JSON object `{id, name, modifiedTime}`.
    """
    params = urllib.parse.urlencode(
        {"uploadType": "media", "supportsAllDrives": "true", "fields": "id,name,modifiedTime"}
    )
    result = send_json(
        f"{DRIVE_UPLOAD_BASE}/files/{quote(file_id)}?{params}",
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
        f"{DOCS_BASE}/documents/{quote(file_id)}:batchUpdate",
        token_cache=USER_TOKEN,
        data=json.dumps(payload).encode("utf-8"),
    )
    return json.dumps({"documentId": result.get("documentId", file_id)}, indent=2)
