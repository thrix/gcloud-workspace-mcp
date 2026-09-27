"""Shared fixtures. Tests never touch the network or the real `gcloud`."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

import pytest

from gcloud_workspace_mcp import auth

# A recognizable token; tests assert it never shows up in any output.
SENTINEL_TOKEN = "ya29.SENTINEL-TOKEN-must-never-leak"


@dataclass
class FakeResponse:
    body: bytes
    content_type: str = "application/json"

    def __post_init__(self) -> None:
        self.headers = Message()
        self.headers["Content-Type"] = self.content_type

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def http_error(
    url: str, code: int, message: str = "", reason: str = "err"
) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    return urllib.error.HTTPError(url, code, reason, Message(), io.BytesIO(body))


@dataclass
class FakeGoogle:
    """Records outbound requests and replays queued responses or handlers."""

    requests: list[urllib.request.Request] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)

    def queue(self, *items: Any) -> None:
        self.responses.extend(items)

    def queue_json(self, *payloads: Any) -> None:
        self.queue(*(FakeResponse(json.dumps(p).encode()) for p in payloads))

    def urlopen(self, req: urllib.request.Request, *args: Any, **kwargs: Any) -> FakeResponse:
        self.requests.append(req)
        if not self.responses:
            raise AssertionError(f"unexpected request to {req.full_url}")
        item = self.responses.pop(0)
        if callable(item):
            item = item(req)
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class FakeGcloud:
    """Stands in for `subprocess.run` of the gcloud token commands."""

    tokens: list[str] = field(default_factory=lambda: [SENTINEL_TOKEN])
    calls: list[list[str]] = field(default_factory=list)
    returncode: int = 0
    stderr: str = ""

    def run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        token = self.tokens[min(len(self.calls) - 1, len(self.tokens) - 1)]
        return subprocess.CompletedProcess(cmd, self.returncode, token + "\n", self.stderr)


@pytest.fixture(autouse=True)
def _reset_token_caches() -> None:
    for cache in (auth.USER_TOKEN, auth.ADC_TOKEN):
        cache._value = None
        cache._expires_at = 0.0


@pytest.fixture
def gcloud(monkeypatch: pytest.MonkeyPatch) -> FakeGcloud:
    fake = FakeGcloud()
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch, gcloud: FakeGcloud) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setenv("GCLOUD_QUOTA_PROJECT", "test-quota-project")
    return fake


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
