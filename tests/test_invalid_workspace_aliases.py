"""
Tests for invalid-workspace alias inference.
"""

import json
import unittest

from api.workspaces import _infer_invalid_workspace_aliases
from utils.path_helpers import normalize_file_path


class TestInvalidWorkspaceAliases(unittest.TestCase):
    def test_majority_vote_alias_selection(self):
        # `createdAt` is required by Composer.from_dict (issue #24's strict
        # numeric-millis gate). Production rows always carry it; the original
        # fixture predated the gate.
        composer_rows = [
            {"key": "composerData:cid-1", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-2", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-3", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
        ]
        composer_id_to_ws = {
            "cid-1": "invalid-ws",
            "cid-2": "invalid-ws",
            "cid-3": "invalid-ws",
        }

        # Drive inference through project_layouts_map -> workspace_path_map
        project_layouts_map = {
            "cid-1": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-2": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-3": [normalize_file_path(r"d:\_Cpp_Digest\team-brain")],
        }
        workspace_path_map = {
            normalize_file_path(r"d:\_cpp_digest\boostbacklog"): "boost-ws",
            normalize_file_path(r"d:\_cpp_digest\team-brain"): "team-ws",
        }

        aliases = _infer_invalid_workspace_aliases(
            composer_rows=composer_rows,
            project_layouts_map=project_layouts_map,
            project_name_map={},
            workspace_path_map=workspace_path_map,
            workspace_entries=[],
            bubble_map={},
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids={"invalid-ws"},
        )

        self.assertEqual(aliases.get("invalid-ws"), "boost-ws")

    def test_drifted_composer_does_not_skew_vote(self):
        # CodeRabbit regression check: a schema-drifted composer
        # (e.g. missing createdAt) must NOT cast a vote, because if it did
        # it could outweigh well-formed composers and misassign every other
        # composer mapped to the same invalid workspace.
        composer_rows = [
            # Two well-formed votes for boost-ws
            {"key": "composerData:cid-1", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-2", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            # One drifted row that, if counted, would vote for team-ws
            # (no createdAt → Composer.from_dict raises SchemaError → skip)
            {"key": "composerData:cid-3", "value": json.dumps({"fullConversationHeadersOnly": []})},
        ]
        composer_id_to_ws = {"cid-1": "invalid-ws", "cid-2": "invalid-ws", "cid-3": "invalid-ws"}
        project_layouts_map = {
            "cid-1": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-2": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-3": [normalize_file_path(r"d:\_Cpp_Digest\team-brain")],
        }
        workspace_path_map = {
            normalize_file_path(r"d:\_cpp_digest\boostbacklog"): "boost-ws",
            normalize_file_path(r"d:\_cpp_digest\team-brain"): "team-ws",
        }

        aliases = _infer_invalid_workspace_aliases(
            composer_rows=composer_rows,
            project_layouts_map=project_layouts_map,
            project_name_map={},
            workspace_path_map=workspace_path_map,
            workspace_entries=[],
            bubble_map={},
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids={"invalid-ws"},
        )

        # cid-3 is dropped (drift), so boost-ws wins 2-0 (not 2-1)
        self.assertEqual(aliases.get("invalid-ws"), "boost-ws")

    def test_non_dict_composer_json_skipped_without_crash(self) -> None:
        composer_rows = [
            {"key": "composerData:cid-1", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-2", "value": json.dumps({"createdAt": 1_715_000_000_000, "fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-bad", "value": json.dumps("not-a-dict")},
        ]
        composer_id_to_ws = {"cid-1": "invalid-ws", "cid-2": "invalid-ws", "cid-bad": "invalid-ws"}
        project_layouts_map = {
            "cid-1": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-2": [normalize_file_path(r"d:\_Cpp_Digest\boostbacklog")],
            "cid-bad": [normalize_file_path(r"d:\_Cpp_Digest\team-brain")],
        }
        workspace_path_map = {
            normalize_file_path(r"d:\_cpp_digest\boostbacklog"): "boost-ws",
            normalize_file_path(r"d:\_cpp_digest\team-brain"): "team-ws",
        }

        aliases = _infer_invalid_workspace_aliases(
            composer_rows=composer_rows,
            project_layouts_map=project_layouts_map,
            project_name_map={},
            workspace_path_map=workspace_path_map,
            workspace_entries=[],
            bubble_map={},
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids={"invalid-ws"},
        )

        self.assertEqual(aliases.get("invalid-ws"), "boost-ws")


if __name__ == "__main__":
    unittest.main()
