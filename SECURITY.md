# Security policy

This server holds Google access tokens for your Drive and Gmail. It must uphold these rules:

1. It gets tokens only from the stdout of `gcloud auth print-access-token` or
   `gcloud auth application-default print-access-token`.
2. It never passes a token as a command-line argument.
3. It never logs a token or puts one in a tool result or error message.
4. It attaches a token only to `https` requests for a `*.googleapis.com` host.
   `gcloud_workspace_mcp.http.check_url` enforces this.
5. It registers write tools only when you set `GCLOUD_WORKSPACE_ENABLE_WRITES=1`, and it has no
   tool that sends mail or deletes anything.

A breach of any of these counts as a vulnerability.

## Reporting

Report it privately through
[GitHub security advisories](https://github.com/thrix/gcloud-workspace-mcp/security/advisories/new).
Don't open a public issue. Expect a reply within a week.

Only the latest release gets fixes.
