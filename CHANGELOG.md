# Changelog

## Unreleased

## 0.1.0 - 2026-09-28

First release.

- Read tools: `drive_search`, `drive_get_metadata`, `docs_export`, `drive_download`,
  `gmail_search`, `gmail_get_message`, `gmail_get_thread`.
- `page_token` pagination for Drive and Gmail search.
- `format="text"` for Gmail messages and threads, which decodes the MIME body to plain text.
- Opt-in write tools behind `GCLOUD_WORKSPACE_ENABLE_WRITES=1`: `docs_create_from_markdown`,
  `docs_replace_content`, `docs_append_text`, `gmail_create_draft`.
- Tokens only go to `https://*.googleapis.com`. The server refuses any other URL.
- `Containerfile` with the Google Cloud SDK on a distroless Hummingbird Python image.
- CI publishes amd64 and arm64 images to `ghcr.io/thrix/gcloud-workspace-mcp` with build
  provenance attestations.
