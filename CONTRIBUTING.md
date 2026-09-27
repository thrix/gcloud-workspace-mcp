# Contributing

Bug reports and pull requests are welcome.

## Setup

You need [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install   # optional; CI runs the same checks
```

## Checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

CI runs all three on Python 3.10 through 3.14.

## Rules

- Tests must not touch the network or a real `gcloud`. Use the fakes in `tests/conftest.py`,
  which patch `subprocess.run` and `urllib.request.urlopen`.
- Keep the HTTP layer on the standard library. A new dependency needs a strong reason.
- Follow the token rules at the top of `src/gcloud_workspace_mcp/auth.py`. A change that sends a
  token anywhere except `https://*.googleapis.com` won't be merged.
- A new write tool goes in `WRITE_TOOLS` in `server.py`, so it stays behind
  `GCLOUD_WORKSPACE_ENABLE_WRITES`. Add it to the leak test table in `tests/test_server.py`.
- Sign off each commit with `git commit -s` to certify the
  [Developer Certificate of Origin](https://developercertificate.org/).
- Add a line to `CHANGELOG.md` under `Unreleased`.
