"""
Regression tests for issue #43 — thread-safe _workspace_path_override.

Run:
    python -m unittest tests.test_workspace_path_thread_safety -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from utils.workspace_path import (
    resolve_workspace_path,
    set_workspace_path_override,
)


class TestWorkspacePathThreadSafety(unittest.TestCase):
    """Concurrent set-workspace + resolve must not observe torn global state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cursor-ws-thread-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path_a = os.path.join(self.tmp, "storage-a")
        self.path_b = os.path.join(self.tmp, "storage-b")
        os.makedirs(self.path_a)
        os.makedirs(self.path_b)
        # Match resolve_workspace_path() (expand_tilde only — no realpath).
        self.allowed_resolved = {self.path_a, self.path_b}
        self._prior_workspace_env = os.environ.pop("WORKSPACE_PATH", None)
        self.addCleanup(self._restore_workspace_env)
        self.addCleanup(set_workspace_path_override, None)
        # With WORKSPACE_PATH popped and override None, this is resolve()'s
        # "override cleared" path — used by test_concurrent_clear_and_set.
        self.fallback_resolved = resolve_workspace_path()

    def _restore_workspace_env(self):
        if self._prior_workspace_env is None:
            os.environ.pop("WORKSPACE_PATH", None)
        else:
            os.environ["WORKSPACE_PATH"] = self._prior_workspace_env

    def test_concurrent_set_and_resolve_never_returns_mixed_paths(self):
        iterations = 500
        errors: list[str] = []
        start = threading.Barrier(9)  # 1 writer + 8 readers
        # Seed before workers start so readers never observe the unset default path.
        set_workspace_path_override(self.path_a)

        def writer() -> None:
            start.wait()
            for i in range(iterations):
                set_workspace_path_override(self.path_a if i % 2 == 0 else self.path_b)

        def reader() -> None:
            start.wait()
            for _ in range(iterations):
                resolved = resolve_workspace_path()
                if resolved not in self.allowed_resolved:
                    errors.append(
                        f"resolve returned unexpected path: {resolved!r}"
                    )

        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(writer)]
            futures.extend(pool.submit(reader) for _ in range(8))
            for fut in as_completed(futures):
                fut.result()

        self.assertEqual(errors, [], "\n".join(errors[:20]))

    def test_concurrent_clear_and_set_stays_consistent(self):
        iterations = 200
        errors: list[str] = []
        start = threading.Barrier(5)

        def toggler() -> None:
            start.wait()
            for i in range(iterations):
                if i % 3 == 0:
                    set_workspace_path_override(None)
                else:
                    set_workspace_path_override(
                        self.path_a if i % 2 == 0 else self.path_b
                    )

        def reader() -> None:
            start.wait()
            for _ in range(iterations):
                resolved = resolve_workspace_path()
                if (
                    resolved in self.allowed_resolved
                    or resolved == self.fallback_resolved
                ):
                    continue
                errors.append(f"resolve returned unexpected path: {resolved!r}")

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(toggler)]
            futures.extend(pool.submit(reader) for _ in range(4))
            for fut in as_completed(futures):
                fut.result()

        self.assertEqual(errors, [], "\n".join(errors[:20]))

    def test_explicit_override_takes_precedence_over_module_override(self):
        set_workspace_path_override(self.path_a)
        self.assertEqual(resolve_workspace_path(override=self.path_b), self.path_b)
        self.assertEqual(resolve_workspace_path(), self.path_a)


if __name__ == "__main__":
    unittest.main()
