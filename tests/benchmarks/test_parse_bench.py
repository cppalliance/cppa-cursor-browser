"""Benchmark list_workspace_projects (nocache) over synthetic composer corpora."""

from __future__ import annotations

import pytest

from services.workspace_listing import list_workspace_projects


@pytest.mark.benchmark(group="parse")
@pytest.mark.parametrize(
    "bench_storage",
    [10, 50, 200],
    indirect=True,
    ids=["composers-10", "composers-50", "composers-200"],
)
def test_list_workspace_projects_nocache(
    benchmark,
    bench_env: dict[str, str],
) -> None:
    workspace_path = bench_env["workspace_path"]

    def _run() -> object:
        return list_workspace_projects(workspace_path, [], nocache=True)

    projects, warnings = benchmark(_run)
    assert isinstance(projects, list) and len(projects) > 0
    assert warnings == []
