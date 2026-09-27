from __future__ import annotations

import time

import pytest
from conftest import FakeGcloud

from gcloud_workspace_mcp import auth
from gcloud_workspace_mcp.auth import TokenCache, WorkspaceError


def test_token_is_cached(gcloud: FakeGcloud) -> None:
    cache = TokenCache(command=["gcloud", "auth", "print-access-token"])
    assert cache.get() == cache.get()
    assert len(gcloud.calls) == 1


def test_token_command_carries_no_token(gcloud: FakeGcloud) -> None:
    auth.USER_TOKEN.get()
    auth.ADC_TOKEN.get()
    assert gcloud.calls == [
        ["gcloud", "auth", "print-access-token"],
        ["gcloud", "auth", "application-default", "print-access-token"],
    ]


def test_token_refreshes_near_expiry(gcloud: FakeGcloud, monkeypatch: pytest.MonkeyPatch) -> None:
    gcloud.tokens = ["first", "second"]
    cache = TokenCache(command=["gcloud"])
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    assert cache.get() == "first"

    # Still fresh just before the refresh window opens.
    skew = auth.REFRESH_SKEW_SECONDS
    monkeypatch.setattr(time, "monotonic", lambda: now + auth.ASSUMED_TTL_SECONDS - skew - 1)
    assert cache.get() == "first"

    # Inside the skew window the token is refreshed.
    monkeypatch.setattr(time, "monotonic", lambda: now + auth.ASSUMED_TTL_SECONDS - skew + 1)
    assert cache.get() == "second"


def test_force_refresh(gcloud: FakeGcloud) -> None:
    gcloud.tokens = ["first", "second"]
    cache = TokenCache(command=["gcloud"])
    assert cache.get() == "first"
    assert cache.get(force_refresh=True) == "second"


def test_gcloud_failure_is_reported(gcloud: FakeGcloud) -> None:
    gcloud.returncode = 1
    gcloud.stderr = "You do not currently have an active account selected."
    with pytest.raises(WorkspaceError, match="active account"):
        TokenCache(command=["gcloud"]).get()


def test_empty_token(gcloud: FakeGcloud) -> None:
    gcloud.tokens = [""]
    with pytest.raises(WorkspaceError, match="empty access token"):
        TokenCache(command=["gcloud"]).get()


def test_gcloud_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("gcloud")

    monkeypatch.setattr(auth.subprocess, "run", missing)
    with pytest.raises(WorkspaceError, match="not found on PATH"):
        TokenCache(command=["gcloud"]).get()
