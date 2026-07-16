"""Compare pytest-benchmark JSON output against stored baselines."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

THRESHOLD = 1.20
STALE_FLOOR = 0.50

# Benchmarks recorded in baselines.json but excluded from the regression gate.
# Use sparingly — only for benches whose timing is inherently noisy across CI runs
# (e.g. file I/O operations that depend on OS page-cache state).
EXCLUDED_FROM_GATE: frozenset[str] = frozenset(
    {
        # round_trip calls set_cached_projects (file write) + get_cached_projects (file read)
        # each round. OS page-cache state on shared runners causes 3-5x variation between
        # consecutive CI runs, making this ungatable with any reasonable slack.
        "test_summary_cache_round_trip",
        # Sub-100µs in-memory cache lookups vary 2.5x+ between consecutive ubuntu-latest
        # runs; gated ratio band (0.5x to 1.2x) cannot bracket both without false failures.
        "test_summary_cache_lookup[hit]",
        "test_summary_cache_lookup[miss]",
        "test_composer_map_cache_lookup[hit]",
        "test_composer_map_cache_lookup[miss]",
        "test_tab_summary_cache_lookup[hit]",
        "test_tab_summary_cache_lookup[miss]",
    }
)


class BenchmarkDataError(ValueError):
    """Raised when benchmark JSON input is malformed or missing required fields."""


def normalize_benchmark_name(name: str) -> str:
    """Strip pytest file node prefix so baselines match short or full benchmark names."""
    text = str(name)
    if "::" not in text:
        return text
    prefix, _, suffix = text.partition("::")
    # Only strip module paths (…/test_foo.py::test_name); leave "::" inside [param::value] intact.
    if prefix.endswith(".py"):
        return suffix
    return text


def load_results(results_path: str | Path) -> dict[str, float]:
    path = Path(results_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkDataError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkDataError(f"invalid JSON in {path}: {exc}") from exc
    try:
        benchmarks = data["benchmarks"]
    except (KeyError, TypeError) as exc:
        raise BenchmarkDataError(f"{path} missing top-level 'benchmarks' array") from exc
    if not isinstance(benchmarks, list):
        raise BenchmarkDataError(f"{path} 'benchmarks' must be an array")

    results: dict[str, float] = {}
    for index, entry in enumerate(benchmarks):
        if not isinstance(entry, dict):
            raise BenchmarkDataError(f"{path} benchmarks[{index}] must be an object")
        try:
            raw_name = entry["name"]
            mean = float(entry["stats"]["mean"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkDataError(
                f"{path} benchmarks[{index}] missing 'name' or 'stats.mean'"
            ) from exc
        name = normalize_benchmark_name(str(raw_name))
        if name in results:
            raise BenchmarkDataError(f"{path} duplicate benchmark name {name!r}")
        results[name] = mean
    return results


def load_baseline_means(baselines_path: str | Path) -> dict[str, float]:
    path = Path(baselines_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkDataError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkDataError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarkDataError(f"{path} root value must be an object")

    if "groups" not in data:
        raise BenchmarkDataError(f"{path} missing required 'groups' key")
    groups = data["groups"]
    if not isinstance(groups, dict):
        raise BenchmarkDataError(f"{path} 'groups' must be an object")

    means: dict[str, float] = {}
    for group_name, value in groups.items():
        if not isinstance(value, dict):
            raise BenchmarkDataError(
                f"{path} groups[{group_name!r}] must be an object of benchmark means"
            )
        for name, mean in value.items():
            bench_name = normalize_benchmark_name(str(name))
            if bench_name in means:
                raise BenchmarkDataError(
                    f"{path} duplicate benchmark name {bench_name!r} across groups"
                )
            try:
                means[bench_name] = float(mean)
            except (TypeError, ValueError) as exc:
                raise BenchmarkDataError(
                    f"{path} groups[{group_name!r}][{name!r}] is not a numeric mean"
                ) from exc
    return means


def _validate_gate_ratios(threshold: float, stale_floor: float) -> None:
    if not math.isfinite(threshold):
        raise BenchmarkDataError("threshold must be finite")
    if threshold <= 1:
        raise BenchmarkDataError("threshold must be greater than 1")
    if not math.isfinite(stale_floor):
        raise BenchmarkDataError("stale_floor must be finite")
    if not 0 < stale_floor < 1:
        raise BenchmarkDataError("stale_floor must be between 0 and 1 (exclusive)")


def check_regression(
    results_path: str | Path,
    baselines_path: str | Path,
    *,
    threshold: float = THRESHOLD,
    stale_floor: float = STALE_FLOOR,
) -> int:
    """Return 0 when within threshold; 1 when any gated benchmark regresses or is stale."""
    _validate_gate_ratios(threshold, stale_floor)
    flat = load_results(results_path)
    baseline_means = load_baseline_means(baselines_path)

    failures: list[str] = []
    stale: list[str] = []
    missing: list[str] = []
    for name, base in baseline_means.items():
        if name in EXCLUDED_FROM_GATE:
            continue
        cur = flat.get(name)
        if cur is None:
            print(f"FAIL: no current result for gated baseline {name!r}")
            missing.append(name)
            continue
        if base == 0:
            print(f"WARN: baseline for {name!r} is zero; skipping ratio check")
            continue
        ratio = cur / base
        if ratio > threshold:
            tag = "FAIL"
            failures.append(name)
        elif ratio < stale_floor:
            tag = "STALE"
            stale.append(name)
        else:
            tag = "ok"
        print(f"[{tag}] {name}: {cur:.6f}s vs {base:.6f}s ({ratio:.2f}x)")

    for name in flat:
        if name in EXCLUDED_FROM_GATE:
            continue
        if name not in baseline_means:
            print(f"WARN: {name!r} has no baseline yet; not gated")

    if failures:
        print(f"\nREGRESSION: {len(failures)} benchmark(s) exceeded {threshold:.0%}")
    if stale:
        print(
            f"\nSTALE: {len(stale)} benchmark(s) are faster than {stale_floor:.0%} of baseline "
            "(refresh baselines after intentional speedups)"
        )
    if missing:
        print(f"\nMISSING: {len(missing)} gated benchmark(s) absent from current results")
    if failures or stale or missing:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_path", help="pytest-benchmark --benchmark-json output")
    parser.add_argument("baselines_path", help="path to benchmarks/baselines.json")
    parser.add_argument(
        "--threshold",
        type=float,
        default=THRESHOLD,
        help="fail when current mean exceeds baseline by more than this ratio (default: 1.20)",
    )
    parser.add_argument(
        "--stale-floor",
        type=float,
        default=STALE_FLOOR,
        help="fail when current mean is below this fraction of baseline (default: 0.50)",
    )
    args = parser.parse_args(argv)
    try:
        return check_regression(
            args.results_path,
            args.baselines_path,
            threshold=args.threshold,
            stale_floor=args.stale_floor,
        )
    except BenchmarkDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
