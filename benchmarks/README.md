# Performance benchmarks

Test files live under `tests/benchmarks/`; this directory holds documentation and `baselines.json` for the CI regression gate.

Repeatable local measurements for workspace listing, export, search, and summary-cache hot paths.

## Run locally

```bash
pip install -r requirements-lock.txt
pip install 'pytest>=8,<9' 'pytest-benchmark==4.0.0'
pytest tests/benchmarks/ --benchmark-only -o addopts= -v
```

## Scenarios

| Group | What |
|-------|------|
| parse | `list_workspace_projects(..., nocache=True)` over 10 / 50 / 200 synthetic composers |
| export | `POST /api/export` (ZIP) over 10 / 50 composer corpora (capped at 50 for CI runtime; parse goes to 200) |
| search | `GET /api/search` over a 50-composer corpus — **live-scan** (`test_search_full_corpus_live_scan`, `NO_SEARCH_INDEX=1`) and **FTS index** (`test_search_full_corpus_indexed`, pre-built index) |
| summary-cache | projects lookup (hit/miss), composer-map lookup (hit/miss), fingerprint (10/50/200), round-trip, tab-summary lookup |

Synthetic corpora are built in `tests/benchmarks/conftest.py` — no real Cursor storage dependency.

### Adding a benchmark group

Every `@pytest.mark.benchmark(group="...")` name must appear in `GATED_GROUPS` inside `scripts/reduce_baselines.py`. Otherwise `reduce_baselines.py` fails at refresh time with an unknown-group error. Update both the test marker and `GATED_GROUPS` when introducing a new group.

## CI gate

The `benchmarks` job on **ubuntu-latest** runs the full `tests/benchmarks/` suite (`--benchmark-json=benchmark-results.json`), then `scripts/check_benchmark_regression.py benchmark-results.json benchmarks/baselines.json`.

- **Fail** when a gated mean exceeds its baseline by **>20%**
- **Fail** when a gated mean is **<50%** of baseline (stale — refresh after intentional speedups)
- **Fail** when a gated baseline name has no current result
- **Warn** for benchmarks without a baseline entry
- All benchmarks listed in `baselines.json` are gated unless named in `EXCLUDED_FROM_GATE` in `scripts/check_benchmark_regression.py`

Pinned runner: `ubuntu-latest`, `--benchmark-min-rounds=5`.

Sub-millisecond cache lookups (`test_summary_cache_lookup`, `test_composer_map_cache_lookup`, `test_tab_summary_cache_lookup`) are already listed in `EXCLUDED_FROM_GATE` because shared runners show 2–4x spread. For remaining gated benches that turn flaky, raise `--slack` at baseline refresh time or add a targeted `EXCLUDED_FROM_GATE` entry.

`test_summary_cache_round_trip` is intentionally excluded from the gate: it calls `set_cached_projects` (file write) + `get_cached_projects` (file read) each round, so OS page-cache state on shared runners causes 3–5x variation between consecutive CI runs. The baseline entry is kept for observation only.

### Export ZIP benchmarks

Both `test_post_export_zip[composers-10]` and `test_post_export_zip[composers-50]` are excluded from the regression gate. ZIP export timing on shared ubuntu-latest runners swings with page-cache and first-write effects (e.g. composers-10 ~4x between runs; composers-50 exceeded 1.2x vs a Jul-15 baseline at 0.045s vs 0.028s). That is environmental variance, not an application slowdown. Baseline entries in `baselines.json` are kept for observation only; parse, search, and fingerprint benches remain gated.

### Out-of-scope CI fixes in feature PRs

Sometimes **Performance benchmarks (gated)** fails on `master` even though the change does not touch export, parse, or search hot paths. When the failure is pre-existing runner variance (export ZIP benches above), the correct fix is a targeted `EXCLUDED_FROM_GATE` entry — **not** a `baselines.json` refresh unless you are doing intentional performance work on that bench.

- **In scope for the feature PR:** unblocking CI so `mypy` + unit tests + the benchmark job all pass.
- **Out of scope for the feature PR:** claiming a performance win/loss, changing export behavior, or refreshing baselines for unrelated benches.
- **Reviewers:** treat `EXCLUDED_FROM_GATE` / `benchmarks/README.md` edits in a feature PR as CI maintenance when the comment block documents environmental spread, not application regression.

## Refresh baselines

After intentional performance work, capture on **ubuntu-latest** (same OS as the gated CI job). Download `benchmark-results.json` from a CI artifact when possible:

```bash
python scripts/reduce_baselines.py benchmark-results.json benchmarks/baselines.json --slack 1.5 --source ubuntu-latest-ci
```

For a quick local snapshot only (may not match CI timings):

```bash
make seed-baselines-local
# writes benchmarks/_raw.json only; does not overwrite benchmarks/baselines.json
make seed-baselines-local FORCE=1   # also runs reduce_baselines into benchmarks/baselines.json
```

`make update-baselines` is a deprecated alias for `seed-baselines-local`. Do not commit baselines from macOS/Windows unless you accept cross-OS gate skew.

## Makefile targets

| Target | Purpose |
|--------|---------|
| `make check-benchmarks` | Run suite + regression gate locally |
| `make seed-baselines-local` | Capture local timings to `benchmarks/_raw.json` (use `FORCE=1` to update `baselines.json`) |
| `make clean-benchmark-artifacts` | Remove `benchmark-results.json` and `benchmarks/_raw.json` |
