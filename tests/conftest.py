from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import create_app


HAPPY_COMPOSER_ID = "cmp-happy"
HAPPY_BUBBLE_ID = "bub-happy"
HAPPY_WORKSPACE_ID = "ws-happy"


def _make_global_state_db(path: str) -> None:
    """globalStorage/state.vscdb with one composerData + one bubbleId row."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{HAPPY_COMPOSER_ID}",
            json.dumps({
                "name": "Happy conversation",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": 1_715_000_500_000,
                "fullConversationHeadersOnly": [
                    {"bubbleId": HAPPY_BUBBLE_ID, "type": 1},
                ],
                "modelConfig": {"modelName": "gpt-4o"},
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{HAPPY_COMPOSER_ID}:{HAPPY_BUBBLE_ID}",
            json.dumps({
                "text": "find me by search term sentinel-grep",
                "type": "user",
                "createdAt": 1_715_000_400_000,
            }),
        ),
    )
    conn.commit()
    conn.close()


def _make_workspace(parent: str, workspace_id: str, project_folder: str) -> None:
    """One per-workspace directory: workspace.json + minimal state.vscdb."""
    ws_dir = os.path.join(parent, workspace_id)
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": project_folder}, f)
    db = os.path.join(ws_dir, "state.vscdb")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
        (
            "composer.composerData",
            json.dumps({"allComposers": [{"composerId": HAPPY_COMPOSER_ID}]}),
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def workspace_storage() -> Generator[str, None, None]:
    """Build a temp workspaceStorage layout and yield the workspace path.

    Layout:
        <tmp>/workspaceStorage/<HAPPY_WORKSPACE_ID>/workspace.json
        <tmp>/workspaceStorage/<HAPPY_WORKSPACE_ID>/state.vscdb
        <tmp>/globalStorage/state.vscdb
        <tmp>/cli_chats/                    (empty — keeps live ~/.cursor leaking out)

    Sets ``WORKSPACE_PATH`` and ``CLI_CHATS_PATH`` env vars for the duration of
    the test and restores them on cleanup.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = os.path.join(tmp, "workspaceStorage")
        global_root = os.path.join(tmp, "globalStorage")
        cli_root = os.path.join(tmp, "cli_chats")
        os.makedirs(ws_root, exist_ok=True)
        os.makedirs(global_root, exist_ok=True)
        os.makedirs(cli_root, exist_ok=True)

        project_folder = os.path.join(tmp, "happy-project")
        os.makedirs(project_folder, exist_ok=True)

        _make_workspace(ws_root, HAPPY_WORKSPACE_ID, project_folder)
        _make_global_state_db(os.path.join(global_root, "state.vscdb"))

        prior_ws = os.environ.get("WORKSPACE_PATH")
        prior_cli = os.environ.get("CLI_CHATS_PATH")
        os.environ["WORKSPACE_PATH"] = ws_root
        os.environ["CLI_CHATS_PATH"] = cli_root
        try:
            yield ws_root
        finally:
            if prior_ws is None:
                os.environ.pop("WORKSPACE_PATH", None)
            else:
                os.environ["WORKSPACE_PATH"] = prior_ws
            if prior_cli is None:
                os.environ.pop("CLI_CHATS_PATH", None)
            else:
                os.environ["CLI_CHATS_PATH"] = prior_cli


@pytest.fixture
def client(workspace_storage: str):
    """Flask test client bound to the temp workspace_storage fixture."""
    app = create_app()
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = []
    return app.test_client()


@pytest.fixture
def empty_workspace_client() -> Generator:
    """Flask test client bound to a workspaceStorage with no workspaces.

    Useful for 404 tests where the workspace id is unknown.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = os.path.join(tmp, "workspaceStorage")
        cli_root = os.path.join(tmp, "cli_chats")
        os.makedirs(ws_root, exist_ok=True)
        os.makedirs(cli_root, exist_ok=True)

        prior_ws = os.environ.get("WORKSPACE_PATH")
        prior_cli = os.environ.get("CLI_CHATS_PATH")
        os.environ["WORKSPACE_PATH"] = ws_root
        os.environ["CLI_CHATS_PATH"] = cli_root
        try:
            app = create_app()
            app.config["TESTING"] = True
            app.config["EXCLUSION_RULES"] = []
            yield app.test_client()
        finally:
            if prior_ws is None:
                os.environ.pop("WORKSPACE_PATH", None)
            else:
                os.environ["WORKSPACE_PATH"] = prior_ws
            if prior_cli is None:
                os.environ.pop("CLI_CHATS_PATH", None)
            else:
                os.environ["CLI_CHATS_PATH"] = prior_cli
