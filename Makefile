.PHONY: seed-baselines-local update-baselines check-benchmarks clean-benchmark-artifacts

# WARNING: captures timings on THIS machine. Production baselines must match ubuntu-latest CI.
# Prefer downloading benchmark-results.json from a CI artifact, then:
#   python scripts/reduce_baselines.py benchmark-results.json benchmarks/baselines.json --slack 1.5
seed-baselines-local:
	@echo "WARNING: seed-baselines-local uses this host's timings; CI gates on ubuntu-latest." >&2
	python -m pytest tests/benchmarks/ --benchmark-only --benchmark-json=benchmarks/_raw.json -o addopts=
	python scripts/reduce_baselines.py benchmarks/_raw.json benchmarks/baselines.json --slack 1.5 --source local

# Deprecated alias — kept for muscle memory; see seed-baselines-local warning above.
update-baselines: seed-baselines-local

check-benchmarks:
	python -m pytest tests/benchmarks/ --benchmark-only --benchmark-json=benchmark-results.json -o addopts=
	python scripts/check_benchmark_regression.py benchmark-results.json benchmarks/baselines.json

clean-benchmark-artifacts:
	python -c "import pathlib; [p.unlink(missing_ok=True) for p in (pathlib.Path('benchmarks/_raw.json'), pathlib.Path('benchmark-results.json'))]"
