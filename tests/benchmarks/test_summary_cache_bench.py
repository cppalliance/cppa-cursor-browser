"""pytest-benchmark coverage for services/summary_cache.py hot paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from services.summary_cache import (
    fingerprint_workspace_storage,
    get_cached_projects,
    get_cached_tab_summaries,
    set_cached_projects,
    set_cached_tab_summaries,
)


@pytest.mark.benchmark(group="summary-cache")
@pytest.mark.parametrize("mode", ["hit", "miss"], ids=["hit", "miss"])
def test_summary_cache_lookup(
    benchmark,
    mode: Literal["hit", "miss"],
    summary_cache_dir: Path,
    workspace_fingerprint: dict[str, Any],
    stale_fingerprint: dict[str, Any],
    sample_projects: list[dict[str, Any]],
) -> None:
    """Time ``get_cached_projects`` only; miss = fingerprint mismatch, not rebuild."""
    set_cached_projects(workspace_fingerprint, sample_projects, [])
    lookup_fp = workspace_fingerprint if mode == "hit" else stale_fingerprint
    result = benchmark(get_cached_projects, lookup_fp)
    if mode == "hit":
        assert result is not None
        projects, warnings = result
        assert projects == sample_projects
        assert warnings == []
    else:
        assert result is None


@pytest.mark.benchmark(group="summary-cache")
@pytest.mark.parametrize(
    "synthetic_workspace",
    [10, 50, 200],
    indirect=True,
)
def test_fingerprint_workspace_entries(
    benchmark,
    synthetic_workspace: tuple[str, list[dict[str, Any]]],
) -> None:
    workspace_path, entries = synthetic_workspace
    benchmark(
        fingerprint_workspace_storage,
        workspace_path,
        entries,
        global_db_path=None,
        rules=[],
    )


@pytest.mark.benchmark(group="summary-cache")
def test_summary_cache_round_trip(
    benchmark,
    summary_cache_dir: Path,
    workspace_fingerprint: dict[str, Any],
    sample_projects: list[dict[str, Any]],
) -> None:
    fp = workspace_fingerprint
    projects = sample_projects

    def _run() -> None:
        set_cached_projects(fp, projects, [])
        get_cached_projects(fp)

    benchmark(_run)


@pytest.mark.benchmark(group="summary-cache")
@pytest.mark.parametrize("mode", ["hit", "miss"], ids=["hit", "miss"])
def test_tab_summary_cache_lookup(
    benchmark,
    mode: Literal["hit", "miss"],
    summary_cache_dir: Path,
    workspace_fingerprint: dict[str, Any],
    stale_fingerprint: dict[str, Any],
) -> None:
    workspace_id = "ws_0000"
    payload = {"tabs": [{"id": "cmp_0000", "title": "Bench"}]}
    set_cached_tab_summaries(workspace_fingerprint, workspace_id, payload, 200)
    lookup_fp = workspace_fingerprint if mode == "hit" else stale_fingerprint
    result = benchmark(get_cached_tab_summaries, lookup_fp, workspace_id)
    if mode == "hit":
        assert result is not None
        cached_payload, status = result
        assert status == 200
        assert cached_payload == payload
    else:
        assert result is None
