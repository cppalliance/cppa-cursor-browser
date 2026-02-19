"""
Tests for fallback workspace-name inference from messageRequestContext.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from api.workspaces import _infer_workspace_name_from_context


class TestWorkspaceNameInference(unittest.TestCase):
    def test_infers_name_from_project_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_path = os.path.join(tmp, "workspaceStorage")
            global_storage = os.path.join(tmp, "globalStorage")
            ws_id = "deadbeef1234"
            ws_dir = os.path.join(workspace_path, ws_id)
            os.makedirs(ws_dir, exist_ok=True)
            os.makedirs(global_storage, exist_ok=True)

            # Local workspace DB with composer IDs
            local_db = os.path.join(ws_dir, "state.vscdb")
            conn = sqlite3.connect(local_db)
            conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
                (
                    "composer.composerData",
                    json.dumps(
                        {
                            "allComposers": [
                                {"composerId": "cmp-1"},
                                {"composerId": "cmp-2"},
                            ]
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()

            # Global DB with projectLayouts for those composers
            global_db = os.path.join(global_storage, "state.vscdb")
            gconn = sqlite3.connect(global_db)
            gconn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
            gconn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    "messageRequestContext:cmp-1:ctx-a",
                    json.dumps(
                        {
                            "projectLayouts": [
                                json.dumps({"rootPath": "file:///d%3A/_Cpp_Digest/boostbacklog"}),
                            ]
                        }
                    ),
                ),
            )
            gconn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    "messageRequestContext:cmp-2:ctx-b",
                    json.dumps(
                        {
                            "projectLayouts": [
                                json.dumps({"rootPath": "file:///d%3A/_Cpp_Digest/boostbacklog"}),
                                json.dumps({"rootPath": "file:///d%3A/_Cpp_Digest/cppdigest-github-app"}),
                            ]
                        }
                    ),
                ),
            )
            gconn.commit()
            gconn.close()

            self.assertEqual(
                _infer_workspace_name_from_context(workspace_path, ws_id),
                "boostbacklog",
            )


if __name__ == "__main__":
    unittest.main()
