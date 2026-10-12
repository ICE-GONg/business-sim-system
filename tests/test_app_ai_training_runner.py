from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AITrainingRunnerTests(unittest.TestCase):
    """Integration coverage for the one-action-per-heartbeat AI controller."""

    @classmethod
    def setUpClass(cls) -> None:
        # Importing the Streamlit module does not execute main(), but gives the
        # tests the exact controller called by the one-second fragment.
        cls.app = importlib.import_module("app")

    def setUp(self) -> None:
        from sim import db

        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-ai-runner-")
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"SIM_DB_PATH": str(Path(self.temp.name) / "ai-runner.db")},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.old_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", self.old_path)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()
        self.db = db

    def _configure_ready_roster(self, *, rounds: int = 2) -> tuple[int, int, int]:
        from sim.ai_training import configure_ai_training_roster

        with self.db.connect() as conn:
            configure_ai_training_roster(conn, 1, 1, 1, "train-pass")
            self.db.set_setting(conn, "total_rounds", rounds)
            self.db.set_setting(conn, "ai_training_transition_seconds", 3)
            player = self.db.one(conn, "SELECT id FROM companies WHERE is_bot=0")
            bot = self.db.one(
                conn,
                "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=0",
            )
            super_bot = self.db.one(conn, "SELECT id FROM companies WHERE is_super_bot=1")
            player_id = int(player["id"])
            conn.execute(
                "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                (self.db.now_iso(), player_id),
            )
            conn.execute(
                "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)",
                (player_id,),
            )
        return player_id, int(bot["id"]), int(super_bot["id"])

    def _save_human_submission(self, company_id: int, round_no: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,"
                "submitted_at,is_draft) VALUES(?,?,3300,6400,?,0)",
                (company_id, round_no, self.db.now_iso()),
            )
            for market in self.db.all_rows(conn, "SELECT city,max_price FROM market_config"):
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,?,?,?)",
                    (company_id, round_no, str(market["city"]), float(market["max_price"])),
                )

    def _fake_submit_regular_bots(self, conn, round_no: int) -> int:
        rows = self.db.all_rows(
            conn,
            "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=0 ORDER BY id",
        )
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO decisions(company_id,round_no,worker_salary,"
                "engineer_salary,submitted_at,is_draft) VALUES(?,?,3300,6400,?,0)",
                (int(row["id"]), int(round_no), self.db.now_iso()),
            )
        return len(rows)

    def _fake_analyse_super_bots(
        self,
        conn,
        round_no: int,
        progress_callback=None,
        *,
        replace_existing: bool = False,
    ) -> int:
        rows = self.db.all_rows(
            conn,
            "SELECT id,code FROM companies WHERE is_super_bot=1 ORDER BY id",
        )
        markets = self.db.all_rows(conn, "SELECT city,max_price FROM market_config ORDER BY city")
        for index, row in enumerate(rows, 1):
            company_id = int(row["id"])
            conn.execute(
                "INSERT OR REPLACE INTO decisions(company_id,round_no,worker_salary,"
                "engineer_salary,submitted_at,is_draft) VALUES(?,?,3300,6400,?,1)",
                (company_id, int(round_no), self.db.now_iso()),
            )
            for market in markets:
                conn.execute(
                    "INSERT OR REPLACE INTO city_decisions(company_id,round_no,city,price) "
                    "VALUES(?,?,?,?)",
                    (
                        company_id,
                        int(round_no),
                        str(market["city"]),
                        float(market["max_price"]),
                    ),
                )
            if progress_callback:
                # Production Super Bot analysis checkpoints each Bot before
                # invoking progress, allowing the callback to renew its lease
                # through a second SQLite connection without writer contention.
                conn.commit()
                progress_callback(index, len(rows), str(row["code"]))
        return len(rows)

    def _fake_settle(self, conn, round_no: int) -> None:
        conn.execute(
            "UPDATE rounds SET status='settled',settled_at=? WHERE round_no=?",
            (self.db.now_iso(), int(round_no)),
        )

    def test_last_human_submission_drives_full_rounds_and_final_release(self) -> None:
        """The minute scan detects the final submit; no admin refresh is required."""
        from sim.ai_training import (
            ACTION_ANALYZE_SUPER_BOTS,
            ACTION_BEGIN_PROCESSING,
            ACTION_COMPLETE,
            ACTION_FINALIZE_SUPER_BOTS,
            ACTION_OPEN_NEXT_ROUND,
            ACTION_RELEASE_FINAL,
            ACTION_SETTLE,
            ACTION_START,
            ACTION_WAIT,
        )

        player_id, _bot_id, super_id = self._configure_ready_roster(rounds=2)
        progress: list[tuple[int, int, str]] = []
        with patch.object(
            self.app, "submit_bot_decisions", side_effect=self._fake_submit_regular_bots,
        ), patch.object(
            self.app, "_remote_super_bot_submit", return_value=False,
        ), patch.object(
            self.app, "submit_local_super_bots", side_effect=self._fake_analyse_super_bots,
        ), patch.object(
            self.app, "assert_remote_job_current",
        ), patch.object(
            self.app, "settle_round", side_effect=self._fake_settle,
        ), patch.object(
            self.app, "remote_pending", return_value=False,
        ):
            started = self.app.run_ai_training_once(
                lambda done, total, code: progress.append((done, total, code))
            )
            self.assertEqual(started["action"], ACTION_START)
            self.assertTrue(started["changed"])
            with self.db.connect() as conn:
                self.assertEqual(self.db.current_round(conn)["status"], "open")
                submitted = self.db.one(
                    conn,
                    "SELECT COUNT(*) AS n FROM decisions d JOIN companies c ON c.id=d.company_id "
                    "WHERE c.is_bot=1 AND c.is_super_bot=0 AND d.round_no=1 "
                    "AND d.submitted_at IS NOT NULL AND d.is_draft=0",
                )
                self.assertEqual(int(submitted["n"]), 1)

            # Before the human submits, the heartbeat is intentionally idle.
            waiting = self.app.run_ai_training_once()
            self.assertEqual(waiting["action"], ACTION_WAIT)
            self.assertEqual(waiting["reason"], "players_not_submitted")
            self.assertFalse(waiting["changed"])

            # The minute collector first persists the lock. The End can now
            # render immediately, before any Super Bot analysis or result is published.
            self._save_human_submission(player_id, 1)
            begin = self.app.run_ai_training_once()
            self.assertEqual(begin["action"], ACTION_BEGIN_PROCESSING)
            self.assertTrue(begin["changed"])
            with self.db.connect() as conn:
                self.assertEqual(
                    self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                    1,
                )
                self.assertEqual(self.db.current_round(conn)["status"], "open")
                self.assertEqual(
                    int(self.db.one(
                        conn,
                        "SELECT COUNT(*) AS n FROM decisions WHERE company_id=? AND round_no=1",
                        (super_id,),
                    )["n"]),
                    0,
                )

            # Only the one-second processing heartbeat performs the expensive work.
            analysed = self.app.run_ai_training_once(
                lambda done, total, code: progress.append((done, total, code))
            )
            self.assertEqual(analysed["action"], ACTION_ANALYZE_SUPER_BOTS)
            self.assertTrue(analysed["changed"])
            with self.db.connect() as conn:
                draft = self.db.one(
                    conn,
                    "SELECT submitted_at,is_draft FROM decisions WHERE company_id=? AND round_no=1",
                    (super_id,),
                )
                self.assertIsNotNone(draft["submitted_at"])
                self.assertEqual(int(draft["is_draft"]), 1)

            finalised = self.app.run_ai_training_once()
            self.assertEqual(finalised["action"], ACTION_FINALIZE_SUPER_BOTS)
            self.assertTrue(finalised["changed"])
            with self.db.connect() as conn:
                self.assertEqual(
                    int(self.db.one(
                        conn,
                        "SELECT is_draft FROM decisions WHERE company_id=? AND round_no=1",
                        (super_id,),
                    )["is_draft"]),
                    0,
                )
                self.assertEqual(self.db.current_round(conn)["status"], "open")
                self.assertEqual(
                    self.db.get_setting(conn, "final_results_release_round", 0, int),
                    0,
                )

            settled = self.app.run_ai_training_once()
            self.assertEqual(settled["action"], ACTION_SETTLE)
            self.assertTrue(settled["changed"])
            with self.db.connect() as conn:
                self.assertEqual(self.db.current_round(conn)["status"], "settled")

            # Keep the settled screen mounted long enough for the The End overlay.
            transition = self.app.run_ai_training_once()
            self.assertEqual(transition["action"], ACTION_WAIT)
            self.assertEqual(transition["reason"], "transition_animation")
            self.assertFalse(transition["changed"])
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rounds SET settled_at='2020-01-01T00:00:00+00:00' WHERE round_no=1"
                )

            opened = self.app.run_ai_training_once()
            self.assertEqual(opened["action"], ACTION_OPEN_NEXT_ROUND)
            self.assertTrue(opened["changed"])
            with self.db.connect() as conn:
                current = self.db.current_round(conn)
                self.assertEqual((int(current["round_no"]), current["status"]), (2, "open"))
                normal_bot = self.db.one(
                    conn,
                    "SELECT COUNT(*) AS n FROM decisions d JOIN companies c ON c.id=d.company_id "
                    "WHERE c.is_bot=1 AND c.is_super_bot=0 AND d.round_no=2 "
                    "AND d.submitted_at IS NOT NULL AND d.is_draft=0",
                )
                self.assertEqual(int(normal_bot["n"]), 1)
                self.assertEqual(
                    self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                    0,
                )

            # The same pipeline runs in round two and ends in an explicit final release.
            self._save_human_submission(player_id, 2)
            self.assertEqual(self.app.run_ai_training_once()["action"], ACTION_BEGIN_PROCESSING)
            self.assertEqual(self.app.run_ai_training_once()["action"], ACTION_ANALYZE_SUPER_BOTS)
            self.assertEqual(self.app.run_ai_training_once()["action"], ACTION_FINALIZE_SUPER_BOTS)
            self.assertEqual(self.app.run_ai_training_once()["action"], ACTION_SETTLE)
            self.assertEqual(self.app.run_ai_training_once()["action"], ACTION_WAIT)
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE rounds SET settled_at='2020-01-01T00:00:00+00:00' WHERE round_no=2"
                )
            released = self.app.run_ai_training_once()
            self.assertEqual(released["action"], ACTION_RELEASE_FINAL)
            self.assertTrue(released["changed"])
            with self.db.connect() as conn:
                self.assertEqual(
                    self.db.get_setting(conn, "final_results_release_round", 0, int),
                    2,
                )
                self.assertEqual(
                    self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                    0,
                )
                self.assertEqual(self.db.current_round(conn)["status"], "settled")
            complete = self.app.run_ai_training_once()
            self.assertEqual(complete["action"], ACTION_COMPLETE)
            self.assertFalse(complete["changed"])

        self.assertTrue(progress)

    def test_active_lease_prevents_a_second_runner_from_mutating_state(self) -> None:
        from sim.ai_training import acquire_ai_training_lease, release_ai_training_lease

        self._configure_ready_roster(rounds=1)
        with self.db.connect() as conn:
            owner = acquire_ai_training_lease(conn, "another_browser", ttl_seconds=60)
        self.assertIsNotNone(owner)
        with patch.object(
            self.app, "start_competition", side_effect=AssertionError("must not start twice"),
        ):
            result = self.app.run_ai_training_once()
        self.assertEqual(result["action"], "wait")
        self.assertEqual(result["reason"], "lease_active")
        self.assertFalse(result["changed"])
        with self.db.connect() as conn:
            self.assertEqual(self.db.current_round(conn)["status"], "waiting")
            self.assertTrue(release_ai_training_lease(conn, owner))

    def test_plan_is_revalidated_after_lease_acquisition(self) -> None:
        """A fast competing page cannot make this page repeat a stale action."""
        from sim.ai_training import acquire_ai_training_lease

        self._configure_ready_roster(rounds=1)
        advanced = False

        def acquire_after_other_page(conn, phase: str, ttl_seconds: int = 1800):
            nonlocal advanced
            if not advanced:
                # Simulate another browser completing the previously planned
                # waiting -> open transition just before this one owns a lease.
                self.db.start_competition(conn, 30, False)
                advanced = True
            return acquire_ai_training_lease(conn, phase, ttl_seconds)

        with patch.object(
            self.app,
            "acquire_ai_training_lease",
            side_effect=acquire_after_other_page,
        ), patch.object(
            self.app,
            "start_competition",
            side_effect=AssertionError("stale start action must not execute"),
        ):
            result = self.app.run_ai_training_once()

        self.assertEqual(result["action"], "wait")
        self.assertEqual(result["reason"], "stale_plan")
        self.assertFalse(result["changed"])
        with self.db.connect() as conn:
            self.assertEqual(self.db.current_round(conn)["status"], "open")
            runtime = self.db.one(conn, "SELECT * FROM ai_training_runtime WHERE id=1")
            self.assertIsNone(runtime["lease_owner"])
            self.assertIsNone(runtime["last_error"])
            self.assertIsNone(runtime["retry_not_before"])

    def test_dual_heartbeat_collects_slowly_then_processes_every_tick(self) -> None:
        """The collector owns scans; the fast heartbeat sleeps until The End."""
        collection_body = self.app.watch_ai_training_collection.__wrapped__
        processing_body = self.app.watch_ai_training_processing.__wrapped__

        with patch.object(
            self.app, "claim_ai_training_collection", return_value=False,
        ) as claim, patch.object(self.app, "_render_ai_training_tick") as tick:
            collection_body()
        claim.assert_called_once()
        tick.assert_not_called()

        with patch.object(
            self.app, "claim_ai_training_collection", return_value=True,
        ), patch.object(
            self.app, "_render_ai_training_tick", return_value={"changed": False},
        ) as tick:
            collection_body()
        tick.assert_called_once_with()

        # No processing marker means the one-second heartbeat performs only a
        # cheap marker read and never scans submissions or runs Bot analysis.
        with patch.object(self.app, "_render_ai_training_tick") as tick:
            processing_body()
        tick.assert_not_called()

        with self.db.connect() as conn:
            self.db.set_setting(conn, "ai_training_processing_round", 1)
        with patch.object(
            self.app, "_render_ai_training_tick", return_value={"changed": False},
        ) as tick:
            processing_body()
        tick.assert_called_once_with()

        with patch.object(self.app, "watch_ai_training_collection") as collect, patch.object(
            self.app, "watch_ai_training_processing",
        ) as process:
            self.app.watch_ai_training_automation()
        collect.assert_called_once_with()
        process.assert_called_once_with()

    def test_processing_marker_renders_the_end_before_settlement(self) -> None:
        self._configure_ready_roster(rounds=1)
        with self.db.connect() as conn:
            self.db.start_competition(conn, 30, False)
            round_row = self.db.current_round(conn)
        with patch.object(self.app.st, "markdown") as markdown:
            locked = self.app.show_ai_processing_lock(round_row, 1)
        self.assertTrue(locked)
        self.assertIn("The End", markdown.call_args.args[0])
        self.assertIn("Round 1", markdown.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
