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
- packaging: `validate-pyproject` and an up-to-date `uv.lock`, which the container build
  installs from
- docs and config: `codespell`, `yamllint`, `markdownlint-cli2`
- CI and container: `actionlint`, the GitHub workflow schema, `hadolint` on the `Containerfile`
- secrets: `gitleaks`

Run them all by hand with `uv run pre-commit run --all-files`.

## Tests

```bash
uv run pytest --cov
```

The suite has to keep 100% line and branch coverage; `--cov` fails below that. CI runs the
tests on Python 3.10 through 3.14, runs every pre-commit hook, and builds and tests the
container image for amd64 and arm64. Pushes to `main` and `v*` tags publish the image to
`ghcr.io`.

## Rules

- Tests must not touch the network or a real `gcloud`. Use the fakes in `tests/conftest.py`,
  which patch `subprocess.run` and the opener in `gcloud_workspace_mcp.http`. `tests/test_stdio.py` starts
  the real server process with a fake `gcloud` on `PATH`.
- Keep the HTTP layer on the standard library. A new dependency needs a strong reason.
- Follow the token rules at the top of `src/gcloud_workspace_mcp/auth.py`. A change that sends a
  token anywhere except `https://*.googleapis.com` won't be merged.
- A new write tool goes in `WRITE_TOOLS` in `server.py`, so it stays behind
  `GCLOUD_WORKSPACE_ENABLE_WRITES`. Add it to the leak test table in `tests/test_server.py`.
- Sign off each commit with `git commit -s` to certify the
  [Developer Certificate of Origin](https://developercertificate.org/).
- Add a line to `CHANGELOG.md` under `Unreleased`.
- To bump the Google Cloud SDK, change `GCLOUD_VERSION` and both `GCLOUD_SHA256_*` values in
  the `Containerfile` together. Take the hashes from Google's
  [versioned archives](https://cloud.google.com/sdk/docs/downloads-versioned-archives) page.
- Workflows pin each action to a commit SHA with the version in a comment. Dependabot bumps
  them.
