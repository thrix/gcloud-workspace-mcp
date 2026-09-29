# gcloud-workspace-mcp

An [MCP](https://modelcontextprotocol.io/) server for **Google Docs, Drive and Gmail** that
authenticates through your local `gcloud` CLI. It doesn't need an OAuth client of its own.

## Why it exists

Some Google Workspace tenants block every third-party OAuth client. Other Workspace MCP servers
ask you to register or reuse an OAuth client ID, and on those tenants the sign-in stops at an
"access blocked by your admin" screen. Google's own Cloud SDK client usually stays allowed. So
this server borrows the access tokens that `gcloud` mints on your machine and calls the Google
REST APIs over HTTPS.

If your tenant lets you register an OAuth app, a full-featured server such as
[google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp) will cover more of
the API. Use this one when `gcloud` is the only client you can log in with.

The only dependency is the `mcp` SDK. The HTTP layer uses the Python standard library.

## Two credential stores

`gcloud` keeps two token stores, and you can't swap one for the other. A `403` usually means a
call used the wrong one.

| Surface      | Store    | Token command                                        | Quota header |
| ------------ | -------- | ---------------------------------------------------- | ------------ |
| Docs & Drive | user     | `gcloud auth print-access-token`                     | not needed   |
| Gmail        | ADC      | `gcloud auth application-default print-access-token` | required     |

## One-time setup

Install the [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) so `gcloud` is on your
`PATH`. Then grant the scopes. The server never runs these commands for you.

**Docs and Drive** (user credentials):

```bash
# Grants the full drive scope. gcloud has no read-only variant.
gcloud auth login --enable-gdrive-access
```

**Gmail** (Application Default Credentials):

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/gmail.readonly
gcloud auth application-default set-quota-project <your-quota-project>
```

Keep `cloud-platform` in the list if other tools on your machine use ADC for Google Cloud. The
login rewrites `~/.config/gcloud/application_default_credentials.json` in place, so back it up
first if you care about its current scopes.

## Install and register

You need [`uv`](https://docs.astral.sh/uv/). Register the server in Claude Code:

```bash
claude mcp add -s user gcloud-workspace \
  -e GCLOUD_QUOTA_PROJECT=<your-quota-project> \
  -- uvx --from git+https://github.com/thrix/gcloud-workspace-mcp gcloud-workspace-mcp
```

For another client, add this to its MCP config:

```json
{
  "mcpServers": {
    "gcloud-workspace": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/thrix/gcloud-workspace-mcp",
        "gcloud-workspace-mcp"
      ],
      "env": { "GCLOUD_QUOTA_PROJECT": "<your-quota-project>" }
    }
  }
}
```

Pin a release by appending `@v0.1.0` to the git URL.

### Container

CI publishes a multi-arch image (amd64, arm64) to `ghcr.io/thrix/gcloud-workspace-mcp`. It
holds the server and the Google Cloud SDK on a distroless
[Hummingbird](https://gitlab.com/redhat/hummingbird/containers) Python image. You still do the
one-time `gcloud` logins on your host. The container reads and refreshes the tokens through a
mount of your gcloud config directory.

```bash
mkdir -p -m 700 "$HOME/.cache/gcloud-workspace-mcp/downloads"
claude mcp add -s user gcloud-workspace -- \
  podman run -i --rm --userns=keep-id:uid=65532,gid=65532 \
    -v "$HOME/.config/gcloud:/gcloud:z" \
    -v "$HOME/.cache/gcloud-workspace-mcp/downloads:$HOME/.cache/gcloud-workspace-mcp/downloads:z" \
    -e GCLOUD_WORKSPACE_DOWNLOAD_DIR="$HOME/.cache/gcloud-workspace-mcp/downloads" \
    -e GCLOUD_QUOTA_PROJECT=<your-quota-project> \
    ghcr.io/thrix/gcloud-workspace-mcp:latest
```

`--userns=keep-id` maps your host user to the image's UID 65532, so gcloud can write refreshed
tokens back to your config. The downloads directory is mounted at the same path it has on your
host, so the paths the server returns work outside the container too. Pass `-e GCLOUD_WORKSPACE_ENABLE_WRITES=1` to enable the write tools.

| Tag | Points at |
| --- | --------- |
| `latest`, `0.1`, `0.1.0` | the newest release, and each release line |
| `main` | the tip of `main` |
| `sha-<commit>` | one commit on `main` or a release tag |

Each published image carries a signed build provenance attestation. Check it with
`gh attestation verify oci://ghcr.io/thrix/gcloud-workspace-mcp:latest --owner thrix`.

To build the image yourself, run `podman build -t gcloud-workspace-mcp .` and use
`gcloud-workspace-mcp` as the image name. To pick a different SDK release, pass
`--build-arg GCLOUD_VERSION=<version>` together with that release's `GCLOUD_SHA256_X86_64` and
`GCLOUD_SHA256_ARM`. The build checks the tarball against them.

### Environment

| Variable                         | Meaning                                                  |
| -------------------------------- | -------------------------------------------------------- |
| `GCLOUD_QUOTA_PROJECT`           | ADC quota project, sent as `x-goog-user-project`. Gmail needs it. |
| `GCLOUD_WORKSPACE_ENABLE_WRITES` | Set to `1` to register the write tools. See [Writes](#writes). |
| `GCLOUD_WORKSPACE_DOWNLOAD_DIR`  | Where binary downloads and `raw` Gmail messages go. Defaults to `$XDG_CACHE_HOME/gcloud-workspace-mcp/downloads`, or `~/.cache/...` without `XDG_CACHE_HOME`. |

## Tools

Drive and Docs use the user credentials:

| Tool | What it does |
| ---- | ------------ |
| `drive_search(query, page_size=20, page_token="")` | Search with a raw Drive [`q` query](https://developers.google.com/drive/api/guides/search-files) across My Drive, files shared with you and every shared drive you belong to. Returns `{files, nextPageToken, incompleteSearch}`; `incompleteSearch: true` means Drive skipped some shared drives. |
| `drive_get_metadata(file_id)` | Metadata for one file: owners, size, link, parents. |
| `docs_export(file_id, mime_type="text/markdown", offset=0)` | Export a Google-native file as `text/markdown`, `text/plain`, `text/html`, `text/csv` or `application/pdf`. Docs take every type but `text/csv`. Sheets take `text/csv`, which holds the first sheet only, or PDF. Slides take `text/plain` or PDF. Drive caps exports at 10 MB. |
| `drive_download(file_id, offset=0)` | Download an uploaded file (PDF, xlsx, txt, images), up to 10 MB. |

Text comes back 50,000 characters at a time. When there is more, the result ends with a note
that gives the `offset` for the next call.

PDF exports and binary downloads don't go into the tool result, since base64 would blow past
the client's output limit. The server saves them to the download directory as `<file id>.<ext>`
and returns `{path, name, mimeType, bytes}`. Claude Code can open the PDF or image at that path
with its Read tool. The directory is created with mode `0700` and each file with `0600`. A
second download of the same file overwrites the first.

Gmail uses ADC plus the quota header:

| Tool | What it does |
| ---- | ------------ |
| `gmail_search(query, max_results=20, page_token="")` | Search with Gmail syntax. Returns `{messages, nextPageToken, resultSizeEstimate}`. The estimate stops at 201, so page through results to count them. |
| `gmail_get_message(message_id, format="text")` | `text` gives you `{id, threadId, labelIds, headers, body, attachments}`, with the body decoded from `text/plain` or from tag-stripped `text/html`, in the charset the part declares. `metadata`, `minimal` and `full` return the Gmail resource unchanged, up to 1 MB. `raw` saves the whole message, attachments included, to `<message id>.eml` in the download directory (up to 10 MB) and returns the resource with `path`, `mimeType` and `bytes` in place of the base64. |
| `gmail_get_thread(thread_id, format="text")` | Every message in a thread, oldest first, decoded the same way. The bodies share a 100,000-character budget, filled from the newest message back. Older messages past it keep their headers, get `body: null`, and appear in `omittedBodies` so you can fetch them with `gmail_get_message`. The resource formats cap the whole thread at 1 MB. |

## Writes

The write tools stay unregistered until you set `GCLOUD_WORKSPACE_ENABLE_WRITES=1`, so your MCP
client can't see or call them by default.

| Tool | What it does |
| ---- | ------------ |
| `docs_create_from_markdown(title, markdown, parent_id="")` | Create a Google Doc. Drive converts the markdown to native formatting. |
| `docs_replace_content(file_id, markdown)` | Replace a Doc's whole body with converted markdown. Drive keeps the old content in version history. Refuses any file that isn't a native Google Doc. |
| `docs_append_text(file_id, text)` | Append plain text to the end of a Doc through the Docs API. |
| `gmail_create_draft(to, subject, body, cc="", thread_id="")` | Create a plain-text draft, threaded as a reply when you pass `thread_id`. You send it yourself from Gmail. |

The server offers no tool that sends mail, deletes files or moves them to the trash.

Drafts need one more ADC scope:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose
```

> **Prompt injection.** The model reads your documents and mail, and anyone who can share a doc
> or send you an email can put instructions in it. With writes on, a poisoned doc could get the
> model to overwrite another doc or draft a message to someone. Turn writes on when you need
> them and review what the model changes.

## Security

- The server gets tokens only by reading the stdout of the `gcloud` subprocess. It never puts a
  token on a command line.
- It never logs a token or returns one in a tool result or error message.
- It attaches a token only to `https` URLs on a `*.googleapis.com` host. It checks this before it
  mints the token, checks every redirect target the same way, and refuses every other URL.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Limitations

- You need a logged-in `gcloud` on the same machine. The server can't run an interactive login.
  When `gcloud` asks for reauthentication, the tool error tells you, and you re-run the login.
  The server gives `gcloud` no stdin, so a prompt fails instead of waiting, and it stops a
  `gcloud` call after 60 seconds.
- `print-access-token` doesn't report an expiry. The server assumes a 55-minute lifetime,
  refreshes 5 minutes early, and retries once after a `401`.
- Gmail calls need `GCLOUD_QUOTA_PROJECT`, and they fail with `403 accessNotConfigured` until
  that project enables the Gmail API. Drive and Docs calls don't send that header. They bill the Cloud
  SDK's own project, and `GCLOUD_QUOTA_PROJECT` can't enable an API for them.
- Message bodies get cut at 50,000 characters, and a thread's bodies at 100,000 in total. Attachments come back as names and sizes only.
  HTML-only mail keeps table cells apart and shows each link target after its text.

## License

[MIT](LICENSE)
