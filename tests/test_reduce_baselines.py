"""Tests for scripts/reduce_baselines.py."""

from __future__ import annotations

import json

import pytest

from scripts.reduce_baselines import reduce_baselines
from scripts.check_benchmark_regression import BenchmarkDataError


def _write_raw(path, benchmarks: list[dict], *, machine: str = "Linux") -> None:
    path.write_text(
        json.dumps(
            {
                "machine_info": {"system": machine},
                "benchmarks": benchmarks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_reduce_baselines_groups_and_slack(tmp_path) -> None:
    raw = tmp_path / "raw.json"
    out = tmp_path / "baselines.json"
    _write_raw(
        raw,
        [
            {
                "name": "test_list_workspace_projects_nocache[composers-50]",
                "group": "parse",
                "stats": {"mean": 0.05},
            },
            {
                "name": "test_post_export_zip[composers-10]",
                "group": "export",
                "stats": {"mean": 0.01},
            },
            {
                "name": "test_search_full_corpus",
                "group": "search",
                "stats": {"mean": 0.04},
            },
            {
                "name": "test_summary_cache_lookup[hit]",
                "group": "summary-cache",
                "stats": {"mean": 0.0001},
            },
        ],
    )

    output = reduce_baselines(raw, out, slack=1.5, source="ubuntu-latest-ci")
    data = json.loads(out.read_text(encoding="utf-8"))
    groups = data["groups"]

    assert groups["parse"]["test_list_workspace_projects_nocache[composers-50]"] == pytest.approx(0.075)
    assert groups["export"]["test_post_export_zip[composers-10]"] == pytest.approx(0.015)
    assert groups["search"]["test_search_full_corpus"] == pytest.approx(0.06)
    assert groups["summary-cache"]["test_summary_cache_lookup[hit]"] == pytest.approx(0.00015)
    assert data["machine"] == "Linux"
    assert "ubuntu-latest CI benchmark-results.json" in data["_note"]
    assert "1.5x slack" in data["_note"]
    assert output["groups"] == groups


def test_reduce_baselines_local_source_note(tmp_path) -> None:
    raw = tmp_path / "raw.json"
    out = tmp_path / "baselines.json"
    _write_raw(
        raw,
        [
            {
                "name": "test_summary_cache_lookup[hit]",
                "group": "summary-cache",
                "stats": {"mean": 0.0001},
            },
        ],
        machine="Windows",
    )

    reduce_baselines(raw, out, source="local")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "local benchmark-results.json" in data["_note"]
    assert data["machine"] == "Windows"


def test_reduce_baselines_rejects_unknown_group(tmp_path) -> None:
    raw = tmp_path / "raw.json"
    out = tmp_path / "baselines.json"
    _write_raw(
        raw,
        [
            {
                "name": "test_cache_only",
                "group": "cache",
                "stats": {"mean": 0.001},
            },
        ],
    )

    with pytest.raises(BenchmarkDataError, match="unknown group 'cache'"):
        reduce_baselines(raw, out)


def test_reduce_baselines_rejects_missing_group(tmp_path) -> None:
    raw = tmp_path / "raw.json"
    out = tmp_path / "baselines.json"
    _write_raw(
        raw,
        [
            {
                "name": "test_no_group",
                "stats": {"mean": 0.001},
            },
        ],
    )

    with pytest.raises(BenchmarkDataError, match="missing required 'group'"):
        reduce_baselines(raw, out)


def test_reduce_baselines_rejects_duplicate_normalized_name(tmp_path) -> None:
    raw = tmp_path / "raw.json"
    out = tmp_path / "baselines.json"
    _write_raw(
        raw,
        [
            {
                "name": "test_summary_cache_lookup[hit]",
                "group": "summary-cache",
                "stats": {"mean": 0.0001},
            },
            {
                "name": "tests/benchmarks/test_summary_cache_bench.py::test_summary_cache_lookup[hit]",
                "group": "summary-cache",
                "stats": {"mean": 0.0002},
            },
        ],
    )

    with pytest.raises(BenchmarkDataError, match="duplicates normalized"):
        reduce_baselines(raw, out)


def test_positive_float_rejects_non_finite() -> None:
    import argparse

    from scripts.reduce_baselines import _positive_float

    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        _positive_float("nan")
    with pytest.raises(argparse.ArgumentTypeError, match="finite"):
        _positive_float("inf")
