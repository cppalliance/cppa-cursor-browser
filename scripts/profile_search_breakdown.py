"""Break down where search time goes with vs without window."""
from __future__ import annotations

import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.search import (
    _composer_dict_timestamp_ms,
    _composer_row_raw_text,
    _index_bubble_texts_matching_query,
    _timestamp_in_search_window,
    resolve_search_since_ms,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    build_composer_id_to_workspace_id_cached,
    collect_workspace_entries,
    open_global_db,
)
from utils.workspace_path import resolve_workspace_path


def profile_window(since_ms, label: str) -> None:
    query = "export"
    q = query.lower()
    wp = resolve_workspace_path()
    entries = collect_workspace_entries(wp)

    t0 = time.perf_counter()
    build_composer_id_to_workspace_id_cached(wp, entries, [])
    print(f"  composer_map: {time.perf_counter() - t0:.2f}s")

    with open_global_db(wp) as (conn, _):
        if conn is None:
            return
        t0 = time.perf_counter()
        composer_rows = conn.execute(COMPOSER_ROWS_WITH_HEADERS_SQL).fetchall()
        print(f"  load_composer_rows ({len(composer_rows)}): {time.perf_counter() - t0:.2f}s")

        window_ids = None
        if since_ms is not None:
            t0 = time.perf_counter()
            window_ids = set()
            in_window_rows = []
            for row in composer_rows:
                composer_id = row["key"].split(":")[1]
                raw_text = _composer_row_raw_text(row)
                try:
                    cd_probe = json.loads(raw_text)
                except Exception:
                    continue
                if not isinstance(cd_probe, dict):
                    continue
                if not _timestamp_in_search_window(
                    _composer_dict_timestamp_ms(cd_probe), since_ms
                ):
                    continue
                window_ids.add(composer_id)
                in_window_rows.append(row)
            composer_rows = in_window_rows
            print(
                f"  window_filter ({len(window_ids)} composers): "
                f"{time.perf_counter() - t0:.2f}s"
            )

        t0 = time.perf_counter()
        idx = _index_bubble_texts_matching_query(conn, q, composer_ids=window_ids)
        mode = "scoped" if window_ids is not None and len(window_ids) <= 500 else "full_scan"
        print(
            f"  bubble_index ({len(idx)} hits, {mode}): "
            f"{time.perf_counter() - t0:.2f}s"
        )

        t0 = time.perf_counter()
        hits = 0
        for row in composer_rows:
            raw = _composer_row_raw_text(row)
            cid = row["key"].split(":")[1]
            if q in raw.lower() or cid in idx:
                hits += 1
        print(f"  composer_match_loop ({hits} candidates): {time.perf_counter() - t0:.2f}s")


def main() -> None:
    since = resolve_search_since_ms(all_history=False)
    print("=== 30-day window ===")
    profile_window(since, "30d")
    print("=== all history ===")
    profile_window(None, "all")


if __name__ == "__main__":
    main()
