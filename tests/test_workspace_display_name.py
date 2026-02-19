"""
Tests for workspace folder parsing and display-name extraction.
"""

import unittest

from utils.path_helpers import get_workspace_display_name, get_workspace_folder_paths


class TestWorkspaceFolderParsing(unittest.TestCase):
    def test_get_workspace_folder_paths_handles_multi_root_uri_shape(self):
        wd = {
            "folders": [
                {"uri": {"scheme": "file", "path": "/d%3A/_Cpp_Digest/cppdigest-github-app"}},
                {"uri": {"scheme": "file", "path": "/d%3A/_Cpp_Digest/boostbacklog"}},
            ]
        }
        paths = get_workspace_folder_paths(wd)
        self.assertEqual(len(paths), 2)
        self.assertIn("/d%3A/_Cpp_Digest/cppdigest-github-app", paths)
        self.assertIn("/d%3A/_Cpp_Digest/boostbacklog", paths)

    def test_get_workspace_display_name_prefers_first_valid_folder(self):
        wd = {
            "folders": [
                {"uri": {"scheme": "file", "path": "/d%3A/_Cpp_Digest/cppdigest-github-app"}},
                {"uri": {"scheme": "file", "path": "/d%3A/_Cpp_Digest/boostbacklog"}},
            ]
        }
        self.assertEqual(get_workspace_display_name(wd, fallback="workspace-id"), "cppdigest-github-app")

    def test_get_workspace_display_name_fallback_when_no_paths(self):
        wd = {"folders": [{"uri": {"scheme": "file"}}]}
        self.assertEqual(get_workspace_display_name(wd, fallback="workspace-id"), "workspace-id")


if __name__ == "__main__":
    unittest.main()
