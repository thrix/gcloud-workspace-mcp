# Contributing

Bug reports and pull requests are welcome.

## Setup

You need [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install
```

The hooks run on every commit, and the `commit-msg` hook rejects a commit without a
`Signed-off-by` line. They cover:

- file hygiene: whitespace, line endings, merge markers, large files, private keys
- Python: `ruff check`, `ruff format`, strict `mypy` on `src/`, and `pytest`
- packaging: `validate-pyproject` and an up-to-date `uv.lock`
- docs and config: `codespell`, `yamllint`, `markdownlint-cli2`
- CI and container: `actionlint`, the GitHub workflow schema, `hadolint` on the `Containerfile`
- secrets: `gitleaks`

Run them all by hand with `uv run pre-commit run --all-files`.

## Tests

```bash
uv run pytest --cov
```

The suite has to keep 100% line and branch coverage; `--cov` fails below that. CI runs the
tests on Python 3.10 through 3.14, runs every pre-commit hook, and builds the container image.

## Rules

- Tests must not touch the network or a real `gcloud`. Use the fakes in `tests/conftest.py`,
  which patch `subprocess.run` and `urllib.request.urlopen`. `tests/test_stdio.py` starts
  the real server process with a fake `gcloud` on `PATH`.
- Keep the HTTP layer on the standard library. A new dependency needs a strong reason.
- Follow the token rules at the top of `src/gcloud_workspace_mcp/auth.py`. A change that sends a
  token anywhere except `https://*.googleapis.com` won't be merged.
- A new write tool goes in `WRITE_TOOLS` in `server.py`, so it stays behind
  `GCLOUD_WORKSPACE_ENABLE_WRITES`. Add it to the leak test table in `tests/test_server.py`.
- Sign off each commit with `git commit -s` to certify the
  [Developer Certificate of Origin](https://developercertificate.org/).
- Add a line to `CHANGELOG.md` under `Unreleased`.
