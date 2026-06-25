"""Reduce pytest-benchmark JSON into benchmarks/baselines.json."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_benchmark_regression import (
    EXCLUDED_FROM_GATE,
    BenchmarkDataError,
    normalize_benchmark_name,
)

GATED_GROUPS = ("parse", "export", "search", "summary-cache")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("slack must be greater than zero")
    return parsed


def reduce_baselines(
    raw_path: str | Path,
    out_path: str | Path,
    *,
    slack: float = 1.0,
    source: str = "local",
) -> dict[str, object]:
    path = Path(raw_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkDataError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise BenchmarkDataError(f"cannot read {path}: {exc}") from exc

    try:
        entries = raw["benchmarks"]
    except (KeyError, TypeError) as exc:
        raise BenchmarkDataError(f"{path} missing top-level 'benchmarks' array") from exc
    if not isinstance(entries, list):
        raise BenchmarkDataError(f"{path} 'benchmarks' must be an array")

    groups: dict[str, dict[str, float]] = {group: {} for group in GATED_GROUPS}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BenchmarkDataError(f"{path} benchmarks[{index}] must be an object")
        try:
            raw_name = entry["name"]
            mean = float(entry["stats"]["mean"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkDataError(
                f"{path} benchmarks[{index}] missing 'name' or 'stats.mean'"
            ) from exc
        bench_name = normalize_benchmark_name(str(raw_name))
        group = entry.get("group")
        if group not in GATED_GROUPS:
            continue
        groups[group][bench_name] = mean * slack

    excluded = ", ".join(sorted(EXCLUDED_FROM_GATE))
    slack_note = f" Values multiplied by {slack}× slack at generation time." if slack != 1.0 else ""
    machine_info = raw.get("machine_info")
    machine = machine_info.get("system") if isinstance(machine_info, dict) else None
    source_labels = {
        "ubuntu-latest-ci": "ubuntu-latest CI benchmark-results.json",
        "local": "local benchmark-results.json",
    }
    source_label = source_labels.get(source, source)
    output: dict[str, object] = {
        "_note": (
            f"Gated means from {source_label}."
            f"{slack_note} "
            f"Excluded from gate (recorded for reference): {excluded}. "
            "Refresh after intentional speedups via reduce_baselines.py."
        ),
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine": machine,
        "groups": groups,
    }
    out = Path(out_path)
    try:
        out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise BenchmarkDataError(f"cannot write {out}: {exc}") from exc
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_path", help="pytest-benchmark --benchmark-json output")
    parser.add_argument("out_path", help="destination baselines.json path")
    parser.add_argument(
        "--slack",
        type=_positive_float,
        default=1.0,
        help="multiply means by this factor (must be > 0)",
    )
    parser.add_argument(
        "--source",
        default="local",
        help="provenance label for _note (e.g. ubuntu-latest-ci, local)",
    )
    args = parser.parse_args(argv)
    try:
        reduce_baselines(args.raw_path, args.out_path, slack=args.slack, source=args.source)
    except BenchmarkDataError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
