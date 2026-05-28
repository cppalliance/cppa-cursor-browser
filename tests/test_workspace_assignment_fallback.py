"""
Tests for conversation-to-workspace assignment fallback behavior.
"""

import unittest

from services.workspace_resolver import determine_project_for_conversation
from utils.path_helpers import normalize_file_path


class TestWorkspaceAssignmentFallback(unittest.TestCase):
    def test_ignores_invalid_composer_to_workspace_mapping(self):
        composer_data = {
            "fullConversationHeadersOnly": [],
            "newlyCreatedFiles": [],
            "codeBlockData": {},
        }
        composer_id = "cmp-123"
        project_layouts_map = {"cmp-123": [normalize_file_path("/d%3A/_Cpp_Digest/boostbacklog")]}
        project_name_to_workspace_id = {"boostbacklog": "good-ws"}
        workspace_path_to_id = {normalize_file_path("d:\\_cpp_digest\\boostbacklog"): "good-ws"}
        workspace_entries = []
        bubble_map = {}
        composer_id_to_workspace_id = {"cmp-123": "broken-ws"}
        invalid_workspace_ids = {"broken-ws"}

        assigned = determine_project_for_conversation(
            composer_data=composer_data,
            composer_id=composer_id,
            project_layouts_map=project_layouts_map,
            project_name_to_workspace_id=project_name_to_workspace_id,
            workspace_path_to_id=workspace_path_to_id,
            workspace_entries=workspace_entries,
            bubble_map=bubble_map,
            composer_id_to_workspace_id=composer_id_to_workspace_id,
            invalid_workspace_ids=invalid_workspace_ids,
        )

        self.assertEqual(assigned, "good-ws")


if __name__ == "__main__":
    unittest.main()
