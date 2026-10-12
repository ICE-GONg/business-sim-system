from __future__ import annotations

import ast
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class AITrainingEndToEndTests(unittest.TestCase):
    """Smoke the durable AI controller without importing Streamlit itself."""

    def setUp(self) -> None:
        from sim import db

        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-ai-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.old_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", self.old_path)
        db.DB_PATH = Path(self.temp.name) / "ai-e2e.db"
        old_env_path = os.environ.get("SIM_DB_PATH")

        def restore_environment() -> None:
            if old_env_path is None:
                os.environ.pop("SIM_DB_PATH", None)
            else:
                os.environ["SIM_DB_PATH"] = old_env_path

        self.addCleanup(restore_environment)
        os.environ["SIM_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        self.db = db
        self.run_once = self._load_controller()

    def _load_controller(self):
        """Extract the controller so a damaged local Streamlit install is irrelevant."""
        from sim import ai_training
        from sim.bots import finalize_super_bot_decisions, submit_bot_decisions
        from sim.engine import settle_round

        source = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text())
        function = next(
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_ai_training_once"
        )

        def mock_super_analysis(
            round_no: int,
            replace_existing: bool = False,
            progress_callback=None,
        ) -> bool:
            del replace_existing
            progress_rows: list[tuple[int, int, str]] = []
            with self.db.connect() as conn:
                markets = self.db.all_rows(
                    conn,
                    "SELECT city,max_price FROM market_config ORDER BY city",
                )
                supers = self.db.all_rows(
                    conn,
                    "SELECT id,home_city,code FROM companies WHERE is_super_bot=1 ORDER BY id",
                )
                for index, company in enumerate(supers, 1):
                    home = self.db.one(
                        conn,
                        "SELECT worker_initial_salary,engineer_initial_salary "
                        "FROM market_config WHERE city=?",
                        (company["home_city"],),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO decisions("
                        "company_id,round_no,worker_salary,engineer_salary,submitted_at,is_draft"
                        ") VALUES(?,?,?,?,?,1)",
                        (
                            company["id"],
                            round_no,
                            home["worker_initial_salary"],
                            home["engineer_initial_salary"],
                            self.db.now_iso(),
                        ),
                    )
                    for market in markets:
                        conn.execute(
                            "INSERT OR REPLACE INTO city_decisions("
                            "company_id,round_no,city,price"
                            ") VALUES(?,?,?,?)",
                            (company["id"], round_no, market["city"], market["max_price"]),
                        )
                    progress_rows.append((index, len(supers), str(company["code"])))
            # The production submitter releases its write transaction before
            # progress renews the orchestration lease. Mirror that ordering.
            if progress_callback:
                for progress_row in progress_rows:
                    progress_callback(*progress_row)
            return True

        namespace = {
            name: getattr(ai_training, name)
            for name in ai_training.__all__
        }
        namespace.update(
            {
                "Any": Any,
                "connect": self.db.connect,
                "current_round": self.db.current_round,
                "get_setting": self.db.get_setting,
                "set_setting": self.db.set_setting,
                "one": self.db.one,
                "start_competition": self.db.start_competition,
                "prepare_first_round_after_test": self.db.prepare_first_round_after_test,
                "open_next_round": self.db.open_next_round,
                "submit_bot_decisions": submit_bot_decisions,
                "finalize_super_bot_decisions": finalize_super_bot_decisions,
                "settle_round": settle_round,
                "remote_pending": lambda *_args, **_kwargs: False,
                "assert_remote_job_current": lambda *_args, **_kwargs: None,
                "_remote_super_bot_submit": mock_super_analysis,
                "submit_local_super_bots": lambda *_args, **_kwargs: None,
                "LOGGER": logging.getLogger("test-ai-training-e2e"),
            }
        )
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"),
            namespace,
        )
        return namespace["run_ai_training_once"]

    def _ready_humans(self) -> list[int]:
        with self.db.connect() as conn:
            ids = []
            for row in self.db.all_rows(
                conn,
                "SELECT id FROM companies WHERE is_bot=0 ORDER BY id",
            ):
                company_id = int(row["id"])
                ids.append(company_id)
                conn.execute(
                    "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                    (self.db.now_iso(), company_id),
                )
                conn.execute(
                    "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)",
                    (company_id,),
                )
            return ids

    def _submit_human(self, company_id: int, round_no: int) -> None:
        with self.db.connect() as conn:
            home = self.db.one(
                conn,
                "SELECT worker_initial_salary,engineer_initial_salary "
                "FROM market_config WHERE city='广州'",
            )
            conn.execute(
                "INSERT INTO decisions("
                "company_id,round_no,worker_salary,engineer_salary,submitted_at,is_draft"
                ") VALUES(?,?,?,?,?,0)",
                (
                    company_id,
                    round_no,
                    home["worker_initial_salary"],
                    home["engineer_initial_salary"],
                    self.db.now_iso(),
                ),
            )
            for market in self.db.all_rows(
                conn,
                "SELECT city,max_price FROM market_config ORDER BY city",
            ):
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,price) "
                    "VALUES(?,?,?,?)",
                    (company_id, round_no, market["city"], market["max_price"]),
                )

    def _skip_animation_delay(self) -> None:
        with self.db.connect() as conn:
            latest = self.db.current_round(conn)
            if latest is not None and str(latest["status"]) == "settled":
                conn.execute(
                    "UPDATE rounds SET settled_at='2020-01-01T00:00:00+00:00' "
                    "WHERE round_no=?",
                    (int(latest["round_no"]),),
                )

    def test_all_bot_training_runs_two_rounds_and_releases_final_results(self) -> None:
        from sim.ai_training import ACTION_COMPLETE, configure_ai_training_roster

        with self.db.connect() as conn:
            config = configure_ai_training_roster(conn, 0, 2, 2)
            self.assertEqual(config["actual_player_count"], 0)
            self.assertEqual(config["actual_bot_count"], 2)
            self.assertEqual(config["actual_super_bot_count"], 2)
            self.assertTrue(config["roster_matches"])
            self.db.set_setting(conn, "total_rounds", 2)

        actions = []
        for _ in range(20):
            outcome = self.run_once()
            self.assertNotIn("error", outcome, outcome)
            actions.append(str(outcome["action"]))
            self._skip_animation_delay()
            if outcome["action"] == ACTION_COMPLETE:
                break

        self.assertEqual(actions[-1], ACTION_COMPLETE)
        self.assertIn("open_next_round", actions)
        self.assertEqual(actions.count("begin_processing"), 2)
        self.assertEqual(actions.count("settle_round"), 2)
        with self.db.connect() as conn:
            latest = self.db.current_round(conn)
            self.assertEqual((int(latest["round_no"]), str(latest["status"])), (2, "settled"))
            self.assertEqual(self.db.get_setting(conn, "final_results_release_round", 0, int), 2)
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                0,
            )
            self.assertEqual(
                int(self.db.one(conn, "SELECT COUNT(*) AS n FROM results")["n"]),
                8,
            )

    def test_mixed_training_waits_for_every_human_then_settles_and_opens_next(self) -> None:
        from sim.ai_training import configure_ai_training_roster

        with self.db.connect() as conn:
            config = configure_ai_training_roster(conn, 2, 1, 0, "players")
            self.assertEqual(config["actual_total"], 3)
            self.assertTrue(config["roster_matches"])
            self.db.set_setting(conn, "total_rounds", 2)
        player_ids = self._ready_humans()

        started = self.run_once()
        self.assertEqual(started["action"], "start_competition")
        waiting = self.run_once()
        self.assertEqual((waiting["action"], waiting["reason"]), ("wait", "players_not_submitted"))

        self._submit_human(player_ids[0], 1)
        waiting = self.run_once()
        self.assertEqual((waiting["action"], waiting["reason"]), ("wait", "players_not_submitted"))

        self._submit_human(player_ids[1], 1)
        processing = self.run_once()
        self.assertEqual(processing["action"], "begin_processing")
        self.assertNotIn("error", processing, processing)
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                1,
            )
            self.assertTrue(
                self.db.get_setting(
                    conn, "ai_training_processing_started_at", "", str
                )
            )

        settled = self.run_once()
        self.assertEqual(settled["action"], "settle_round")
        self.assertNotIn("error", settled, settled)
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                1,
            )
        self._skip_animation_delay()
        opened = self.run_once()
        self.assertEqual(opened["action"], "open_next_round")

        with self.db.connect() as conn:
            latest = self.db.current_round(conn)
            self.assertEqual((int(latest["round_no"]), str(latest["status"])), (2, "open"))
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                0,
            )
            submitted = self.db.one(
                conn,
                "SELECT "
                "SUM(CASE WHEN c.is_bot=1 AND d.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS bots,"
                "SUM(CASE WHEN c.is_bot=0 AND d.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS humans "
                "FROM companies c LEFT JOIN decisions d "
                "ON d.company_id=c.id AND d.round_no=2",
            )
            self.assertEqual(int(submitted["bots"] or 0), 1)
            self.assertEqual(int(submitted["humans"] or 0), 0)

    def test_minute_collection_sets_lock_before_super_analysis(self) -> None:
        from sim.ai_training import (
            claim_ai_training_collection,
            configure_ai_training_roster,
        )

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.db.connect() as conn:
            configure_ai_training_roster(conn, 1, 0, 1, "players")
            self.db.set_setting(conn, "total_rounds", 2)
        player_id = self._ready_humans()[0]

        with self.db.connect() as conn:
            self.assertTrue(claim_ai_training_collection(conn, 60, now=base))
        self.assertEqual(self.run_once()["action"], "start_competition")

        # Opening a new round deliberately allows one immediate initial scan;
        # that scan establishes the minute cadence for later submissions.
        with self.db.connect() as conn:
            self.assertTrue(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=1)
                )
            )
        initial_wait = self.run_once()
        self.assertEqual(
            (initial_wait["action"], initial_wait["reason"]),
            ("wait", "players_not_submitted"),
        )
        self._submit_human(player_id, 1)

        with self.db.connect() as conn:
            self.assertFalse(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=59)
                )
            )
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                0,
            )
            self.assertEqual(
                int(self.db.one(
                    conn,
                    "SELECT COUNT(*) AS n FROM decisions d JOIN companies c "
                    "ON c.id=d.company_id WHERE c.is_super_bot=1 AND d.round_no=1",
                )["n"]),
                0,
            )

        with self.db.connect() as conn:
            self.assertTrue(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=61)
                )
            )
        self.assertEqual(self.run_once()["action"], "begin_processing")
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                1,
            )
            self.assertFalse(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=121)
                )
            )
            self.assertEqual(
                int(self.db.one(
                    conn,
                    "SELECT COUNT(*) AS n FROM decisions d JOIN companies c "
                    "ON c.id=d.company_id WHERE c.is_super_bot=1 AND d.round_no=1",
                )["n"]),
                0,
            )

        self.assertEqual(self.run_once()["action"], "analyze_super_bots")
        self.assertEqual(self.run_once()["action"], "finalize_super_bots")
        self.assertEqual(self.run_once()["action"], "settle_round")
        self._skip_animation_delay()
        self.assertEqual(self.run_once()["action"], "open_next_round")
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                0,
            )
            self.assertEqual(
                (int(self.db.current_round(conn)["round_no"]), self.db.current_round(conn)["status"]),
                (2, "open"),
            )


if __name__ == "__main__":
    unittest.main()
