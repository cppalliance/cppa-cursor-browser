"""Tests for utils.path_helpers path/timestamp helpers (closes #46).

Covers ``normalize_file_path`` and ``to_epoch_ms``, both previously duplicated
in scripts/export.py. All call-sites in the web app and CLI export script now
use the shared implementations in utils.path_helpers.

Test inventory (this module only): 21 cases — 12 ``normalize_file_path``,
9 ``to_epoch_ms``. On win32, 2 cases skip (POSIX passthrough in
``TestNormalizeFilePathPosixPassthrough`` only). A full-suite run may report
more skips (e.g. ``skipped=4``) from other test modules, not this file.
"""

import sys
import unittest
from datetime import datetime, timezone

from utils.path_helpers import normalize_file_path, to_epoch_ms


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

        Only test that exercises the leading-``/`` + drive-letter shape end-to-end
        (Cursor sometimes stores ``/d%3A/...`` URIs). Other drive-path tests use
        ``D:/...`` or ``D:\\...`` without a leading slash.

        On win32 the win32 branch strips the leading slash, lowercases, and
        normalises to backslashes. On other platforms the leading ``/`` prevents
        the ``^[a-zA-Z]:[/\\]`` cross-platform branch in ``path_helpers``, so the
        path is returned as percent-decoded only (no slash flip / lowercasing).
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
        # file:/// stripped, then same drive-letter branch as D:/ and D:\ inputs.
        self.assertEqual(out, r"c:\users\dev\project")

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


class TestToEpochMs(unittest.TestCase):
    def test_none_returns_zero(self) -> None:
        self.assertEqual(to_epoch_ms(None), 0)

    def test_ms_int_passthrough(self) -> None:
        self.assertEqual(to_epoch_ms(1_700_000_000_000), 1_700_000_000_000)

    def test_seconds_int_converted_to_ms(self) -> None:
        self.assertEqual(to_epoch_ms(1_700_000_000), 1_700_000_000_000)

    def test_seconds_float_converted_to_ms(self) -> None:
        self.assertEqual(to_epoch_ms(1_700_000_000.5), 1_700_000_000_500)

    def test_zero_returns_zero(self) -> None:
        self.assertEqual(to_epoch_ms(0), 0)

    def test_iso8601_zulu(self) -> None:
        expected = int(
            datetime(2026, 2, 3, 20, 39, 54, 17_000, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(to_epoch_ms("2026-02-03T20:39:54.017Z"), expected)

    def test_numeric_string_already_ms(self) -> None:
        self.assertEqual(to_epoch_ms("1700000000000"), 1_700_000_000_000)

    def test_numeric_string_seconds(self) -> None:
        self.assertEqual(to_epoch_ms("1700000000"), 1_700_000_000_000)

    def test_unrecognised_string_returns_zero(self) -> None:
        self.assertEqual(to_epoch_ms("not-a-timestamp"), 0)


if __name__ == "__main__":
    unittest.main()
