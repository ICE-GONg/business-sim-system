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
            "SUPER_BOT_REMOTE_URLS": "",
            "SUPER_BOT_FALLBACK_URL": "",
            "SUPER_BOT_REMOTE_TOKEN": "test-token",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        from sim import db

        self.db = db
        self.health_patch = patch(
            "sim.remote_worker.remote_health",
            side_effect=lambda endpoints, timeout: [
                {"line": index, "ok": True, "message": "已连通，计算规则版本一致", "seconds": 0.01}
                for index, _ in enumerate(endpoints, 1)
            ],
        )
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        previous_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", previous_path)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()

    def prepare(self, *, super_bot=True, human=False):
        from sim.bots import finalize_super_bot_decisions, submit_bot_decisions, submit_super_bot_decisions
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
            if super_bot:
                finalize_super_bot_decisions(conn, 1)
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
            self.assertEqual(self.db.one(conn, "SELECT COUNT(*) AS n FROM decisions WHERE submitted_at IS NOT NULL AND is_draft=0")["n"], 3)
            self.assertEqual(self.db.one(conn, "SELECT COUNT(*) AS n FROM decisions WHERE is_draft=1")["n"], 1)
            self.assertEqual(self.db.one(conn, "SELECT phase FROM super_bot_remote_jobs WHERE round_no=1")["phase"], "done")
        self.assertTrue(any("已撤销第 1 轮" in item.value for item in self.at.success))
        submit_all = next(
            item for item in self.at.button
            if item.label == "正式提交全部 Super Bot 决策"
        )
        self.assertFalse(submit_all.disabled)
        submit_all.click().run()
        self.assertFalse(self.at.exception)
        with self.db.connect() as conn:
            self.assertEqual(self.db.one(conn, "SELECT COUNT(*) AS n FROM decisions WHERE submitted_at IS NOT NULL AND is_draft=0")["n"], 4)
            self.assertEqual(self.db.one(conn, "SELECT COUNT(*) AS n FROM decisions WHERE is_draft=1")["n"], 0)
        self.assertTrue(any("已正式提交并锁定" in item.value for item in self.at.success))

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

    def test_health_button_accepts_native_secrets_array_without_computing(self):
        self.prepare(super_bot=False)
        endpoints = ["https://local.test", "https://cloud.test"]
        self.at.secrets["SUPER_BOT_REMOTE_URLS"] = endpoints
        self.at.run()
        statuses = [
            {"line": 1, "ok": True, "message": "已连通", "seconds": 0.01},
            {"line": 2, "ok": False, "message": "连接失败或超时", "seconds": 4.0},
        ]
        with patch("sim.remote_worker.remote_health", return_value=statuses) as health, patch(
            "sim.remote_worker._post", side_effect=AssertionError("health must not compute"),
        ):
            self.at.button(key="check_compute_health").click().run()
        self.assertFalse(self.at.exception)
        health.assert_called_once_with(endpoints, timeout=2.5)
        self.assertTrue(any("第 1 线路" in row.value and "已连通" in row.value for row in self.at.success))
        self.assertTrue(any("第 2 线路" in row.value and "连接失败" in row.value for row in self.at.warning))

    def test_admin_can_pair_local_worker_as_first_line(self):
        self.prepare(super_bot=False)
        local_url = "https://fresh-local.trycloudflare.com"
        self.at.text_input(key="local_compute_url_input").set_value(local_url).run()
        status = [{"line": 1, "ok": True, "message": "已连通", "seconds": 0.02}]
        with patch("sim.remote_worker.remote_health", return_value=status) as health:
            self.at.button(key="connect_local_compute").click().run()
        self.assertFalse(self.at.exception)
        health.assert_called_once_with([local_url], timeout=2.5)
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "super_bot_local_worker_url", "", str),
                local_url,
            )
        self.assertTrue(any("本地算力已连接" in row.value for row in self.at.success))

    def test_offline_saved_local_line_is_skipped_before_long_compute_request(self):
        self.prepare()
        local_url = "https://expired-local.trycloudflare.com"
        with self.db.connect() as conn:
            self.db.set_setting(conn, "super_bot_local_worker_url", local_url)
        offline = [
            {"line": 1, "ok": False, "message": "连接失败或超时", "seconds": 2.5},
            {"line": 2, "ok": True, "message": "已连通，计算规则版本一致", "seconds": 0.01},
        ]
        with patch("sim.remote_worker.remote_health", return_value=offline) as health, patch(
            "sim.remote_worker._post", self.post,
        ):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        health.assert_called_once_with([local_url, "https://worker.test"], timeout=2.5)
        self.assertEqual(self.calls, ["bot", "rebalance"])

    def test_stale_cloud_falls_back_to_current_app_and_finishes_drafts(self):
        self.prepare()
        stale = [{"line": 1, "ok": False, "reachable": True, "message": "版本不一致", "seconds": 0.01}]
        with patch("sim.remote_worker.remote_health", return_value=stale), patch(
            "sim.remote_worker._post", side_effect=AssertionError("must not compute on a stale node"),
        ):
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertFalse(self.at.error)
        with self.db.connect() as conn:
            self.assertEqual(self.db.one(conn, "SELECT is_draft FROM decisions WHERE company_id=?", (self.super_id,))["is_draft"], 1)
            self.assertEqual(self.db.one(conn, "SELECT phase FROM super_bot_remote_jobs WHERE round_no=1")["phase"], "done")
        self.assertTrue(any("已撤销第 1 轮" in row.value for row in self.at.success))

    def test_node_version_race_after_saved_bot_finishes_with_current_app(self):
        from sim.remote_worker import RemoteRevisionError, submit_local_super_bots

        self.prepare()

        def race(endpoint, token, payload):
            if payload["phase"] == "rebalance":
                with self.db.connect() as conn:
                    self.assertEqual(self.db.one(conn, "SELECT is_draft FROM decisions WHERE company_id=?", (self.super_id,))["is_draft"], 1)
                raise RemoteRevisionError("node changed version")
            return self.post(endpoint, token, payload)

        with patch("sim.remote_worker._post", race), patch(
            "sim.remote_worker.submit_local_super_bots", wraps=submit_local_super_bots,
        ) as local:
            self.at.button(key="round_rollback_button").click().run()
        self.assertFalse(self.at.exception)
        self.assertFalse(self.at.error)
        self.assertEqual(self.calls, ["bot"])
        self.assertTrue(local.call_args.kwargs["replace_existing"])
        with self.db.connect() as conn:
            self.assertEqual(self.db.one(conn, "SELECT is_draft FROM decisions WHERE company_id=?", (self.super_id,))["is_draft"], 1)
            self.assertEqual(self.db.one(conn, "SELECT phase FROM super_bot_remote_jobs WHERE round_no=1")["phase"], "done")

    def test_pairing_stale_local_worker_reports_version_not_connection(self):
        self.prepare(super_bot=False)
        self.at.text_input(key="local_compute_url_input").set_value("https://stale-local.test").run()
        stale = [{"line": 1, "ok": False, "reachable": True, "message": "版本不一致", "seconds": 0.01}]
        with patch("sim.remote_worker.remote_health", return_value=stale):
            self.at.button(key="connect_local_compute").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("已连通，但计算规则版本不一致" in row.value for row in self.at.error))
        with self.db.connect() as conn:
            self.assertEqual(self.db.get_setting(conn, "super_bot_local_worker_url", "", str), "")
