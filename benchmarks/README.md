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
| export | `POST /api/export` (ZIP) over 10 / 50 composer corpora |
| search | `GET /api/search` over a 50-composer synthetic corpus |
| summary-cache | cache lookup (hit/miss), fingerprint (10/50/200), round-trip, tab-summary lookup |

Synthetic corpora are built in `tests/benchmarks/conftest.py` — no real Cursor storage dependency.

## CI gate

The `benchmarks` job on **ubuntu-latest** runs the full `tests/benchmarks/` suite (`--benchmark-json=benchmark-results.json`), then `scripts/check_benchmark_regression.py benchmark-results.json benchmarks/baselines.json`.

- **Fail** when a gated mean exceeds its baseline by **>20%**
- **Fail** when a gated mean is **<50%** of baseline (stale — refresh after intentional speedups)
- **Fail** when a gated baseline name has no current result
- **Warn** for benchmarks without a baseline entry
- **Skip gate** for `EXCLUDED_FROM_GATE` names (smallest parse corpus, full-corpus search — sub-ms CI noise)

Pinned runner: `ubuntu-latest`, `--benchmark-min-rounds=5`.

## Refresh baselines

After intentional performance work, capture on **ubuntu-latest** (same OS as the gated CI job). Download `benchmark-results.json` from a CI artifact when possible:

```bash
python scripts/reduce_baselines.py benchmark-results.json benchmarks/baselines.json --slack 1.5
```

For a quick local snapshot only (may not match CI timings):

```bash
make seed-baselines-local
```

`make update-baselines` is a deprecated alias for `seed-baselines-local`. Do not commit baselines from macOS/Windows unless you accept cross-OS gate skew.

## Makefile targets

| Target | Purpose |
|--------|---------|
| `make check-benchmarks` | Run suite + regression gate locally |
| `make seed-baselines-local` | Capture local timings into `benchmarks/baselines.json` (with slack) |
| `make clean-benchmark-artifacts` | Remove `benchmark-results.json` and `benchmarks/_raw.json` |
