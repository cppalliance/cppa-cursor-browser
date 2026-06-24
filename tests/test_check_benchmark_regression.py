"""Tests for scripts/check_benchmark_regression.py."""

from __future__ import annotations

import json

import pytest

from scripts.check_benchmark_regression import (
    BenchmarkDataError,
    check_regression,
    load_baseline_means,
    load_results,
    normalize_benchmark_name,
)

GATED_BENCH = "test_summary_cache_hit"


def _write_results(path, benchmarks: list[dict]) -> None:
    path.write_text(
        json.dumps({"benchmarks": benchmarks}, indent=2),
        encoding="utf-8",
    )


def _write_baselines(path, groups: dict[str, dict[str, float]]) -> None:
    path.write_text(
        json.dumps({"groups": groups}, indent=2),
        encoding="utf-8",
    )


def test_normalize_benchmark_name_strips_module_prefix() -> None:
    full = "tests/benchmarks/test_summary_cache_bench.py::test_summary_cache_hit"
    assert normalize_benchmark_name(full) == "test_summary_cache_hit"
    assert normalize_benchmark_name("test_summary_cache_hit") == "test_summary_cache_hit"


def test_normalize_benchmark_name_preserves_colons_in_param_values() -> None:
    short = "test_x[param::v]"
    full = f"tests/benchmarks/test_x.py::{short}"
    assert normalize_benchmark_name(short) == short
    assert normalize_benchmark_name(full) == short


def test_load_results_normalizes_full_node_id(tmp_path) -> None:
    path = tmp_path / "results.json"
    _write_results(
        path,
        [
            {
                "name": "tests/benchmarks/test_summary_cache_bench.py::test_summary_cache_hit",
                "stats": {"mean": 0.0001},
            }
        ],
    )

    assert load_results(path)["test_summary_cache_hit"] == pytest.approx(0.0001)


def test_missing_baseline_warns_without_failing(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(
        results,
        [
            {"name": "test_new_bench", "stats": {"mean": 0.01}},
            {"name": GATED_BENCH, "stats": {"mean": 0.0001}},
        ],
    )
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0001}},
    )

    assert check_regression(results, baselines) == 0
    out = capsys.readouterr().out
    assert "WARN: 'test_new_bench' has no baseline yet" in out


def test_regression_over_threshold_fails(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(
        results,
        [{"name": GATED_BENCH, "stats": {"mean": 0.00025}}],
    )
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0002}},
    )

    assert check_regression(results, baselines) == 1
    out = capsys.readouterr().out
    assert "REGRESSION" in out


def test_within_threshold_passes(tmp_path) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(
        results,
        [{"name": GATED_BENCH, "stats": {"mean": 0.00022}}],
    )
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0002}},
    )

    assert check_regression(results, baselines) == 0


def test_load_results_rejects_malformed_json(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchmarkDataError, match="invalid JSON"):
        load_results(path)


def test_load_results_requires_benchmarks_array(tmp_path) -> None:
    path = tmp_path / "results.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(BenchmarkDataError, match="'benchmarks' array"):
        load_results(path)


def test_load_results_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(BenchmarkDataError, match="cannot read"):
        load_results(tmp_path / "missing.json")


def test_zero_baseline_skips_ratio_check(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(
        results,
        [{"name": GATED_BENCH, "stats": {"mean": 0.00025}}],
    )
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0}},
    )

    assert check_regression(results, baselines) == 0
    assert f"baseline for '{GATED_BENCH}' is zero" in capsys.readouterr().out


def test_exactly_at_threshold_passes(tmp_path) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(
        results,
        [{"name": GATED_BENCH, "stats": {"mean": 0.00024}}],
    )
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0002}},
    )

    assert check_regression(results, baselines) == 0


def test_missing_current_result_fails(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    results = tmp_path / "results.json"
    baselines = tmp_path / "baselines.json"
    _write_results(results, [])
    _write_baselines(
        baselines,
        {"summary-cache": {GATED_BENCH: 0.0002}},
    )

    assert check_regression(results, baselines) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "no current result for gated baseline" in out


def test_main_reports_benchmark_data_error(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.check_benchmark_regression import main

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    baselines = tmp_path / "baselines.json"
    _write_baselines(baselines, {"summary-cache": {GATED_BENCH: 0.0002}})

    assert main([str(bad), str(baselines)]) == 2
    assert "ERROR:" in capsys.readouterr().err


def test_duplicate_baseline_name_raises(tmp_path) -> None:
    baselines = tmp_path / "baselines.json"
    _write_baselines(
        baselines,
        {
            "summary-cache": {GATED_BENCH: 0.0002},
            "export": {GATED_BENCH: 0.0003},
        },
    )

    with pytest.raises(BenchmarkDataError, match="duplicate benchmark name"):
        load_baseline_means(baselines)


def test_load_baseline_means_rejects_non_dict_group(tmp_path) -> None:
    baselines = tmp_path / "baselines.json"
    baselines.write_text(
        json.dumps({"groups": {"summary-cache": "not-a-dict"}}),
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkDataError, match="must be an object"):
        load_baseline_means(baselines)
