"""Benchmark GET /api/search over a 50-composer synthetic corpus."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient

from tests.benchmarks.conftest import BENCH_SEARCH_TERM


@pytest.mark.benchmark(group="search")
def test_search_full_corpus(
    benchmark,
    bench_client_search_corpus: FlaskClient,
) -> None:
    def _run() -> object:
        return bench_client_search_corpus.get(
            f"/api/search?q={BENCH_SEARCH_TERM}&all_history=1",
        )

    response = benchmark(_run)
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    results = body.get("results")
    assert isinstance(results, list) and len(results) > 0
