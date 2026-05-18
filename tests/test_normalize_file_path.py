"""Tests for utils.path_helpers.normalize_file_path.

Covers the shared implementation that was previously duplicated in
scripts/export.py (closes #46). All call-sites in both the web app and the
CLI export script now use this single copy.

Edge-case matrix:
  - file:/// and file:// URI schemes
  - Percent-encoded characters: spaces (%20), colons (%3A), hashes (%23)
  - Windows-style drive paths (backslash and forward-slash) on all platforms
  - Drive-letter lowercasing on win32
  - Plain POSIX paths pass through unchanged
  - Empty / None-like input
"""

import sys
import unittest

from utils.path_helpers import normalize_file_path


class TestNormalizeFilePathUriStripping(unittest.TestCase):
    def test_file_triple_slash_stripped(self) -> None:
        out = normalize_file_path("file:///home/user/project")
        self.assertFalse(out.startswith("file:"))
        self.assertIn("home", out)

    def test_file_double_slash_stripped(self) -> None:
        out = normalize_file_path("file://server/share/file.txt")
        self.assertFalse(out.startswith("file:"))
        self.assertIn("share", out)

    def test_empty_string(self) -> None:
        self.assertEqual(normalize_file_path(""), "")


class TestNormalizeFilePathPercentEncoding(unittest.TestCase):
    def test_space_decoded(self) -> None:
        out = normalize_file_path("file:///C:/My%20Documents/file.txt")
        self.assertNotIn("%20", out)
        self.assertIn("my documents", out)

    def test_hash_decoded(self) -> None:
        out = normalize_file_path("file:///C:/repo/src%23internal/mod.py")
        self.assertNotIn("%23", out)
        self.assertIn("#", out)

    def test_percent_encoded_colon_in_uri_prefix(self) -> None:
        """URI-style /d%3A/... path: %3A is decoded to ':'.

        On win32 the backslash branch is entered (leading slash removed
        and path lowercased). On other platforms the leading slash prevents
        the Windows-drive branch, so the path is returned as decoded only.
        """
        out = normalize_file_path("/d%3A/_Work/project")
        self.assertNotIn("%3A", out)
        if sys.platform == "win32":
            self.assertEqual(out, r"d:\_work\project")
        else:
            self.assertEqual(out, "/d:/_Work/project")


class TestNormalizeFilePathWindowsDrives(unittest.TestCase):
    """Paths with Windows-style drive letters are normalised on all platforms.

    On win32 the win32 branch handles them natively.  On Linux/macOS the
    ``^[a-zA-Z]:[/\\]`` regex branch converts forward-slashes to backslashes
    and lowercases the path so cross-platform reads of Cursor's Windows
    workspaceStorage produce consistent keys.
    """

    def test_backslash_drive_path_lowercased(self) -> None:
        out = normalize_file_path(r"D:\Work\Boost")
        self.assertEqual(out, r"d:\work\boost")

    def test_forward_slash_drive_path_converted(self) -> None:
        out = normalize_file_path("D:/Work/Boost")
        self.assertEqual(out, r"d:\work\boost")

    def test_file_uri_with_windows_drive(self) -> None:
        out = normalize_file_path("file:///C:/Users/Dev/project")
        self.assertIn("users", out)
        self.assertIn("dev", out)
        self.assertTrue(out.startswith("c:"))

    def test_mixed_case_drive_lowercased(self) -> None:
        out = normalize_file_path(r"E:\Mixed\Case\Path")
        self.assertTrue(out.startswith("e:"))
        self.assertEqual(out, r"e:\mixed\case\path")


class TestNormalizeFilePathPosixPassthrough(unittest.TestCase):
    def test_plain_posix_path_unchanged_on_non_windows(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX path semantics differ on win32")
        out = normalize_file_path("/home/user/project")
        self.assertEqual(out, "/home/user/project")

    def test_path_without_scheme_unchanged(self) -> None:
        if sys.platform == "win32":
            self.skipTest("plain relative path behaviour differs on win32")
        out = normalize_file_path("relative/path/file.py")
        self.assertEqual(out, "relative/path/file.py")


if __name__ == "__main__":
    unittest.main()
