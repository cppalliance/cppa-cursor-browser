"""One-off search profiler — run: python scripts/profile_search.py [query]"""
from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import ParseWarningCollector
from services.search import search_global_storage
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    build_composer_id_to_workspace_id_cached,
    collect_workspace_entries,
    open_global_db,
)
from services.search import _index_bubble_texts_matching_query, _sql_like_substring
from utils.workspace_path import resolve_workspace_path


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "export"
    q = query.lower()
    wp = resolve_workspace_path()
    entries = collect_workspace_entries(wp)

    t0 = time.perf_counter()
    build_composer_id_to_workspace_id_cached(wp, entries, [])
    print(f"composer_map: {time.perf_counter() - t0:.2f}s")

    with open_global_db(wp) as (conn, _):
        if conn is None:
            print("no global db")
            return
        t0 = time.perf_counter()
        rows = conn.execute(COMPOSER_ROWS_WITH_HEADERS_SQL).fetchall()
        print(f"composer_rows {len(rows)}: {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        bubble_count = conn.execute(
            "SELECT count(*) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchone()[0]
        print(f"bubble_count {bubble_count}: {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        idx = _index_bubble_texts_matching_query(conn, q)
        print(f"bubble_index {len(idx)} composers: {time.perf_counter() - t0:.2f}s")

        pattern = _sql_like_substring(q)
        t0 = time.perf_counter()
        raw_hits = sum(
            1 for row in rows
            if q in (row["value"] or "").lower()
        )
        print(f"composer_raw_hits {raw_hits}: {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    results = search_global_storage(wp, query, q, [], ParseWarningCollector())
    print(f"search_global_storage {len(results)} hits: {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
