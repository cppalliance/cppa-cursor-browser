from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Regression tests for the PR #30 review finding: "models are defined +
# tested but not wired at any production read site." Each test invokes the
# specific code path Brad cited and asserts the corresponding from_dict
# classmethod was called at least once. If a future refactor unwires the
# model, these tests fail loudly.


WORKSPACE_ID = "ws-wired"
COMPOSER_ID = "cmp-wired"
BUBBLE_ID = "bub-wired"


def _build_workspace_storage(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, WORKSPACE_ID)
    os.makedirs(ws_dir, exist_ok=True)
    project_folder = os.path.join(parent, "wired-project")
    os.makedirs(project_folder, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": project_folder}, f)

    local_db = os.path.join(ws_dir, "state.vscdb")
    with closing(sqlite3.connect(local_db)) as conn:
        conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"allComposers": [{"composerId": COMPOSER_ID}]}),
            ),
        )
        conn.commit()

    global_db = os.path.join(global_root, "state.vscdb")
    with closing(sqlite3.connect(global_db)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"composerData:{COMPOSER_ID}",
                json.dumps({
                    "name": "Wired conversation",
                    "createdAt": 1_715_000_000_000,
                    "lastUpdatedAt": 1_715_000_500_000,
                    "fullConversationHeadersOnly": [{"bubbleId": BUBBLE_ID, "type": 1}],
                    "modelConfig": {"modelName": "gpt-4o"},
                }),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID}",
                json.dumps({"text": "find me sentinel-wired", "type": "user"}),
            ),
        )
        conn.commit()

    return ws_root


class TestBubbleWiredAtReadSite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace_path = _build_workspace_storage(self._tmp.name)
        self._prior_ws = os.environ.get("WORKSPACE_PATH")
        self._prior_cli = os.environ.get("CLI_CHATS_PATH")
        os.environ["WORKSPACE_PATH"] = self.workspace_path
        os.environ["CLI_CHATS_PATH"] = os.path.join(self._tmp.name, "cli-empty")
        os.makedirs(os.environ["CLI_CHATS_PATH"], exist_ok=True)

    def tearDown(self):
        for key, prior in (("WORKSPACE_PATH", self._prior_ws), ("CLI_CHATS_PATH", self._prior_cli)):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        self._tmp.cleanup()

    def test_search_endpoint_finds_bubble_text(self):
        """Search uses a lightweight bubble text extractor (not Bubble.from_dict)."""
        from app import create_app
        import models.conversation as conversation_mod
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(
            conversation_mod.Bubble, "from_dict", wraps=conversation_mod.Bubble.from_dict,
        ) as bubble_spy:
            client = app.test_client()
            response = client.get("/api/search?q=sentinel-wired&all_history=1")
            self.assertEqual(response.status_code, 200)
            results = response.get_json().get("results", [])
            self.assertGreaterEqual(
                len(results), 1,
                msg="/api/search must find bubble text via the lightweight search path",
            )
            self.assertTrue(
                any("sentinel-wired" in (r.get("matchingText") or "").lower() for r in results),
            )
            bubble_spy.assert_not_called()

    def test_workspace_tabs_endpoint_calls_bubble_from_dict(self):
        from app import create_app
        import services.workspace_db as workspace_db_mod
        from models import Bubble
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(
            workspace_db_mod.Bubble, "from_dict", wraps=Bubble.from_dict
        ) as spy:
            client = app.test_client()
            response = client.get(f"/api/workspaces/{WORKSPACE_ID}/tabs")
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="Bubble.from_dict was never called from /api/workspaces/.../tabs — "
                    "model is defined but not wired at the production read site",
            )

    def test_bubble_schema_drift_is_logged_not_swallowed_silently(self):
        # CodeRabbit: SchemaError used to be lumped in with JSONDecodeError /
        # ValueError and skipped silently. Schema drift must now log a
        # `Schema drift in bubble <bid>` line so disappearing bubbles can be
        # traced. The well-formed row still loads alongside.
        from app import create_app
        # Seed a deliberately-malformed bubble row that will trip
        # Bubble.from_dict's "expected non-empty str" gate on the bubble_id by
        # putting a non-dict at the value slot.
        global_db = os.path.join(self._tmp.name, "globalStorage", "state.vscdb")
        with closing(sqlite3.connect(global_db)) as conn:
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (f"bubbleId:{COMPOSER_ID}:bub-bad", json.dumps("not-a-dict")),
            )
            conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with self.assertLogs("services.workspace_db", level="WARNING") as logs:
            client = app.test_client()
            response = client.get(f"/api/workspaces/{WORKSPACE_ID}/tabs")
            self.assertEqual(response.status_code, 200)
        messages = "\n".join(logs.output)
        self.assertIn("Schema drift in bubble", messages,
            msg=f"expected drift log line, got logs:\n{messages!r}")
        self.assertIn("bub-bad", messages,
            msg="drift log must include the offending bubble id")

    def test_workspace_tabs_endpoint_calls_composer_from_dict(self):
        # Brad's most-important finding: list_workspaces() validates each composer
        # with Composer.from_dict, but get_workspace_tabs() used raw json.loads.
        # Schema drift would have been hidden from one of the two primary
        # conversation-browsing paths. This test pins that BOTH paths must now
        # validate via the model.
        from app import create_app
        import services.workspace_tabs as workspace_tabs_mod
        from models import Composer
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(
            workspace_tabs_mod.Composer, "from_dict", wraps=Composer.from_dict
        ) as spy:
            client = app.test_client()
            response = client.get(f"/api/workspaces/{WORKSPACE_ID}/tabs")
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="Composer.from_dict was never called from /api/workspaces/.../tabs — "
                    "the second primary conversation path is bypassing schema validation",
            )


class TestWorkspaceWiredAtReadSite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace_path = _build_workspace_storage(self._tmp.name)
        self._prior_ws = os.environ.get("WORKSPACE_PATH")
        self._prior_cli = os.environ.get("CLI_CHATS_PATH")
        os.environ["WORKSPACE_PATH"] = self.workspace_path
        os.environ["CLI_CHATS_PATH"] = os.path.join(self._tmp.name, "cli-empty")
        os.makedirs(os.environ["CLI_CHATS_PATH"], exist_ok=True)

    def tearDown(self):
        for key, prior in (("WORKSPACE_PATH", self._prior_ws), ("CLI_CHATS_PATH", self._prior_cli)):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        self._tmp.cleanup()

    def test_composers_endpoint_calls_workspace_from_dict(self):
        from app import create_app
        import api.composers as composers_mod
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(composers_mod.Workspace, "from_dict", wraps=composers_mod.Workspace.from_dict) as spy:
            client = app.test_client()
            response = client.get("/api/composers")
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="Workspace.from_dict was never called from /api/composers — "
                    "model is defined but not wired at the production read site",
            )

    def test_workspace_display_name_calls_workspace_from_dict(self):
        from services.workspace_resolver import lookup_workspace_display_name
        import services.workspace_resolver as workspace_resolver_mod
        from models import Workspace
        with patch.object(
            workspace_resolver_mod.Workspace,
            "from_dict",
            wraps=Workspace.from_dict,
        ) as spy:
            name = lookup_workspace_display_name(self.workspace_path, WORKSPACE_ID)
            self.assertIsInstance(name, str)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="Workspace.from_dict was never called from lookup_workspace_display_name",
            )

    def test_list_composers_sort_reads_typed_last_updated_at_not_raw_dict(self):
        # Brad's review: WorkspaceLocalComposer.from_dict was used purely as a
        # gate — the typed object was discarded and the raw dict appended.
        # This test pins that the typed object is now load-bearing: the sort
        # key reads `local.last_updated_at`, not `c.get("lastUpdatedAt")`.
        # If a future refactor reverts to the raw dict, this test fails.
        from app import create_app
        from models import WorkspaceLocalComposer
        import api.composers as composers_mod

        # Seed two composers with raw lastUpdatedAt values in REVERSE order
        # to the values we'll return from a patched WorkspaceLocalComposer.
        # If the sort reads from the raw dict, A comes first; if it reads
        # from the typed model, B comes first.
        ws_db = os.path.join(self.workspace_path, WORKSPACE_ID, "state.vscdb")
        with closing(sqlite3.connect(ws_db)) as conn:
            conn.execute(
                "UPDATE ItemTable SET value = ? WHERE [key] = 'composer.composerData'",
                (json.dumps({"allComposers": [
                    {"composerId": "cmp-A", "lastUpdatedAt": 9000},
                    {"composerId": "cmp-B", "lastUpdatedAt": 1000},
                ]}),),
            )
            conn.commit()

        # Patch from_dict to swap the timestamps in the typed return value.
        # The raw dict still has A=9000, B=1000; the typed values are flipped.
        def swapped_from_dict(raw):  # type: ignore[no-redef]
            cid = raw["composerId"]
            return WorkspaceLocalComposer(
                composer_id=cid,
                last_updated_at=1000 if cid == "cmp-A" else 9000,
                _raw=raw,
            )

        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(composers_mod.WorkspaceLocalComposer, "from_dict", side_effect=swapped_from_dict):
            client = app.test_client()
            response = client.get("/api/composers")
            self.assertEqual(response.status_code, 200)
            ids = [c["composerId"] for c in response.get_json()]
            self.assertEqual(
                ids[:2], ["cmp-B", "cmp-A"],
                msg="Sort key still reads raw dict instead of typed "
                    "WorkspaceLocalComposer.last_updated_at — typed model "
                    "is back to being just a filter.",
            )


class TestGetComposerValidatesSchema(unittest.TestCase):
    # Brad's follow-up finding: list_composers() validates each row via
    # WorkspaceLocalComposer.from_dict and logs drift, but the single-composer
    # fetch get_composer() returned raw dict unchanged. The two paths must
    # agree — a drifted composer that's hidden from the list must NOT be
    # silently served by /api/composers/<id>.

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace_path = _build_workspace_storage(self._tmp.name)
        self._prior_ws = os.environ.get("WORKSPACE_PATH")
        self._prior_cli = os.environ.get("CLI_CHATS_PATH")
        os.environ["WORKSPACE_PATH"] = self.workspace_path
        os.environ["CLI_CHATS_PATH"] = os.path.join(self._tmp.name, "cli-empty")
        os.makedirs(os.environ["CLI_CHATS_PATH"], exist_ok=True)

    def tearDown(self):
        for key, prior in (("WORKSPACE_PATH", self._prior_ws), ("CLI_CHATS_PATH", self._prior_cli)):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        self._tmp.cleanup()

    def test_get_composer_calls_workspace_local_composer_from_dict(self):
        from app import create_app
        import api.composers as composers_mod
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        with patch.object(
            composers_mod.WorkspaceLocalComposer, "from_dict",
            wraps=composers_mod.WorkspaceLocalComposer.from_dict,
        ) as spy:
            client = app.test_client()
            response = client.get(f"/api/composers/{COMPOSER_ID}")
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="WorkspaceLocalComposer.from_dict was never called from "
                    "/api/composers/<id> — the per-workspace path is bypassing "
                    "schema validation that list_composers performs.",
            )

    def test_get_composer_handles_non_dict_envelope_via_fallback(self):
        # CodeRabbit: data.get(...) used to crash with AttributeError when the
        # per-workspace blob isn't a dict. Now must be caught as SchemaError
        # so the function falls through to the global fallback instead of 500.
        from app import create_app
        ws_db = os.path.join(self.workspace_path, WORKSPACE_ID, "state.vscdb")
        with closing(sqlite3.connect(ws_db)) as conn:
            # Replace the dict envelope with a list — would have raised
            # AttributeError on data.get(...) before the guards landed.
            conn.execute(
                "UPDATE ItemTable SET value = ? WHERE [key] = 'composer.composerData'",
                (json.dumps(["not", "a", "dict"]),),
            )
            conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        client = app.test_client()
        response = client.get(f"/api/composers/{COMPOSER_ID}")
        # Global fallback still has the composer seeded, so this must return
        # 200, not 500. The per-workspace drift gets logged and skipped.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("name"), "Wired conversation")

    def test_get_composer_handles_non_list_all_composers_via_fallback(self):
        from app import create_app
        ws_db = os.path.join(self.workspace_path, WORKSPACE_ID, "state.vscdb")
        with closing(sqlite3.connect(ws_db)) as conn:
            conn.execute(
                "UPDATE ItemTable SET value = ? WHERE [key] = 'composer.composerData'",
                (json.dumps({"allComposers": "should-be-list"}),),
            )
            conn.commit()
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        client = app.test_client()
        response = client.get(f"/api/composers/{COMPOSER_ID}")
        # Drift surfaces via global fallback, not as a 500.
        self.assertEqual(response.status_code, 200)

    def test_get_composer_calls_composer_from_dict_on_global_fallback(self):
        # When the per-workspace path misses (composer only in globalStorage),
        # the fallback must validate via Composer.from_dict — not just decode
        # the JSON blob and return it.
        from app import create_app
        import api.composers as composers_mod
        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        # Force the global fallback by zeroing out per-workspace allComposers
        ws_db = os.path.join(self.workspace_path, WORKSPACE_ID, "state.vscdb")
        with closing(sqlite3.connect(ws_db)) as conn:
            conn.execute(
                "UPDATE ItemTable SET value = ? WHERE [key] = 'composer.composerData'",
                (json.dumps({"allComposers": []}),),
            )
            conn.commit()
        with patch.object(
            composers_mod.Composer, "from_dict",
            wraps=composers_mod.Composer.from_dict,
        ) as spy:
            client = app.test_client()
            response = client.get(f"/api/composers/{COMPOSER_ID}")
            self.assertEqual(response.status_code, 200)
            self.assertGreaterEqual(
                spy.call_count, 1,
                msg="Composer.from_dict was never called from /api/composers/<id> "
                    "global-fallback — drifted composers in globalStorage would "
                    "still leak to the client.",
            )


class TestExportEntryWiredAtReadSite(unittest.TestCase):
    def test_load_manifest_entries_calls_export_entry_from_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.jsonl")
            with open(manifest_path, "w", encoding="utf-8") as f:
                # One well-formed entry matching the new writer's schema.
                f.write(json.dumps({
                    "log_id": "log-wired",
                    "title": "Wired chat",
                    "workspace": "wired-project",
                    "path": "out.md",
                    "updated_at": "2026-05-14T00:00:00",
                }) + "\n")

            from scripts import export as export_mod
            with patch.object(
                export_mod.ExportEntry, "from_dict",
                wraps=export_mod.ExportEntry.from_dict,
            ) as spy:
                entries = export_mod.load_manifest_entries(manifest_path)
                self.assertIn("log-wired", entries)
                self.assertGreaterEqual(
                    spy.call_count, 1,
                    msg="ExportEntry.from_dict was never called from "
                        "load_manifest_entries — model is defined but not "
                        "wired at the production read site",
                )

    def test_load_manifest_entries_skips_pre_pr30_entries(self):
        # Backwards-compat: an old manifest from before this PR will lack
        # title/workspace. The reader must skip those silently (and the next
        # export rebuilds the entry under the new schema) rather than 500.
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.jsonl")
            with open(manifest_path, "w", encoding="utf-8") as f:
                # Old-schema entry (no title, no workspace).
                f.write(json.dumps({
                    "log_id": "legacy",
                    "path": "out.md",
                    "updated_at": "2026-05-01T00:00:00",
                }) + "\n")
                # New-schema entry alongside it.
                f.write(json.dumps({
                    "log_id": "modern",
                    "title": "Modern chat",
                    "workspace": "modern-project",
                    "path": "out.md",
                    "updated_at": "2026-05-14T00:00:00",
                }) + "\n")

            from scripts import export as export_mod
            entries = export_mod.load_manifest_entries(manifest_path)
            self.assertNotIn("legacy", entries, msg="pre-PR-30 entries must be skipped")
            self.assertIn("modern", entries, msg="new-schema entries must still load")


if __name__ == "__main__":
    unittest.main()
