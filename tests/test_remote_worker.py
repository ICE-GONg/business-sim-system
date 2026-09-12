from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sim import remote_worker as remote


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        global db
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"SIM_DB_PATH": str(Path(self.directory.name) / "test.db")})
        environment.start()
        self.addCleanup(environment.stop)
        from sim import db
        old_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", old_path)
        db.DB_PATH = Path(self.directory.name) / "test.db"
        db.init_db()
        self.conn = sqlite3.connect(db.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM companies WHERE id>2")
        self.conn.execute("UPDATE companies SET is_bot=1,is_super_bot=1,home_city='广州'")
        self.conn.execute("UPDATE rounds SET status='open',starts_at='start-1'")
        self.conn.execute("INSERT INTO settings VALUES('ADMIN_PASSWORD','do-not-transmit')")
        self.conn.commit()

    @staticmethod
    def fake_submit(conn, round_no, **kwargs):
        company_id = next(iter(kwargs["target_ids"]))
        conn.execute("DELETE FROM decisions WHERE company_id=? AND round_no=?", (company_id, round_no))
        conn.execute("INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) VALUES(?,?,?,?)",
                     (company_id, round_no, 100 * company_id, "new-submission"))
        for row in conn.execute("SELECT city FROM market_config"):
            conn.execute("INSERT OR REPLACE INTO city_decisions(company_id,round_no,city,price) VALUES(?,?,?,12345)", (company_id, round_no, row[0]))
        conn.commit()
        return 1

    @staticmethod
    def fake_rebalance(conn, round_no):
        conn.execute("UPDATE decisions SET management_investment=management_investment+1 WHERE round_no=?", (round_no,))
        return 2

    def dispatch(self, endpoint, token, payload):
        self.calls.append((payload["phase"], payload["company_id"]))
        with sqlite3.connect(":memory:") as worker:
            worker.row_factory = sqlite3.Row
            remote.load_snapshot(worker, remote.decode_snapshot(payload))
            with patch("sim.bots.submit_super_bot_decisions", self.fake_submit), patch("sim.bots.rebalance_super_bot_decisions", self.fake_rebalance):
                return remote.run_remote_request(worker, payload)

    def test_snapshot_omits_credentials_and_round_backups(self):
        request = remote.create_remote_request(self.conn, 1, company_id=1)
        raw = gzip.decompress(base64.b64decode(request["snapshot_b64"]))
        self.assertNotIn(b"do-not-transmit", raw)
        self.assertNotIn(b"pbkdf2_sha256$", raw)
        self.assertNotIn(b"round_snapshots", raw)
        with sqlite3.connect(":memory:") as worker:
            remote.load_snapshot(worker, remote.decode_snapshot(request))
            self.assertEqual(worker.execute("SELECT password_hash FROM companies").fetchone()[0], "")
        self.assertLess(len(request["snapshot_b64"]), len(raw))

    def test_stale_admin_edit_is_not_overwritten(self):
        self.calls = []
        request = remote.create_remote_request(self.conn, 1, company_id=1)
        result = self.dispatch("https://example.test", "token", request)
        self.conn.execute("UPDATE companies SET cash=123 WHERE id=1")
        self.conn.commit()
        with self.assertRaisesRegex(remote.RemoteWorkerError, "数据已改变"):
            remote.apply_remote_result(self.conn, request, result)
        self.assertEqual(self.conn.execute("SELECT cash FROM companies WHERE id=1").fetchone()[0], 123)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_wrong_scope_does_not_partially_write(self):
        self.calls = []
        request = remote.create_remote_request(self.conn, 1, company_id=1)
        result = self.dispatch("https://example.test", "token", request)
        result["city_decisions"][0]["company_id"] = 2
        with self.assertRaises(remote.RemoteWorkerError):
            remote.apply_remote_result(self.conn, request, result)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_interrupt_resumes_saved_bots_and_final_rebalance(self):
        self.calls = []
        progress = []
        def interrupted(done, total, stage):
            progress.append(stage)
            if stage == "C01":
                raise RuntimeError("browser disconnected")
        with patch.object(remote, "_post", self.dispatch):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                remote.remote_submit(self.conn, "https://example.test", "token", 1, progress_callback=interrupted)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
            self.assertTrue(remote.remote_pending(self.conn, 1))
            remote.remote_submit(self.conn, "https://example.test", "token", 1)
        self.assertEqual(self.calls, [("bot", 1), ("bot", 2), ("rebalance", None)])
        self.assertFalse(remote.remote_pending(self.conn, 1))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT management_investment FROM decisions ORDER BY company_id")], [101, 201])

    def test_interrupted_reanalysis_keeps_old_pending_decision(self):
        self.calls = []
        for company_id in (1, 2):
            self.conn.execute("INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) VALUES(?,1,99,'old')", (company_id,))
        self.conn.commit()
        def interrupted(done, total, stage):
            if stage == "C01":
                raise RuntimeError("stop")
        with patch.object(remote, "_post", self.dispatch):
            with self.assertRaises(RuntimeError):
                remote.remote_submit(self.conn, "https://example.test", "token", 1, replace_existing=True, progress_callback=interrupted)
            self.assertEqual(self.conn.execute("SELECT management_investment FROM decisions WHERE company_id=2").fetchone()[0], 99)
            remote.remote_submit(self.conn, "https://example.test", "token", 1, replace_existing=True)
        self.assertEqual(self.calls, [("bot", 1), ("bot", 2), ("rebalance", None)])

    def test_interrupt_before_final_rebalance_can_resume_alone(self):
        self.calls = []
        def interrupted(done, total, stage):
            if stage == "联合复算中":
                raise RuntimeError("stop")
        with patch.object(remote, "_post", self.dispatch):
            with self.assertRaises(RuntimeError):
                remote.remote_submit(self.conn, "https://example.test", "token", 1, progress_callback=interrupted)
            self.assertTrue(remote.remote_pending(self.conn, 1))
            remote.remote_submit(self.conn, "https://example.test", "token", 1, replace_existing=True)
        self.assertEqual(self.calls, [("bot", 1), ("bot", 2), ("rebalance", None)])

    def test_kds_change_between_segments_restarts_every_super_bot(self):
        self.calls = []

        def kds_submit(conn, round_no, **kwargs):
            company_id = next(iter(kwargs["target_ids"]))
            power = int(float(conn.execute(
                "SELECT value FROM settings WHERE key='cpi_price_power'"
            ).fetchone()[0]))
            conn.execute(
                "DELETE FROM decisions WHERE company_id=? AND round_no=?",
                (company_id, round_no),
            )
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) "
                "VALUES(?,?,?,?)",
                (company_id, round_no, power * 1000 + company_id, "new-submission"),
            )
            for row in conn.execute("SELECT city FROM market_config"):
                conn.execute(
                    "INSERT OR REPLACE INTO city_decisions(company_id,round_no,city,price) "
                    "VALUES(?,?,?,12345)",
                    (company_id, round_no, row[0]),
                )
            conn.commit()
            return 1

        def dispatch(endpoint, token, payload):
            self.calls.append((payload["phase"], payload["company_id"]))
            with sqlite3.connect(":memory:") as worker:
                worker.row_factory = sqlite3.Row
                remote.load_snapshot(worker, remote.decode_snapshot(payload))
                with patch("sim.bots.submit_super_bot_decisions", kds_submit), patch(
                    "sim.bots.rebalance_super_bot_decisions", self.fake_rebalance
                ):
                    return remote.run_remote_request(worker, payload)

        def interrupted(done, total, stage):
            if stage == "C01":
                raise RuntimeError("browser disconnected")

        old_power = int(float(self.conn.execute(
            "SELECT value FROM settings WHERE key='cpi_price_power'"
        ).fetchone()[0]))
        with patch.object(remote, "_post", dispatch):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                remote.remote_submit(
                    self.conn, "https://example.test", "token", 1,
                    progress_callback=interrupted,
                )
            # The completed segment remains durable after interruption.
            self.assertEqual(
                self.conn.execute(
                    "SELECT management_investment FROM decisions WHERE company_id=1"
                ).fetchone()[0],
                old_power * 1000 + 1,
            )
            self.conn.execute(
                "UPDATE settings SET value=? WHERE key='cpi_price_power'",
                (str(old_power + 7),),
            )
            self.conn.commit()
            remote.remote_submit(self.conn, "https://example.test", "token", 1)

        # Bot 1 is intentionally recalculated: the final batch cannot contain
        # one old-KDS decision and one new-KDS decision.
        self.assertEqual(self.calls, [
            ("bot", 1), ("bot", 1), ("bot", 2), ("rebalance", None),
        ])
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                "SELECT management_investment FROM decisions ORDER BY company_id"
            )],
            [(old_power + 7) * 1000 + 2, (old_power + 7) * 1000 + 3],
        )
        self.assertFalse(remote.remote_pending(self.conn, 1))

    def test_finished_job_becomes_pending_when_player_input_changes(self):
        self.calls = []
        self.conn.execute(
            "UPDATE companies SET is_bot=0,is_super_bot=0 WHERE id=2"
        )
        self.conn.execute(
            "INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) "
            "VALUES(2,1,500,'player')"
        )
        self.conn.commit()
        with patch.object(remote, "_post", self.dispatch):
            remote.remote_submit(self.conn, "https://example.test", "token", 1)
            self.assertFalse(remote.remote_pending(self.conn, 1))
            self.conn.execute(
                "UPDATE decisions SET management_investment=900 "
                "WHERE company_id=2 AND round_no=1"
            )
            self.conn.commit()
            self.assertTrue(remote.remote_pending(self.conn, 1))
            remote.remote_submit(self.conn, "https://example.test", "token", 1)
        self.assertEqual(self.calls, [
            ("bot", 1), ("rebalance", None),
            ("bot", 1), ("rebalance", None),
        ])

    def test_kds_change_during_one_call_restarts_before_next_segment(self):
        self.calls = []
        changed = False

        def mutate_after_first_save(done, total, stage):
            nonlocal changed
            if stage == "C01" and not changed:
                changed = True
                self.conn.execute(
                    "UPDATE settings SET value='19' WHERE key='cpi_price_power'"
                )
                self.conn.commit()

        with patch.object(remote, "_post", self.dispatch):
            remote.remote_submit(
                self.conn, "https://example.test", "token", 1,
                progress_callback=mutate_after_first_save,
            )
        self.assertEqual(self.calls, [
            ("bot", 1), ("bot", 1), ("bot", 2), ("rebalance", None),
        ])
        self.assertFalse(remote.remote_pending(self.conn, 1))

    def test_completed_local_fallback_is_invalidated_by_later_kds_edit(self):
        for company_id in (1, 2):
            self.fake_submit(self.conn, 1, target_ids={company_id})
        self.fake_rebalance(self.conn, 1)
        self.conn.commit()
        remote.mark_remote_job_complete(self.conn, 1)
        self.assertFalse(remote.remote_pending(self.conn, 1))
        self.conn.execute(
            "UPDATE settings SET value='23' WHERE key='cpi_price_power'"
        )
        self.conn.commit()
        self.assertTrue(remote.remote_pending(self.conn, 1))

    def test_pause_and_timer_extension_do_not_restart_saved_segments(self):
        self.calls = []

        def interrupted(done, total, stage):
            if stage == "C01":
                raise RuntimeError("browser disconnected")

        with patch.object(remote, "_post", self.dispatch):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                remote.remote_submit(
                    self.conn, "https://example.test", "token", 1,
                    progress_callback=interrupted,
                )
            self.conn.execute(
                "UPDATE rounds SET status='paused',ends_at='extended' WHERE round_no=1"
            )
            self.conn.commit()
            remote.remote_submit(self.conn, "https://example.test", "token", 1)
        self.assertEqual(self.calls, [
            ("bot", 1), ("bot", 2), ("rebalance", None),
        ])

    def test_local_fallback_restarts_if_kds_changes_during_batch(self):
        attempts = []
        changed = False

        def local_batch(conn, round_no, progress_callback=None, **kwargs):
            attempts.append(bool(kwargs.get("replace_existing")))
            for company_id in (1, 2):
                power = int(float(conn.execute(
                    "SELECT value FROM settings WHERE key='cpi_price_power'"
                ).fetchone()[0]))
                conn.execute(
                    "DELETE FROM decisions WHERE company_id=? AND round_no=?",
                    (company_id, round_no),
                )
                conn.execute(
                    "INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) "
                    "VALUES(?,?,?,'local')",
                    (company_id, round_no, power * 1000 + company_id),
                )
                for row in conn.execute("SELECT city FROM market_config"):
                    conn.execute(
                        "INSERT OR REPLACE INTO city_decisions(company_id,round_no,city,price) "
                        "VALUES(?,?,?,12345)",
                        (company_id, round_no, row[0]),
                    )
                conn.commit()
                if progress_callback:
                    progress_callback(company_id, 2, f"C0{company_id}")
            return 2

        def mutate_once(done, total, stage):
            nonlocal changed
            if stage == "C01" and not changed:
                changed = True
                self.conn.execute(
                    "UPDATE settings SET value='17' WHERE key='cpi_price_power'"
                )
                self.conn.commit()

        with patch("sim.bots.submit_super_bot_decisions", local_batch):
            self.assertEqual(
                remote.submit_local_super_bots(
                    self.conn, 1, mutate_once, max_restarts=1,
                ),
                2,
            )
        self.assertEqual(attempts, [False, True])
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                "SELECT management_investment FROM decisions ORDER BY company_id"
            )],
            [17001, 17002],
        )
        self.assertFalse(remote.remote_pending(self.conn, 1))

    def test_settlement_guard_rejects_changed_completed_analysis(self):
        for company_id in (1, 2):
            self.fake_submit(self.conn, 1, target_ids={company_id})
        self.fake_rebalance(self.conn, 1)
        self.conn.commit()
        remote.mark_remote_job_complete(self.conn, 1)
        self.conn.execute("BEGIN IMMEDIATE")
        remote.assert_remote_job_current(self.conn, 1)
        self.conn.rollback()
        self.conn.execute(
            "UPDATE settings SET value='29' WHERE key='cpi_price_power'"
        )
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(remote.RemoteAnalysisChangedError, "重新分析"):
            remote.assert_remote_job_current(self.conn, 1)
        self.conn.rollback()

    def test_worker_requires_token_and_health_is_read_only(self):
        from scf_worker import main_handler
        with patch.dict(os.environ, {"SUPER_BOT_REMOTE_TOKEN": ""}):
            self.assertEqual(main_handler({"body": "{}"}, None)["statusCode"], 503)
            health = main_handler({"httpMethod": "GET"}, None)
            self.assertEqual(json.loads(health["body"])["protocol"], 2)
        with patch.dict(os.environ, {"SUPER_BOT_REMOTE_TOKEN": "correct"}):
            self.assertEqual(main_handler({"body": '{"token":"wrong"}'}, None)["statusCode"], 403)

    def test_worker_roundtrip_returns_only_scoped_decisions(self):
        from scf_worker import main_handler
        payload = remote.create_remote_request(self.conn, 1, company_id=1)
        payload["token"] = "correct"
        with patch.dict(os.environ, {"SUPER_BOT_REMOTE_TOKEN": "correct"}), patch("sim.bots.submit_super_bot_decisions", self.fake_submit):
            response = main_handler({"body": json.dumps(payload)}, None)
        self.assertEqual(response["statusCode"], 200)
        result = json.loads(response["body"])
        self.assertNotIn("db_b64", result)
        self.assertEqual(result["company_ids"], [1])
        self.assertIn("worker_seconds", result["timings"])
        remote.apply_remote_result(self.conn, payload, result)
        self.assertEqual(self.conn.execute("SELECT management_investment FROM decisions WHERE company_id=1").fetchone()[0], 100)

    def test_legacy_wal_snapshot_contains_committed_decisions_on_warm_calls(self):
        from scf_worker import main_handler
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        encoded = base64.b64encode(db.DB_PATH.read_bytes()).decode()
        original_path = db.DB_PATH
        def legacy_submit(conn, round_no, **kwargs):
            conn.execute("INSERT INTO decisions(company_id,round_no,management_investment,submitted_at) VALUES(1,1,100,'legacy') ON CONFLICT(company_id,round_no) DO UPDATE SET management_investment=management_investment+100")
            return 1
        with patch.dict(os.environ, {"SUPER_BOT_REMOTE_TOKEN": "correct"}), patch("sim.bots.submit_super_bot_decisions", legacy_submit):
            for expected in (100, 200):
                response = main_handler({"body": json.dumps({"token": "correct", "db_b64": encoded, "round_no": 1})}, None)
                self.assertEqual(response["statusCode"], 200)
                encoded = json.loads(response["body"])["db_b64"]
                returned = Path(self.directory.name) / f"returned-{expected}.db"
                returned.write_bytes(base64.b64decode(encoded))
                with sqlite3.connect(returned) as check:
                    self.assertEqual(check.execute("SELECT management_investment FROM decisions WHERE company_id=1").fetchone()[0], expected)
                check.close()
        self.assertEqual(db.DB_PATH, original_path)

    def test_cold_worker_import_uses_writable_temp_storage(self):
        # Separate interpreter: in the regular suite sim.db is already loaded,
        # which used to hide its import-time mkdir on SCF's read-only code mount.
        script = """
import errno, os, tempfile
from pathlib import Path
original_mkdir = Path.mkdir
temp_root = Path(tempfile.gettempdir()).resolve()
def readonly_mount_guard(path, *args, **kwargs):
    if not path.resolve().is_relative_to(temp_root):
        raise OSError(errno.EROFS, 'Read-only file system', str(path))
    return original_mkdir(path, *args, **kwargs)
Path.mkdir = readonly_mount_guard
os.environ['SIM_DB_PATH'] = '/var/user/data/sim.db'
import scf_worker
from sim import db
assert db.DB_PATH.resolve().is_relative_to(temp_root), db.DB_PATH
with db.connect() as conn:
    assert conn.execute('SELECT count(*) FROM companies').fetchone()[0] > 0
print('cold bootstrap passed')
"""
        process = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                 text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("cold bootstrap passed", process.stdout)


if __name__ == "__main__":
    unittest.main()
