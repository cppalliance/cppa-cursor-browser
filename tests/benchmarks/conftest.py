"""Synthetic workspace trees for summary-cache performance benchmarks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services import summary_cache
from services.summary_cache import fingerprint_workspace_storage


def make_workspace_entries(workspace_root: Path, count: int) -> list[dict[str, Any]]:
    """Build *count* synthetic workspace entries with on-disk state files."""
    entries: list[dict[str, Any]] = []
    for i in range(count):
        name = f"ws_{i:04d}"
        entry_dir = workspace_root / name
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "state.vscdb").write_bytes(b"bench")
        workspace_json = entry_dir / "workspace.json"
        workspace_json.write_text('{"folder": "/bench"}', encoding="utf-8")
        entries.append(
            {
                "name": name,
                "workspaceJsonPath": str(workspace_json),
            }
        )
    return entries


@pytest.fixture
def summary_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect summary-cache files to an isolated temp directory.

    Patches ``CACHE_DIR`` (also used by tab-summary paths via ``_tab_summaries_path``)
    plus the projects/composer-map file constants used by current benchmarks.
    Tab-summary cache benchmarks are deferred to issue #110 (unified benchmark suite).
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(summary_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(summary_cache, "PROJECTS_CACHE_FILE", cache_dir / "projects.json")
    monkeypatch.setattr(
        summary_cache,
        "COMPOSER_MAP_CACHE_FILE",
        cache_dir / "composer-id-to-ws.json",
    )
    return cache_dir


@pytest.fixture
def sample_projects() -> list[dict[str, Any]]:
    return [
        {
            "id": "ws_0000",
            "name": "Bench Project",
            "conversationCount": 3,
            "lastModified": "2026-06-24T00:00:00Z",
        }
    ]


@pytest.fixture
def synthetic_workspace(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[str, list[dict[str, Any]]]:
    """Workspace path + entries. Parametrize via indirect ``workspace_entry_count``."""
    count = getattr(request, "param", 10)
    workspace_root = tmp_path / "workspaceStorage"
    workspace_root.mkdir()
    entries = make_workspace_entries(workspace_root, count)
    return str(workspace_root), entries


@pytest.fixture
def workspace_fingerprint(synthetic_workspace: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    workspace_path, entries = synthetic_workspace
    return fingerprint_workspace_storage(
        workspace_path,
        entries,
        global_db_path=None,
        rules=[],
    )


@pytest.fixture
def stale_fingerprint(workspace_fingerprint: dict[str, Any]) -> dict[str, Any]:
    """Return a fingerprint guaranteed to differ from the stored one."""
    return {**workspace_fingerprint, "rules_digest": "deadbeefdeadbeef"}
