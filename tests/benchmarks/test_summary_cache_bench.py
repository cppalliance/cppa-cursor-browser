"""pytest-benchmark coverage for services/summary_cache.py hot paths."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.summary_cache import (
    fingerprint_workspace_storage,
    get_cached_projects,
    set_cached_projects,
)

@pytest.mark.benchmark(group="summary-cache")
def test_summary_cache_hit(
    benchmark,
    summary_cache_dir: Path,
    workspace_fingerprint: dict[str, Any],
    sample_projects: list[dict[str, Any]],
) -> None:
    set_cached_projects(workspace_fingerprint, sample_projects, [])
    benchmark(get_cached_projects, workspace_fingerprint)


@pytest.mark.benchmark(group="summary-cache")
def test_summary_cache_miss(
    benchmark,
    summary_cache_dir: Path,
    workspace_fingerprint: dict[str, Any],
    stale_fingerprint: dict[str, Any],
    sample_projects: list[dict[str, Any]],
) -> None:
    set_cached_projects(workspace_fingerprint, sample_projects, [])
    benchmark(get_cached_projects, stale_fingerprint)


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
