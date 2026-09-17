from __future__ import annotations

import ast
import sqlite3
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sim.remote_worker import (
    RemoteAnalysisChangedError,
    RemoteRevisionError,
    RemoteWorkerError,
)


class AppNodeFallbackTests(unittest.TestCase):
    """Exercise app routing without running or modifying a live Streamlit DB."""

    def setUp(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_remote_super_bot_submit")
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE saved_drafts(id INTEGER, investment INTEGER)")
        self.conn.execute("INSERT INTO saved_drafts VALUES(1, 125000)")
        self.conn.commit()
        self.routes = Mock(return_value=["https://local.test", "https://cloud.test"])
        self.health = Mock(return_value=[self.status(1, True), self.status(2, True)])
        self.remote = Mock()
        self.local = Mock()
        self.warning = Mock()
        self.connect = Mock(side_effect=lambda: nullcontext(self.conn))
        namespace = {
            "_remote_super_bot_endpoints": self.routes,
            "remote_health": self.health,
            "remote_submit": self.remote,
            "submit_local_super_bots": self.local,
            "RemoteRevisionError": RemoteRevisionError,
            "connect": self.connect,
            "_deployment_secret": Mock(return_value="test-token"),
            "LOGGER": Mock(),
            "st": SimpleNamespace(warning=self.warning),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), namespace)
        self.submit = namespace["_remote_super_bot_submit"]

    @staticmethod
    def status(line, ok, *, reachable=False):
        return {"line": line, "ok": ok, "reachable": reachable, "seconds": 0.01}

    def assert_drafts_unchanged(self):
        self.assertEqual(self.conn.execute("SELECT * FROM saved_drafts").fetchall(), [(1, 125000)])

    def test_stale_only_returns_same_app_fallback_without_remote_work(self):
        self.routes.return_value = ["https://cloud.test"]
        self.health.return_value = [self.status(1, False, reachable=True)]
        self.assertFalse(self.submit(4))
        self.remote.assert_not_called()
        self.local.assert_not_called()
        self.connect.assert_not_called()
        self.warning.assert_called_once()
        self.assert_drafts_unchanged()

    def test_fresh_local_and_stale_cloud_only_sends_to_fresh_node(self):
        self.health.return_value = [self.status(1, True), self.status(2, False, reachable=True)]
        progress = Mock()
        self.assertTrue(self.submit(4, replace_existing=True, progress_callback=progress))
        self.health.assert_called_once_with(self.routes.return_value, timeout=2.5)
        args, kwargs = self.remote.call_args
        self.assertEqual(args, (self.conn, ["https://local.test"], "test-token", 4))
        self.assertTrue(kwargs["replace_existing"])
        self.assertIs(kwargs["progress_callback"], progress)
        self.local.assert_not_called()
        self.assert_drafts_unchanged()

    def test_all_temporarily_offline_returns_same_app_fallback(self):
        self.health.return_value = [self.status(1, False), self.status(2, False)]
        self.assertFalse(self.submit(4))
        self.remote.assert_not_called()
        self.connect.assert_not_called()
        self.assert_drafts_unchanged()

    def test_offline_primary_uses_compatible_fallback(self):
        self.health.return_value = [self.status(1, False), self.status(2, True)]
        self.assertTrue(self.submit(4))
        self.assertEqual(self.remote.call_args.args[1], ["https://cloud.test"])
        self.local.assert_not_called()

    def test_version_race_recomputes_whole_batch_without_clearing_drafts(self):
        self.remote.side_effect = RemoteRevisionError("stale worker")
        self.local.side_effect = lambda *args, **kwargs: self.assert_drafts_unchanged()
        progress = Mock()
        self.assertTrue(self.submit(4, progress_callback=progress))
        self.local.assert_called_once_with(self.conn, 4, progress, replace_existing=True)
        self.warning.assert_called_once()
        self.assert_drafts_unchanged()

    def test_authentication_failure_is_not_hidden_by_fallback(self):
        self.remote.side_effect = RemoteWorkerError("鉴权失败 HTTP 403")
        with self.assertRaisesRegex(RemoteWorkerError, "403"):
            self.submit(4)
        self.local.assert_not_called()
        self.assert_drafts_unchanged()

    def test_changed_inputs_are_not_hidden_by_fallback(self):
        self.remote.side_effect = RemoteAnalysisChangedError("inputs changed")
        with self.assertRaises(RemoteAnalysisChangedError):
            self.submit(4)
        self.local.assert_not_called()
        self.assert_drafts_unchanged()

    def test_no_external_nodes_uses_existing_same_app_path(self):
        self.routes.return_value = []
        self.assertFalse(self.submit(4))
        self.health.assert_not_called()
        self.remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
