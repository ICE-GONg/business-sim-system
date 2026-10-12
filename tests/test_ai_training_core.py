from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class AITrainingCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        from sim import db

        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-ai-core-")
        self.addCleanup(self.temp.cleanup)
        self.old_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", self.old_path)
        db.DB_PATH = Path(self.temp.name) / "ai-training.db"
        os.environ["SIM_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        self.db = db

    def test_migration_is_idempotent_and_manual_by_default(self) -> None:
        from sim.ai_training import MANUAL_MODE, get_ai_training_config

        self.db.init_db()
        with self.db.connect() as conn:
            tables = {
                str(row["name"])
                for row in self.db.all_rows(
                    conn,
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )
            }
            self.assertIn("ai_training_runtime", tables)
            config = get_ai_training_config(conn)
            self.assertFalse(config["enabled"])
            self.assertEqual(config["mode"], MANUAL_MODE)
            self.assertEqual(config["actual_player_count"], 4)
            self.assertTrue(config["roster_matches"])

    def test_explicit_roster_rebuild_is_exact_and_bots_are_ready(self) -> None:
        from sim.ai_training import configure_ai_training_roster, get_ai_training_config

        with self.db.connect() as conn:
            config = configure_ai_training_roster(conn, 2, 3, 2, "train-pass")
            self.assertTrue(config["enabled"])
            self.assertTrue(config["auto_enabled"])
            self.assertEqual(self.db.get_setting(conn, "operation_mode", "", str), "ai_training")
            self.assertEqual(config["actual_total"], 7)
            self.assertTrue(config["roster_matches"])
            rows = self.db.all_rows(
                conn,
                "SELECT code,password_hash,is_bot,is_super_bot,home_city,setup_submitted_at "
                "FROM companies ORDER BY id",
            )
            self.assertEqual([row["code"] for row in rows], [
                "C01", "C02", "BOT01", "BOT02", "BOT03", "SBOT01", "SBOT02"
            ])
            self.assertTrue(all(row["home_city"] is None for row in rows[:2]))
            self.assertTrue(all(row["setup_submitted_at"] is None for row in rows[:2]))
            self.assertTrue(all(row["home_city"] for row in rows[2:]))
            self.assertTrue(all(row["setup_submitted_at"] for row in rows[2:]))
            agents = self.db.one(conn, "SELECT COUNT(*) AS n FROM agents")
            self.assertEqual(int(agents["n"]), 5)
            self.assertTrue(self.db.verify_password("train-pass", rows[0]["password_hash"]))

            # A second explicit call replaces the roster instead of appending.
            configure_ai_training_roster(conn, 0, 1, 1)
            exact = get_ai_training_config(conn)
            self.assertEqual(exact["actual_total"], 2)
            self.assertEqual(exact["actual_player_count"], 0)
            self.assertEqual(exact["actual_bot_count"], 1)
            self.assertEqual(exact["actual_super_bot_count"], 1)

    def test_roster_rebuild_refuses_started_competition(self) -> None:
        from sim.ai_training import configure_ai_training_roster

        with self.db.connect() as conn:
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
            with self.assertRaisesRegex(ValueError, "尚未开始"):
                configure_ai_training_roster(conn, 1, 1, 1)

    def test_action_progression_waits_for_players_then_super_bots(self) -> None:
        from sim.ai_training import (
            ACTION_ANALYZE_SUPER_BOTS,
            ACTION_BEGIN_PROCESSING,
            ACTION_FINALIZE_SUPER_BOTS,
            ACTION_SETTLE,
            ACTION_START,
            ACTION_SUBMIT_REGULAR_BOTS,
            ACTION_WAIT,
            ai_training_action,
            begin_ai_training_processing,
            configure_ai_training_roster,
            set_ai_training_enabled,
        )

        with self.db.connect() as conn:
            configure_ai_training_roster(conn, 1, 1, 1)
            set_ai_training_enabled(conn, True)
            self.assertEqual(ai_training_action(conn)["action"], ACTION_WAIT)
            player = self.db.one(conn, "SELECT id FROM companies WHERE is_bot=0")
            conn.execute(
                "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                (self.db.now_iso(), int(player["id"])),
            )
            conn.execute(
                "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)",
                (int(player["id"]),),
            )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_START)
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
            self.assertEqual(ai_training_action(conn)["action"], ACTION_SUBMIT_REGULAR_BOTS)

            normal_bot = self.db.one(
                conn,
                "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=0",
            )
            for company_id in (int(normal_bot["id"]), int(player["id"])):
                conn.execute(
                    "INSERT INTO decisions(company_id,round_no,submitted_at,is_draft) VALUES(?,1,?,0)",
                    (company_id, self.db.now_iso()),
                )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_BEGIN_PROCESSING)
            begin_ai_training_processing(conn, 1)
            self.assertEqual(
                self.db.get_setting(conn, "ai_training_processing_round", 0, int),
                1,
            )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_ANALYZE_SUPER_BOTS)

            super_bot = self.db.one(conn, "SELECT id FROM companies WHERE is_super_bot=1")
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,submitted_at,is_draft) VALUES(?,1,?,1)",
                (int(super_bot["id"]), self.db.now_iso()),
            )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_FINALIZE_SUPER_BOTS)
            conn.execute(
                "UPDATE decisions SET is_draft=0 WHERE company_id=? AND round_no=1",
                (int(super_bot["id"]),),
            )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_SETTLE)

    def test_settled_actions_cover_next_round_and_final_release(self) -> None:
        from sim.ai_training import (
            ACTION_COMPLETE,
            ACTION_OPEN_NEXT_ROUND,
            ACTION_RELEASE_FINAL,
            ai_training_action,
            set_ai_training_enabled,
        )

        with self.db.connect() as conn:
            set_ai_training_enabled(conn, True)
            self.db.set_setting(conn, "total_rounds", 3)
            conn.execute("UPDATE rounds SET status='settled',settled_at=? WHERE round_no=1", ("2020-01-01T00:00:00+00:00",))
            self.assertEqual(ai_training_action(conn)["action"], ACTION_OPEN_NEXT_ROUND)
            conn.execute("DELETE FROM rounds")
            conn.execute(
                "INSERT INTO rounds(round_no,status,settled_at) VALUES(3,'settled',?)",
                ("2020-01-01T00:00:00+00:00",),
            )
            self.assertEqual(ai_training_action(conn)["action"], ACTION_RELEASE_FINAL)
            self.db.set_setting(conn, "final_results_release_round", 3)
            self.assertEqual(ai_training_action(conn)["action"], ACTION_COMPLETE)

    def test_auto_advance_switch_and_transition_delay_are_respected(self) -> None:
        from sim.ai_training import (
            ACTION_NONE,
            ACTION_OPEN_NEXT_ROUND,
            ACTION_WAIT,
            ai_training_action,
            set_ai_training_enabled,
        )

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.db.connect() as conn:
            set_ai_training_enabled(conn, True)
            self.db.set_setting(conn, "total_rounds", 2)
            self.db.set_setting(conn, "ai_training_auto_enabled", 0)
            self.assertEqual(ai_training_action(conn, base)["action"], ACTION_NONE)
            self.db.set_setting(conn, "ai_training_auto_enabled", 1)
            self.db.set_setting(conn, "ai_training_transition_seconds", 5)
            conn.execute(
                "UPDATE rounds SET status='settled',settled_at=? WHERE round_no=1",
                (base.isoformat(),),
            )
            transition = ai_training_action(conn, base + timedelta(seconds=2))
            self.assertEqual(transition["action"], ACTION_WAIT)
            self.assertEqual(transition["reason"], "transition_animation")
            self.assertEqual(
                ai_training_action(conn, base + timedelta(seconds=6))["action"],
                ACTION_OPEN_NEXT_ROUND,
            )
            set_ai_training_enabled(conn, False)
            self.assertEqual(self.db.get_setting(conn, "operation_mode", "", str), "manual")

    def test_lease_is_exclusive_owner_checked_and_can_expire(self) -> None:
        from sim.ai_training import (
            acquire_ai_training_lease,
            release_ai_training_lease,
            set_ai_training_enabled,
        )

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.db.connect() as conn:
            set_ai_training_enabled(conn, True)
        with self.db.connect() as first:
            owner = acquire_ai_training_lease(first, "settle_round", 10, now=base)
            self.assertIsNotNone(owner)
        with self.db.connect() as second:
            self.assertIsNone(acquire_ai_training_lease(second, "settle_round", 10, now=base))
            self.assertFalse(release_ai_training_lease(second, "wrong-owner", now=base))
            replacement = acquire_ai_training_lease(
                second,
                "settle_round",
                10,
                now=base + timedelta(seconds=11),
            )
            self.assertIsNotNone(replacement)
            self.assertTrue(
                release_ai_training_lease(
                    second,
                    replacement,
                    "temporary",
                    5,
                    now=base + timedelta(seconds=11),
                )
            )
            self.assertIsNone(
                acquire_ai_training_lease(
                    second,
                    "settle_round",
                    10,
                    now=base + timedelta(seconds=11),
                )
            )
            self.assertIsNotNone(
                acquire_ai_training_lease(second, "settle_round", 10, now=base + timedelta(seconds=17))
            )

    def test_submission_collection_is_global_and_limited_to_once_per_minute(self) -> None:
        from sim.ai_training import (
            begin_ai_training_processing,
            claim_ai_training_collection,
            clear_ai_training_processing,
            set_ai_training_enabled,
        )

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.db.connect() as conn:
            set_ai_training_enabled(conn, True)
            self.assertTrue(claim_ai_training_collection(conn, 60, now=base))
        with self.db.connect() as conn:
            self.assertFalse(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=59)
                )
            )
            self.assertTrue(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=60)
                )
            )
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
            begin_ai_training_processing(conn, 1)
            self.assertFalse(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=120)
                )
            )
            self.assertTrue(clear_ai_training_processing(conn, 1))
            self.assertTrue(
                claim_ai_training_collection(
                    conn, 60, now=base + timedelta(seconds=121)
                )
            )

    def test_open_next_round_is_idempotent_and_bonus_paid_once(self) -> None:
        with self.db.connect() as conn:
            company = self.db.one(conn, "SELECT id,cash FROM companies ORDER BY id LIMIT 1")
            company_id = int(company["id"])
            initial_cash = float(company["cash"])
            conn.execute("UPDATE rounds SET status='settled',settled_at=? WHERE round_no=1", (self.db.now_iso(),))
            self.assertEqual(self.db.open_next_round(conn, 1, 15, {company_id: 123_456}), 2)
            self.assertEqual(self.db.open_next_round(conn, 1, 15, {company_id: 999_999}), 2)
            after = self.db.one(conn, "SELECT cash FROM companies WHERE id=?", (company_id,))
            self.assertEqual(float(after["cash"]), initial_cash + 123_456)
            bonuses = self.db.all_rows(
                conn,
                "SELECT amount FROM round_bonuses WHERE company_id=? AND round_no=2",
                (company_id,),
            )
            self.assertEqual(len(bonuses), 1)
            self.assertEqual(float(bonuses[0]["amount"]), 123_456)


if __name__ == "__main__":
    unittest.main()
