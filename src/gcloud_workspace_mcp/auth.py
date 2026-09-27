"""Access tokens borrowed from the local `gcloud` CLI.

Two separate credential stores are used, and they are not interchangeable
(a 403 almost always means the wrong store was used):

  * Docs & Drive use the *user* credentials:
        gcloud auth print-access-token
    Granted once, out of band:
        gcloud auth login --enable-gdrive-access

  * Gmail uses *Application Default Credentials* (ADC):
        gcloud auth application-default print-access-token
    Granted once, out of band, with the Gmail scopes and a quota project.
    Every Gmail REST call must send `x-goog-user-project: <quota-project>`.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

from mcp.server.mcpserver.exceptions import ToolError

# ---------------------------------------------------------------------------
# SECURITY — token handling rules (hard requirements, do not relax)
# ---------------------------------------------------------------------------
#  * Tokens are obtained ONLY by capturing the stdout of the `gcloud`
#    subprocess into an in-process Python variable.
#  * A token is NEVER passed as a command-line argument to any subprocess or
#    external tool (that would leak it via the process table / shell history).
#  * A token is NEVER logged, printed, echoed, or included in an error message
#    or tool result.
#  * A token is NEVER sent to any third party. It stays in this process and is
#    attached only to https requests aimed at *.googleapis.com (enforced in
#    `http.py`).
# ---------------------------------------------------------------------------

# Refresh a token when it is within this many seconds of its (assumed) expiry.
REFRESH_SKEW_SECONDS = 5 * 60
# gcloud access tokens last ~1h; `print-access-token` does not reliably report
# the expiry, so we assume a conservative lifetime and refresh early.
ASSUMED_TTL_SECONDS = 55 * 60


class WorkspaceError(ToolError):
    """Raised for auth/HTTP failures; message is safe to surface (no token).

    Subclassing `ToolError` makes the MCP SDK hand the message to the client.
    Any other exception reaches the client as a bare "Error executing tool".
    """


@dataclass
class TokenCache:
    """In-memory cache for a single gcloud-minted access token.

    The token string lives only in ``_value`` and is never logged or returned.
    """

    # The gcloud invocation that mints the token. Note: no token ever appears
    # on a command line — this command *produces* one on stdout.
    command: list[str]
    _value: str | None = field(default=None, repr=False)
    _expires_at: float = 0.0

    def _fresh(self) -> bool:
        return (
            self._value is not None and time.monotonic() < self._expires_at - REFRESH_SKEW_SECONDS
        )

    def get(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing via gcloud if needed."""
        if force_refresh or not self._fresh():
            self._refresh()
        assert self._value is not None  # noqa: S101
        return self._value

    def _refresh(self) -> None:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no user input
                self.command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkspaceError(
                "`gcloud` CLI not found on PATH. Install the Google Cloud SDK "
                "and authenticate (see the README)."
            ) from exc

        if proc.returncode != 0:
            # stderr from gcloud may explain a missing login; it does not
            # contain a token, so it is safe to surface.
            stderr = (proc.stderr or "").strip()
            raise WorkspaceError(
                f"gcloud failed to mint an access token (exit {proc.returncode}). "
                f"Have you completed the one-time login? Detail: {stderr}"
            )

        token = proc.stdout.strip()
        if not token:
            raise WorkspaceError(
                "gcloud returned an empty access token; re-run the one-time "
                "login documented in the README."
            )
        # The token is stored only here, in memory. Never logged / returned.
        self._value = token
        self._expires_at = time.monotonic() + ASSUMED_TTL_SECONDS


# Two independent caches — user creds (Docs/Drive) vs ADC creds (Gmail).
USER_TOKEN = TokenCache(command=["gcloud", "auth", "print-access-token"])
ADC_TOKEN = TokenCache(command=["gcloud", "auth", "application-default", "print-access-token"])
