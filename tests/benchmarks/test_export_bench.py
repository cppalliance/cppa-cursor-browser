"""Benchmark POST /api/export (ZIP) over synthetic workspace + global DB."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient


@pytest.mark.benchmark(group="export")
@pytest.mark.parametrize(
    "bench_storage",
    [10, 50],
    indirect=True,
    ids=["composers-10", "composers-50"],
)
def test_post_export_zip(
    benchmark,
    bench_client: FlaskClient,
) -> None:
    def _run() -> object:
        return bench_client.post(
            "/api/export",
            json={},
            content_type="application/json",
        )

    response = benchmark(_run)
    assert response.status_code == 200
    assert response.content_type.startswith("application/zip")
    assert int(response.headers.get("X-Export-Count", "0")) >= 1
