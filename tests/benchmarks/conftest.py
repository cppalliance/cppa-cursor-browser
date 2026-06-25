"""Shared synthetic fixtures for pytest-benchmark hot paths."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from app import create_app
from services import summary_cache
from services.summary_cache import fingerprint_workspace_storage
from tests.benchmarks.constants import BENCH_SEARCH_TERM


def make_workspace_entries(workspace_root: Path, count: int) -> list[dict[str, Any]]:
    """Build *count* synthetic workspace entries with on-disk state files."""
    entries: list[dict[str, Any]] = []
    for i in range(count):
        name = f"ws_{i:04d}"
        entry_dir = workspace_root / name
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "state.vscdb").write_bytes(b"bench")
        workspace_json = entry_dir / "workspace.json"
        workspace_json.write_text('{"folder": "/bench"}', encoding="utf-8")
        entries.append(
            {
                "name": name,
                "workspaceJsonPath": str(workspace_json),
            }
        )
    return entries


def _composer_ids(count: int) -> list[tuple[str, str, str]]:
    return [(f"ws_{i:04d}", f"cmp_{i:04d}", f"bub_{i:04d}") for i in range(count)]


def build_bench_storage(root: Path, composer_count: int) -> dict[str, str]:
    """Create workspaceStorage, globalStorage, and cli_chats trees for *composer_count* composers."""
    ws_root = root / "workspaceStorage"
    global_root = root / "globalStorage"
    cli_root = root / "cli_chats"
    projects_root = root / "projects"
    ws_root.mkdir(parents=True)
    global_root.mkdir(parents=True)
    cli_root.mkdir(parents=True)
    projects_root.mkdir(parents=True)

    global_db_path = global_root / "state.vscdb"
    with contextlib.closing(sqlite3.connect(global_db_path)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        base_ts = 1_715_000_000_000
        for i, (workspace_id, composer_id, bubble_id) in enumerate(_composer_ids(composer_count)):
            project_folder = projects_root / f"proj_{i:04d}"
            project_folder.mkdir(parents=True, exist_ok=True)

            ws_dir = ws_root / workspace_id
            ws_dir.mkdir(parents=True, exist_ok=True)
            (ws_dir / "workspace.json").write_text(
                json.dumps({"folder": str(project_folder)}),
                encoding="utf-8",
            )
            with contextlib.closing(sqlite3.connect(ws_dir / "state.vscdb")) as ws_conn:
                ws_conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
                ws_conn.execute(
                    "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
                    (
                        "composer.composerData",
                        json.dumps({"allComposers": [{"composerId": composer_id}]}),
                    ),
                )
                ws_conn.commit()

            created_at = base_ts + i * 1_000
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    f"composerData:{composer_id}",
                    json.dumps(
                        {
                            "name": f"Bench chat {i:04d}",
                            "createdAt": created_at,
                            "lastUpdatedAt": created_at + 500,
                            "fullConversationHeadersOnly": [
                                {"bubbleId": bubble_id, "type": 1},
                            ],
                            "modelConfig": {"modelName": "gpt-4o"},
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    f"bubbleId:{composer_id}:{bubble_id}",
                    json.dumps(
                        {
                            "text": f"find {BENCH_SEARCH_TERM} in composer {i:04d}",
                            "type": "user",
                            "createdAt": created_at + 400,
                        }
                    ),
                ),
            )
        conn.commit()

    return {
        "workspace_path": str(ws_root),
        "cli_chats_path": str(cli_root),
        "storage_root": str(root),
    }


def _make_bench_flask_client(
    storage: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_subdir: str = ".cursor-chat-browser",
) -> FlaskClient:
    """Flask test client with env + export state patched for synthetic storage."""
    monkeypatch.setenv("WORKSPACE_PATH", storage["workspace_path"])
    monkeypatch.setenv("CLI_CHATS_PATH", storage["cli_chats_path"])
    monkeypatch.setenv("CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX", "1")
    state_dir = tmp_path / state_subdir
    state_dir.mkdir()
    monkeypatch.setattr("api.export_api._get_state_dir", lambda: str(state_dir))
    app = create_app()
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = []
    return app.test_client()


@pytest.fixture
def summary_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect summary-cache files to an isolated temp directory."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(summary_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(summary_cache, "PROJECTS_CACHE_FILE", cache_dir / "projects.json")
    monkeypatch.setattr(
        summary_cache,
        "COMPOSER_MAP_CACHE_FILE",
        cache_dir / "composer-id-to-ws.json",
    )
    return cache_dir


@pytest.fixture
def sample_projects() -> list[dict[str, Any]]:
    return [
        {
            "id": "ws_0000",
            "name": "Bench Project",
            "conversationCount": 3,
            "lastModified": "2026-06-24T00:00:00Z",
        }
    ]


@pytest.fixture
def synthetic_workspace(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[str, list[dict[str, Any]]]:
    """Workspace path + entries. Parametrize via indirect ``workspace_entry_count``."""
    count = getattr(request, "param", 10)
    workspace_root = tmp_path / "workspaceStorage"
    workspace_root.mkdir()
    entries = make_workspace_entries(workspace_root, count)
    return str(workspace_root), entries


@pytest.fixture
def workspace_fingerprint(synthetic_workspace: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    workspace_path, entries = synthetic_workspace
    return fingerprint_workspace_storage(
        workspace_path,
        entries,
        global_db_path=None,
        rules=[],
    )


@pytest.fixture
def stale_fingerprint(workspace_fingerprint: dict[str, Any]) -> dict[str, Any]:
    """Return a fingerprint guaranteed to differ from the stored one."""
    return {**workspace_fingerprint, "rules_digest": "deadbeefdeadbeef"}


@pytest.fixture
def bench_storage(tmp_path: Path, request: pytest.FixtureRequest) -> dict[str, str]:
    """On-disk Cursor layout with N composers (indirect ``composer_count`` param)."""
    count = getattr(request, "param", 10)
    return build_bench_storage(tmp_path / "storage", count)


@pytest.fixture
def bench_env(
    bench_storage: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Set WORKSPACE_PATH / CLI_CHATS_PATH for the synthetic storage tree."""
    monkeypatch.setenv("WORKSPACE_PATH", bench_storage["workspace_path"])
    monkeypatch.setenv("CLI_CHATS_PATH", bench_storage["cli_chats_path"])
    monkeypatch.setenv("CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX", "1")
    return bench_storage


@pytest.fixture
def bench_client(bench_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    """Flask test client bound to synthetic bench storage."""
    return _make_bench_flask_client(bench_env, tmp_path, monkeypatch)


@pytest.fixture
def bench_client_search_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FlaskClient:
    """Flask client over a fixed 50-composer corpus for search benchmarks."""
    storage = build_bench_storage(tmp_path / "search_storage", 50)
    return _make_bench_flask_client(
        storage,
        tmp_path,
        monkeypatch,
        state_subdir=".cursor-chat-browser-search",
    )
