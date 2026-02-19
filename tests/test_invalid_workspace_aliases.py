"""
Tests for invalid-workspace alias inference.
"""

import json
import unittest

from api.workspaces import _infer_invalid_workspace_aliases


class TestInvalidWorkspaceAliases(unittest.TestCase):
    def test_majority_vote_alias_selection(self):
        composer_rows = [
            {"key": "composerData:cid-1", "value": json.dumps({"fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-2", "value": json.dumps({"fullConversationHeadersOnly": []})},
            {"key": "composerData:cid-3", "value": json.dumps({"fullConversationHeadersOnly": []})},
        ]
        composer_id_to_ws = {
            "cid-1": "invalid-ws",
            "cid-2": "invalid-ws",
            "cid-3": "invalid-ws",
        }

        # Drive inference through project_layouts_map -> workspace_path_map
        project_layouts_map = {
            "cid-1": [r"d:\_Cpp_Digest\boostbacklog"],
            "cid-2": [r"d:\_Cpp_Digest\boostbacklog"],
            "cid-3": [r"d:\_Cpp_Digest\team-brain"],
        }
        workspace_path_map = {
            r"d:\_cpp_digest\boostbacklog": "boost-ws",
            r"d:\_cpp_digest\team-brain": "team-ws",
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
