from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_resolver import (
    determine_project_for_conversation,
    get_project_from_file_path,
)


def _write_workspace_json(parent: str, name: str, folder: str) -> dict:
    ws_dir = os.path.join(parent, name)
    os.makedirs(ws_dir, exist_ok=True)
    wj = os.path.join(ws_dir, "workspace.json")
    with open(wj, "w") as f:
        json.dump({"folder": folder}, f)
    return {"name": name, "workspaceJsonPath": wj}


class TestPrefixCollision(unittest.TestCase):
    def test_sibling_prefix_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "repo", "app")
            app2 = os.path.join(tmp, "repo", "app2")
            os.makedirs(app, exist_ok=True)
            os.makedirs(app2, exist_ok=True)

            entries = [
                _write_workspace_json(tmp, "ws-app", app),
                _write_workspace_json(tmp, "ws-app2", app2),
            ]

            file_in_app2 = os.path.join(app2, "src", "main.py")
            self.assertEqual(
                get_project_from_file_path(file_in_app2, entries),
                "ws-app2",
            )

    def test_file_outside_any_workspace_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "repo", "app")
            os.makedirs(app, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-app", app)]

            unrelated = os.path.join(tmp, "elsewhere", "file.py")
            self.assertIsNone(get_project_from_file_path(unrelated, entries))

    def test_file_inside_workspace_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "repo", "app")
            os.makedirs(app, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-app", app)]

            inside = os.path.join(app, "src", "main.py")
            self.assertEqual(get_project_from_file_path(inside, entries), "ws-app")


class TestDetermineProjectUsesPathHelper(unittest.TestCase):
    """Regression: determine_project_for_conversation must resolve get_project_from_file_path."""

    def test_newly_created_files_triggers_path_helper(self):
        from models.conversation import Composer

        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "repo", "app")
            os.makedirs(app, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-app", app)]
            inside = os.path.join(app, "src", "main.py")
            composer = Composer.from_dict(
                {
                    "name": "Path test",
                    "createdAt": 1_739_200_000_000,
                    "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}],
                    "newlyCreatedFiles": [{"uri": {"path": inside}}],
                },
                composer_id="cmp-path",
            )
            ws_id = determine_project_for_conversation(
                composer,
                "cmp-path",
                {},
                {},
                {},
                entries,
                {},
                None,
                None,
            )
            self.assertEqual(ws_id, "ws-app")


if __name__ == "__main__":
    unittest.main()
