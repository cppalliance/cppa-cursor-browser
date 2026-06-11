"""
Tests for determine_project_for_conversation multi-stage resolution (issue #87).

Run:
  python -m unittest tests.test_workspace_assignment_fallback -v
  python -m pytest tests/test_workspace_assignment_fallback.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hypothesis import given, settings
from hypothesis import strategies as st

from models import Bubble, SchemaError
from services.workspace_resolver import determine_project_for_conversation
from utils.path_helpers import normalize_file_path


def _bubble_map_from_raw(raw: dict) -> dict[str, Bubble]:
    out: dict[str, Bubble] = {}
    for bid, val in raw.items():
        if not isinstance(val, dict):
            continue
        try:
            out[bid] = Bubble.from_dict(val, bubble_id=bid)
        except SchemaError:
            continue
    return out


def _write_workspace_json(parent: str, name: str, folder: str) -> dict:
    ws_dir = os.path.join(parent, name)
    os.makedirs(ws_dir, exist_ok=True)
    wj = os.path.join(ws_dir, "workspace.json")
    with open(wj, "w", encoding="utf-8") as f:
        json.dump({"folder": folder}, f)
    return {"name": name, "workspaceJsonPath": wj}


def _resolve(
    composer_data: dict,
    *,
    composer_id: str = "cmp-test",
    project_layouts_map: dict | None = None,
    project_name_to_workspace_id: dict | None = None,
    workspace_path_to_id: dict | None = None,
    workspace_entries: list | None = None,
    bubble_map: dict | None = None,
    composer_id_to_workspace_id: dict | None = None,
    invalid_workspace_ids: set[str] | None = None,
) -> str | None:
    return determine_project_for_conversation(
        composer_data=composer_data,
        composer_id=composer_id,
        project_layouts_map=project_layouts_map or {},
        project_name_to_workspace_id=project_name_to_workspace_id or {},
        workspace_path_to_id=workspace_path_to_id or {},
        workspace_entries=workspace_entries or [],
        bubble_map=bubble_map or {},
        composer_id_to_workspace_id=composer_id_to_workspace_id,
        invalid_workspace_ids=invalid_workspace_ids,
    )


_EMPTY_COMPOSER: dict[str, object] = {
    "fullConversationHeadersOnly": [],
    "newlyCreatedFiles": [],
    "codeBlockData": {},
}


class TestDetermineProjectPrimaryMapping(unittest.TestCase):
    def test_definitive_mapping_returns_workspace_id(self) -> None:
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-1",
            composer_id_to_workspace_id={"cmp-1": "ws-primary"},
        )
        self.assertEqual(assigned, "ws-primary")

    def test_definitive_mapping_wins_over_conflicting_layouts(self) -> None:
        root = normalize_file_path("/tmp/other-project")
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-1",
            project_layouts_map={"cmp-1": [root]},
            workspace_path_to_id={root: "ws-layout"},
            composer_id_to_workspace_id={"cmp-1": "ws-primary"},
        )
        self.assertEqual(assigned, "ws-primary")

    def test_ignores_invalid_composer_to_workspace_mapping(self) -> None:
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-123",
            project_layouts_map={
                "cmp-123": [normalize_file_path("/d%3A/_Cpp_Digest/boostbacklog")]
            },
            project_name_to_workspace_id={"boostbacklog": "good-ws"},
            workspace_path_to_id={
                normalize_file_path("d:\\_cpp_digest\\boostbacklog"): "good-ws"
            },
            composer_id_to_workspace_id={"cmp-123": "broken-ws"},
            invalid_workspace_ids={"broken-ws"},
        )
        self.assertEqual(assigned, "good-ws")

    def test_invalid_definitive_mapping_with_no_fallback_returns_none(self) -> None:
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-1",
            composer_id_to_workspace_id={"cmp-1": "broken-ws"},
            invalid_workspace_ids={"broken-ws"},
        )
        self.assertIsNone(assigned)


class TestDetermineProjectStageOrdering(unittest.TestCase):
    """Non-definitive stages are tried in fixed order; earlier stage wins on conflict."""

    def test_newly_created_files_wins_over_conflicting_code_block_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_a_root = os.path.join(tmp, "project-a")
            ws_b_root = os.path.join(tmp, "project-b")
            os.makedirs(ws_a_root, exist_ok=True)
            os.makedirs(ws_b_root, exist_ok=True)
            entries = [
                _write_workspace_json(tmp, "ws-a", ws_a_root),
                _write_workspace_json(tmp, "ws-b", ws_b_root),
            ]
            file_a = os.path.join(ws_a_root, "src", "a.py")
            file_b = os.path.join(ws_b_root, "src", "b.py")
            os.makedirs(os.path.dirname(file_a), exist_ok=True)
            os.makedirs(os.path.dirname(file_b), exist_ok=True)

            assigned = _resolve(
                {
                    "newlyCreatedFiles": [{"uri": {"path": file_a}}],
                    "codeBlockData": {f"file://{file_b}": {"language": "python"}},
                },
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-a")


class TestDetermineProjectLayoutsStage(unittest.TestCase):
    def test_project_layouts_resolves_via_workspace_path_to_id(self) -> None:
        root = normalize_file_path("/work/repos/myapp")
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-layout",
            project_layouts_map={"cmp-layout": [root]},
            workspace_path_to_id={root: "ws-from-path"},
        )
        self.assertEqual(assigned, "ws-from-path")

    def test_project_layouts_resolves_via_project_name_fallback(self) -> None:
        root = normalize_file_path("d:/work/repos/myapp")
        assigned = _resolve(
            _EMPTY_COMPOSER,
            composer_id="cmp-layout",
            project_layouts_map={"cmp-layout": [root]},
            project_name_to_workspace_id={"myapp": "ws-from-name"},
        )
        self.assertEqual(assigned, "ws-from-name")

    def test_newly_created_files_resolves_without_project_layouts_entry(self) -> None:
        """No projectLayouts row: newlyCreatedFiles still resolves via workspace_entries."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "proj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-entries", ws_root)]
            inside = os.path.join(ws_root, "src", "main.py")
            os.makedirs(os.path.dirname(inside), exist_ok=True)

            assigned = _resolve(
                {"newlyCreatedFiles": [{"uri": {"path": inside}}]},
                composer_id="cmp-unknown",
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-entries")

    def test_unresolvable_project_layouts_falls_through_to_file_paths(self) -> None:
        """projectLayouts roots that do not map still allow later file-path stages."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "fallback-proj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-fallback", ws_root)]
            inside = os.path.join(ws_root, "lib", "mod.py")
            os.makedirs(os.path.dirname(inside), exist_ok=True)
            unmapped_root = normalize_file_path("/no/such/workspace/root")

            assigned = _resolve(
                {"newlyCreatedFiles": [{"uri": {"path": inside}}]},
                composer_id="cmp-unmapped-layout",
                project_layouts_map={"cmp-unmapped-layout": [unmapped_root]},
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-fallback")


class TestDetermineProjectFilePathStages(unittest.TestCase):
    def test_newly_created_files_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "myproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-newly", ws_root)]
            file_path = os.path.join(ws_root, "lib", "foo.py")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            assigned = _resolve(
                {"newlyCreatedFiles": [{"uri": {"path": file_path}}]},
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-newly")

    def test_code_block_data_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "codeproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-cbd", ws_root)]
            file_path = os.path.join(ws_root, "src", "main.cpp")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            assigned = _resolve(
                {"codeBlockData": {f"file://{file_path}": {"language": "cpp"}}},
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-cbd")


class TestDetermineProjectBubbleStages(unittest.TestCase):
    def _bubble_composer(self, bubble_id: str) -> dict:
        return {"fullConversationHeadersOnly": [{"bubbleId": bubble_id}]}

    def test_bubble_relevant_files_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "bubbleproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-bubble", ws_root)]
            file_path = os.path.join(ws_root, "README.md")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("hi")

            assigned = _resolve(
                self._bubble_composer("b-rel"),
                bubble_map={
                    "b-rel": Bubble.from_dict(
                        {"relevantFiles": [file_path]}, bubble_id="b-rel"
                    ),
                },
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-bubble")

    def test_bubble_attached_file_chunks_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "attachproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-attach", ws_root)]
            file_path = os.path.join(ws_root, "pkg", "mod.py")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            assigned = _resolve(
                self._bubble_composer("b-att"),
                bubble_map={
                    "b-att": Bubble.from_dict(
                        {"attachedFileCodeChunksUris": [{"path": file_path}]},
                        bubble_id="b-att",
                    ),
                },
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-attach")

    def test_bubble_context_file_selections_resolves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "ctxproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-ctx", ws_root)]
            file_path = os.path.join(ws_root, "docs", "guide.md")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            assigned = _resolve(
                self._bubble_composer("b-ctx"),
                bubble_map={
                    "b-ctx": Bubble.from_dict(
                        {
                            "context": {
                                "fileSelections": [{"uri": {"path": file_path}}]
                            }
                        },
                        bubble_id="b-ctx",
                    ),
                },
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-ctx")

    def test_non_dict_headers_skipped_without_crash(self) -> None:
        assigned = _resolve(
            {
                "fullConversationHeadersOnly": [
                    "not-a-dict",
                    None,
                    {"bubbleId": "missing"},
                ],
            },
            bubble_map={},
        )
        self.assertIsNone(assigned)


class TestDetermineProjectPathSegmentStage(unittest.TestCase):
    def test_path_segment_matching_last_resort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "storage", "segproj")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-seg", ws_root)]

            # Path outside workspace roots but contains folder basename "segproj".
            orphan = os.path.join(tmp, "external", "segproj", "orphan.py")
            os.makedirs(os.path.dirname(orphan), exist_ok=True)

            assigned = _resolve(
                {"newlyCreatedFiles": [{"uri": {"path": orphan}}]},
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-seg")

    def test_longer_folder_name_wins_path_segment_tie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            short_root = os.path.join(tmp, "short")
            long_root = os.path.join(tmp, "short-long")
            os.makedirs(short_root, exist_ok=True)
            os.makedirs(long_root, exist_ok=True)
            entries = [
                _write_workspace_json(tmp, "ws-short", short_root),
                _write_workspace_json(tmp, "ws-long", long_root),
            ]

            orphan = os.path.join(
                tmp, "external", "short-long", "short", "file.py"
            )
            os.makedirs(os.path.dirname(orphan), exist_ok=True)

            assigned = _resolve(
                {"newlyCreatedFiles": [{"uri": {"path": orphan}}]},
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-long")

    def test_path_segment_matching_from_bubble_relevant_files(self) -> None:
        """Path-segment last resort can use bubble file refs when composer has no file keys."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "storage", "bubbleseg")
            os.makedirs(ws_root, exist_ok=True)
            entries = [_write_workspace_json(tmp, "ws-bubble-seg", ws_root)]

            orphan = os.path.join(tmp, "external", "bubbleseg", "orphan.py")
            os.makedirs(os.path.dirname(orphan), exist_ok=True)

            assigned = _resolve(
                {"fullConversationHeadersOnly": [{"bubbleId": "b-seg"}]},
                bubble_map={
                    "b-seg": Bubble.from_dict(
                        {"relevantFiles": [orphan]}, bubble_id="b-seg"
                    ),
                },
                workspace_entries=entries,
            )
            self.assertEqual(assigned, "ws-bubble-seg")


class TestDetermineProjectTerminalNone(unittest.TestCase):
    def test_all_stages_fail_returns_none(self) -> None:
        assigned = _resolve(_EMPTY_COMPOSER)
        self.assertIsNone(assigned)

    def test_empty_composer_data_returns_none(self) -> None:
        assigned = _resolve({})
        self.assertIsNone(assigned)

    def test_schema_drift_empty_values_return_none(self) -> None:
        assigned = _resolve(
            {
                "fullConversationHeadersOnly": None,
                "newlyCreatedFiles": None,
                "codeBlockData": None,
            },
            project_layouts_map={"cmp-test": []},
            composer_id="cmp-test",
        )
        self.assertIsNone(assigned)

    def test_malformed_newly_created_entries_skipped(self) -> None:
        assigned = _resolve(
            {
                "newlyCreatedFiles": [
                    "not-a-dict",
                    {"uri": "not-a-dict"},
                    {"uri": {"path": None}},
                ],
            },
        )
        self.assertIsNone(assigned)


_JSON_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.text(max_size=120),
    st.lists(st.text(max_size=40), max_size=4),
)

_COMPOSER_RAW = st.dictionaries(
    st.text(min_size=0, max_size=30),
    st.one_of(
        _JSON_VALUES,
        st.lists(
            st.one_of(
                st.dictionaries(st.text(max_size=20), _JSON_VALUES, max_size=4),
                st.text(max_size=80),
                st.none(),
            ),
            max_size=6,
        ),
        st.dictionaries(st.text(max_size=20), _JSON_VALUES, max_size=6),
    ),
    max_size=10,
)

_BUBBLE_MAP_RAW = st.dictionaries(
    st.text(min_size=0, max_size=24),
    st.one_of(_COMPOSER_RAW, st.none(), st.text(max_size=80)),
    max_size=6,
)


class TestDetermineProjectFuzz(unittest.TestCase):
    @given(composer_data=_COMPOSER_RAW, bubble_map=_BUBBLE_MAP_RAW)
    @settings(max_examples=100, deadline=None)
    def test_never_raises_on_arbitrary_inputs(
        self, composer_data: dict, bubble_map: dict
    ) -> None:
        result = determine_project_for_conversation(
            composer_data=composer_data,
            composer_id="fuzz-cid",
            project_layouts_map={},
            project_name_to_workspace_id={},
            workspace_path_to_id={},
            workspace_entries=[],
            bubble_map=_bubble_map_from_raw(bubble_map),
            composer_id_to_workspace_id=None,
            invalid_workspace_ids=None,
        )
        self.assertTrue(result is None or isinstance(result, str))

    @given(
        composer_id=st.text(min_size=1, max_size=40),
        workspace_id=st.text(min_size=1, max_size=40),
        composer_data=_COMPOSER_RAW,
    )
    @settings(max_examples=60, deadline=None)
    def test_valid_definitive_mapping_always_wins(
        self, composer_id: str, workspace_id: str, composer_data: dict
    ) -> None:
        result = determine_project_for_conversation(
            composer_data=composer_data,
            composer_id=composer_id,
            project_layouts_map={composer_id: [normalize_file_path("/other")]},
            project_name_to_workspace_id={"other": "ws-other"},
            workspace_path_to_id={normalize_file_path("/other"): "ws-other"},
            workspace_entries=[],
            bubble_map={},
            composer_id_to_workspace_id={composer_id: workspace_id},
            invalid_workspace_ids=set(),
        )
        self.assertEqual(result, workspace_id)

    @given(
        composer_id=st.text(min_size=1, max_size=40),
        invalid_id=st.text(min_size=1, max_size=40),
    )
    @settings(max_examples=60, deadline=None)
    def test_invalid_definitive_mapping_never_returned_without_fallback(
        self, composer_id: str, invalid_id: str
    ) -> None:
        """Ignored invalid mapping must not leak through when no heuristic stage matches."""
        result = determine_project_for_conversation(
            composer_data=dict(_EMPTY_COMPOSER),
            composer_id=composer_id,
            project_layouts_map={},
            project_name_to_workspace_id={},
            workspace_path_to_id={},
            workspace_entries=[],
            bubble_map={},
            composer_id_to_workspace_id={composer_id: invalid_id},
            invalid_workspace_ids={invalid_id},
        )
        self.assertNotEqual(result, invalid_id)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
