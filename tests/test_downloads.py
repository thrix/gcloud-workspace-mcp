from __future__ import annotations

from pathlib import Path

import pytest

from gcloud_workspace_mcp import downloads


def test_download_dir_defaults_to_xdg_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(downloads.DOWNLOAD_DIR_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert downloads.download_dir() == tmp_path / "gcloud-workspace-mcp" / "downloads"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert downloads.download_dir() == tmp_path / ".cache" / "gcloud-workspace-mcp" / "downloads"


def test_download_dir_env_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(downloads.DOWNLOAD_DIR_ENV, "~/dl")
    assert downloads.download_dir() == tmp_path / "dl"
