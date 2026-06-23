"""Profile search with 30-day window vs all history."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import ParseWarningCollector
from services.search import (
    _composer_dict_timestamp_ms,
    _timestamp_in_search_window,
    resolve_search_since_ms,
    search_cli_sessions,
    search_global_storage,
    search_legacy_workspaces,
)
from services.workspace_db import COMPOSER_ROWS_WITH_HEADERS_SQL, open_global_db
from utils.workspace_path import get_cli_chats_path, resolve_workspace_path


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "export"
    q = query.lower()
    wp = resolve_workspace_path()
    since = resolve_search_since_ms(all_history=False)
    assert since is not None
    print(
        f"since_ms={since} "
        f"({datetime.fromtimestamp(since / 1000, tz=timezone.utc).isoformat()})"
    )

    with open_global_db(wp) as (conn, _):
        if conn is None:
            print("no global db")
            return
        rows = conn.execute(COMPOSER_ROWS_WITH_HEADERS_SQL).fetchall()
        total = len(rows)
        in_window = 0
        unknown_ts = 0
        for row in rows:
            try:
                cd = json.loads(row["value"])
            except Exception:
                continue
            ts = _composer_dict_timestamp_ms(cd)
            if ts <= 0:
                unknown_ts += 1
            if _timestamp_in_search_window(ts, since):
                in_window += 1
        bubble_count = conn.execute(
            "SELECT count(*) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchone()[0]

    print(f"composers total={total} in_30d_window={in_window} unknown_ts={unknown_ts}")
    print(f"bubble_rows={bubble_count}")
    print(f"below_bubble_threshold={in_window <= 500}")

    pw = ParseWarningCollector()
    for label, since_ms in [("30d", since), ("all", None)]:
        t0 = time.perf_counter()
        r1 = search_global_storage(wp, query, q, [], pw, since_ms=since_ms)
        t1 = time.perf_counter() - t0
        t0 = time.perf_counter()
        r2 = search_legacy_workspaces(wp, query, q, "all", [], since_ms=since_ms)
        t2 = time.perf_counter() - t0
        t0 = time.perf_counter()
        r3 = search_cli_sessions(get_cli_chats_path(), query, q, [], pw, since_ms=since_ms)
        t3 = time.perf_counter() - t0
        total_hits = len(r1) + len(r2) + len(r3)
        total_s = t1 + t2 + t3
        print(
            f"{label}: global={len(r1)} ({t1:.2f}s) "
            f"legacy={len(r2)} ({t2:.2f}s) cli={len(r3)} ({t3:.2f}s) "
            f"total={total_hits} ({total_s:.2f}s)"
        )


if __name__ == "__main__":
    main()
