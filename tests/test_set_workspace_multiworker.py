"""POST /api/set-workspace behavior under multi-worker WSGI deployments."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.test_workspace_path_validation import _make_cursor_workspace_dir


class TestSetWorkspaceMultiWorker(unittest.TestCase):
    def setUp(self):
        from flask import Flask

        from api.config_api import bp as config_bp
        from utils.workspace_path import set_workspace_path_override

        self.tmp = tempfile.mkdtemp(prefix="cursor-multiworker-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(set_workspace_path_override, None)

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.register_blueprint(config_bp)
        self.client = app.test_client()
        self.storage = _make_cursor_workspace_dir(self.tmp)

    def test_multi_worker_returns_409_with_stable_code(self):
        with patch(
            "api.config_api.is_multi_worker_process_deployment",
            return_value=True,
        ):
            resp = self.client.post(
                "/api/set-workspace",
                json={"path": self.storage},
            )
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body["code"], "set_workspace_multi_worker_unsupported")
        self.assertIn("WORKSPACE_PATH", body["error"])

    def test_single_process_still_succeeds_when_not_multi_worker(self):
        with patch(
            "api.config_api.is_multi_worker_process_deployment",
            return_value=False,
        ):
            resp = self.client.post(
                "/api/set-workspace",
                json={"path": self.storage},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])


class TestMultiWorkerDetection(unittest.TestCase):
    def test_explicit_env_flag(self):
        from utils.workspace_path import is_multi_worker_process_deployment

        with patch.dict(os.environ, {"CURSOR_BROWSER_MULTI_WORKER": "1"}, clear=False):
            self.assertTrue(is_multi_worker_process_deployment())
        with patch.dict(os.environ, {"CURSOR_BROWSER_MULTI_WORKER": "0"}, clear=False):
            self.assertFalse(is_multi_worker_process_deployment())

    def test_web_concurrency_gt_one(self):
        from utils.workspace_path import is_multi_worker_process_deployment

        with patch.dict(
            os.environ,
            {"WEB_CONCURRENCY": "4", "CURSOR_BROWSER_MULTI_WORKER": ""},
            clear=False,
        ):
            self.assertTrue(is_multi_worker_process_deployment())

    def test_gunicorn_cmd_args_workers(self):
        from utils.workspace_path import is_multi_worker_process_deployment

        with patch.dict(
            os.environ,
            {
                "GUNICORN_CMD_ARGS": "app:create_app --bind :5000 --workers 3",
                "CURSOR_BROWSER_MULTI_WORKER": "",
            },
            clear=False,
        ):
            self.assertTrue(is_multi_worker_process_deployment())
