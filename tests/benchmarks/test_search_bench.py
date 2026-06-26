"""Benchmark GET /api/search over a 50-composer synthetic corpus."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient

from tests.benchmarks.constants import BENCH_SEARCH_TERM


def _search_url() -> str:
    return f"/api/search?q={BENCH_SEARCH_TERM}&all_history=1"


def _assert_search_response(response: object) -> None:
    assert response.status_code == 200  # type: ignore[attr-defined]
    body = response.get_json()  # type: ignore[attr-defined]
    assert isinstance(body, dict)
    results = body.get("results")
    assert isinstance(results, list) and len(results) > 0


@pytest.mark.benchmark(group="search")
def test_search_full_corpus_live_scan(
    benchmark,
    bench_client_search_corpus: FlaskClient,
) -> None:
    """Live-scan fallback only (``CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX=1``)."""

    def _run() -> object:
        return bench_client_search_corpus.get(_search_url())

    response = benchmark(_run)
    _assert_search_response(response)


@pytest.mark.benchmark(group="search")
def test_search_full_corpus_indexed(
    benchmark,
    bench_client_search_corpus_indexed: FlaskClient,
) -> None:
    """FTS index path (#113) with pre-built ``search_index.sqlite``."""

    def _run() -> object:
        return bench_client_search_corpus_indexed.get(_search_url())

    response = benchmark(_run)
    _assert_search_response(response)
