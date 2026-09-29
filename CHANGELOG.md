# Changelog

## Unreleased

## 0.1.0 - 2026-09-28

First release.

- Read tools: `drive_search`, `drive_get_metadata`, `docs_export`, `drive_download`,
  `gmail_search`, `gmail_get_message`, `gmail_get_thread`.
- `page_token` pagination for Drive and Gmail search.
- `drive_search` searches every shared drive you belong to (`corpora=allDrives`) and reports
  `incompleteSearch`.
- File, message and thread ids must match `[A-Za-z0-9_-]+`, so `.` and `..` can't reach a
  parent API path.
- `format="text"` for Gmail messages and threads, which decodes the MIME body to plain text.
- Opt-in write tools behind `GCLOUD_WORKSPACE_ENABLE_WRITES=1`: `docs_create_from_markdown`,
  `docs_replace_content`, `docs_append_text`, `gmail_create_draft`.
- Tokens only go to `https://*.googleapis.com`. The server refuses any other URL, including
  a redirect target.
- `docs_replace_content` refuses files that aren't native Google Docs.
- `docs_export` accepts `text/csv`, the only text format Sheets export to.
- `drive_download` and PDF exports save binary files to `GCLOUD_WORKSPACE_DOWNLOAD_DIR` and
  return the path, because base64 in the tool result would exceed the client's output limit.
  Downloads and exports are capped at 10 MB. Text comes back 50,000 characters at a time,
  paged with `offset`.
- Gmail `format="text"` decodes each part in its declared charset, keeps table cells apart and
  shows link targets. A self-closing `<br/>` is one line break. It matches header names in any
  case, so Outlook's `CC` shows up. A thread's bodies share a 100,000-character budget, and
  `omittedBodies` lists the messages past it. The `full`, `metadata` and `minimal` formats cap
  the response at 1 MB.
- `gmail_get_message(format="raw")` saves the decoded message to `<message id>.eml` in the
  download directory, up to 10 MB, and returns the path in place of the base64.
- `gmail_create_draft` threads a reply under the last sent message and skips earlier drafts.
- `Containerfile` with the Google Cloud SDK on a distroless Hummingbird Python image. The build
  checks the SDK tarball's SHA-256 and installs dependencies from `uv.lock` with their hashes.
- CI publishes amd64 and arm64 images to `ghcr.io/thrix/gcloud-workspace-mcp` with build
  provenance attestations.
