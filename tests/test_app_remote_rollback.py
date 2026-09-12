from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class RemoteRollbackAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-rollback-ui-")
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {
            "SIM_DB_PATH": str(Path(self.temp.name) / "ui.db"),
            "SUPER_BOT_REMOTE_URL": "https://worker.test",
            "SUPER_BOT_REMOTE_TOKEN": "test-token",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from sim import db

        self.db = db
        previous_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", previous_path)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()

    def prepare(self, *, super_bot=True, human=False):
        from sim.bots import submit_bot_decisions, submit_super_bot_decisions
        from sim.engine import settle_round

        with self.db.connect() as conn:
            conn.execute(
                "UPDATE companies SET home_city='广州',setup_submitted_at=?,is_bot=1",
                (self.db.now_iso(),),
            )
            ids = [row["id"] for row in self.db.all_rows(conn, "SELECT id FROM companies ORDER BY id")]
            self.super_id = ids[0] if super_bot else None
            if super_bot:
                conn.execute("UPDATE companies SET is_super_bot=1 WHERE id=?", (ids[0],))
            conn.execute("UPDATE rounds SET status='open',starts_at=? WHERE round_no=1", (self.db.now_iso(),))
            for company_id in ids:
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company_id,))
            submit_bot_decisions(conn, 1)
            if super_bot:
                submit_super_bot_decisions(conn, 1)
            if human:
                # Preserve an ordinary generated decision as the human's
                # submitted test decision; rollback must make it a draft.
                conn.execute("UPDATE companies SET is_bot=0 WHERE id=?", (ids[-1],))
            settle_round(conn, 1)
        self.calls = []
        self.at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
        self.at.session_state["auth"] = {"role": "admin"}
        self.at.run()
        self.at.sidebar.radio[0].set_value("回合控制").run()
        self.at.text_input(key="round_rollback_confirm").set_value("ROLLBACK").run()
        self.assertFalse(self.at.exception)

    def post(self, endpoint, token, payload):
        from sim.remote_worker import decode_snapshot, load_snapshot, run_remote_request

        self.assertEqual(endpoint, "https://worker.test")
        # A separate connection must see the completed rollback before the
        # HTTP request starts, with no long-running writer transaction held.
        with self.db.connect() as observed:
            self.assertEqual(self.db.one(observed, "SELECT status FROM rounds WHERE round_no=1")["status"], "open")
        self.calls.append(payload["phase"])
        with closing(sqlite3.connect(":memory:")) as worker:
            worker.row_factory = sqlite3.Row
            load_snapshot(worker, decode_snapshot(payload))
            return run_remote_request(worker, payload)

    def test_rollback_uses_remote_after_committing_reopened_round(self):
        self.prepare()
        with patch("sim.remote_worker._post", self.post):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.calls, ["bot", "rebalance"])
        with self.db.connect() as conn:
            self.assertEqual(self.db.one(conn, "SELECT COUNT(*) AS n FROM decisions WHERE submitted_at IS NOT NULL")["n"], 4)
            self.assertEqual(self.db.one(conn, "SELECT phase FROM super_bot_remote_jobs WHERE round_no=1")["phase"], "done")
        self.assertTrue(any("已撤销第 1 轮" in item.value for item in self.at.success))

    def test_remote_interruption_does_not_undo_or_repeat_rollback(self):
        from sim.remote_worker import RemoteWorkerError

        self.prepare()

        def interrupted(endpoint, token, payload):
            if payload["phase"] == "rebalance":
                raise RemoteWorkerError("test interruption")
            return self.post(endpoint, token, payload)

        with patch("sim.remote_worker._post", interrupted):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.calls, ["bot"])
        self.assertTrue(any("无需再次回退" in item.value for item in self.at.warning))
        self.assertTrue(any(item.label == "继续分析未完成的超级 Bot" for item in self.at.button))
        with self.db.connect() as conn:
            self.assertEqual(self.db.one(conn, "SELECT status FROM rounds WHERE round_no=1")["status"], "open")
            self.assertIsNotNone(self.db.one(conn, "SELECT submitted_at FROM decisions WHERE company_id=?", (self.super_id,))["submitted_at"])

    def test_no_super_bots_does_not_call_remote(self):
        self.prepare(super_bot=False)
        with patch("sim.remote_worker._post", side_effect=AssertionError("unexpected remote call")):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("已撤销第 1 轮" in item.value for item in self.at.success))

    def test_unsubmitted_human_waits_before_remote_analysis(self):
        self.prepare(human=True)
        with patch("sim.remote_worker._post", side_effect=AssertionError("unexpected remote call")):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("已撤销第 1 轮" in item.value for item in self.at.success))
        with self.db.connect() as conn:
            self.assertIsNone(self.db.one(conn, "SELECT submitted_at FROM decisions WHERE company_id=?", (self.super_id,)))
