from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DisruptorBotTests(unittest.TestCase):
    """Contract tests for the hidden, Super-Bot-powered disruptor role."""

    def setUp(self) -> None:
        from sim import db

        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-disruptor-")
        self.addCleanup(self.temp.cleanup)
        self.old_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", self.old_path)
        self.old_env_path = os.environ.get("SIM_DB_PATH")

        def restore_environment() -> None:
            if self.old_env_path is None:
                os.environ.pop("SIM_DB_PATH", None)
            else:
                os.environ["SIM_DB_PATH"] = self.old_env_path

        self.addCleanup(restore_environment)
        db.DB_PATH = Path(self.temp.name) / "disruptor.db"
        os.environ["SIM_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        self.db = db

    def test_migration_adds_disruptor_role_and_runtime_count_idempotently(self) -> None:
        """Existing databases upgrade with the new role disabled by default."""
        from sim.ai_training import get_ai_training_config

        self.db.init_db()
        with self.db.connect() as conn:
            company_columns = {
                str(row["name"])
                for row in self.db.all_rows(conn, "PRAGMA table_info(companies)")
            }
            runtime_columns = {
                str(row["name"])
                for row in self.db.all_rows(
                    conn, "PRAGMA table_info(ai_training_runtime)"
                )
            }
            self.assertIn("is_disruptor_bot", company_columns)
            self.assertIn("disruptor_bot_count", runtime_columns)
            config = get_ai_training_config(conn)
            self.assertEqual(config["disruptor_bot_count"], 0)
            self.assertEqual(config["actual_disruptor_bot_count"], 0)
            self.assertTrue(config["roster_matches"])

    def test_exact_roster_counts_disruptors_separately_but_names_them_like_bots(
        self,
    ) -> None:
        """The admin can count the role, while player-visible identity stays generic."""
        from sim.ai_training import configure_ai_training_roster

        with self.db.connect() as conn:
            config = configure_ai_training_roster(
                conn,
                2,
                2,
                2,
                "train-pass",
                disruptor_bot_count=2,
            )
            self.assertEqual(config["actual_total"], 8)
            self.assertEqual(config["actual_player_count"], 2)
            self.assertEqual(config["actual_bot_count"], 2)
            self.assertEqual(config["actual_super_bot_count"], 2)
            self.assertEqual(config["actual_disruptor_bot_count"], 2)
            self.assertTrue(config["roster_matches"])

            disruptors = self.db.all_rows(
                conn,
                "SELECT code,name,is_bot,is_super_bot,is_disruptor_bot,"
                "home_city,setup_submitted_at FROM companies "
                "WHERE is_disruptor_bot=1 ORDER BY id",
            )
            self.assertEqual(len(disruptors), 2)
            self.assertTrue(all(int(row["is_bot"]) == 1 for row in disruptors))
            self.assertTrue(all(int(row["is_super_bot"]) == 1 for row in disruptors))
            self.assertTrue(all(row["home_city"] for row in disruptors))
            self.assertTrue(all(row["setup_submitted_at"] for row in disruptors))

            # A player must not be able to infer the hidden role from either
            # public team identifier.  Internally the flag remains available
            # to the administrator and analysis controller.
            forbidden = ("disrupt", "kamikaze", "自曝", "爆", "sbot", "dbot")
            for row in disruptors:
                code = str(row["code"])
                name = str(row["name"])
                self.assertRegex(code, r"^BOT\d+$")
                self.assertRegex(name, r"^Bot \d+$")
                public_identity = f"{code} {name}".lower()
                self.assertFalse(
                    any(token in public_identity for token in forbidden),
                    public_identity,
                )

            all_codes = [
                str(row["code"])
                for row in self.db.all_rows(
                    conn, "SELECT code FROM companies ORDER BY id"
                )
            ]
            self.assertEqual(len(all_codes), len(set(all_codes)))

    def test_analysis_order_is_super_first_early_and_disruptor_first_late(
        self,
    ) -> None:
        """The second group can always react to the first group's saved drafts."""
        from sim.ai_training import (
            configure_ai_training_roster,
            super_bot_analysis_order,
        )

        with self.db.connect() as conn:
            configure_ai_training_roster(
                conn,
                0,
                0,
                2,
                disruptor_bot_count=2,
            )
            self.db.set_setting(conn, "total_rounds", 5)
            standard_ids = {
                int(row["id"])
                for row in self.db.all_rows(
                    conn,
                    "SELECT id FROM companies WHERE is_super_bot=1 "
                    "AND is_disruptor_bot=0",
                )
            }
            disruptor_ids = {
                int(row["id"])
                for row in self.db.all_rows(
                    conn,
                    "SELECT id FROM companies WHERE is_disruptor_bot=1",
                )
            }

            for round_no in (-1, 1, 2, 3):
                order = list(super_bot_analysis_order(conn, round_no))
                self.assertEqual(set(order[:2]), standard_ids, (round_no, order))
                self.assertEqual(set(order[2:]), disruptor_ids, (round_no, order))
                # Repeated calls must be stable so a resumed remote/local job
                # cannot silently change which model gets to observe which.
                self.assertEqual(order, list(super_bot_analysis_order(conn, round_no)))

            for round_no in (4, 5):
                order = list(super_bot_analysis_order(conn, round_no))
                self.assertEqual(set(order[:2]), disruptor_ids, (round_no, order))
                self.assertEqual(set(order[2:]), standard_ids, (round_no, order))
                self.assertEqual(order, list(super_bot_analysis_order(conn, round_no)))

    def test_early_selector_holds_near_seventy_percent_of_leader_assets(self) -> None:
        """Before the final two rounds, survival/shadowing beats runaway growth."""
        from sim.bots import _select_disruptor_early_candidate

        candidates = [
            {
                "label": "under",
                "ending_assets": 55_000_000.0,
                "predicted_profit": 3_000_000.0,
                "risk_profit": 2_000_000.0,
            },
            {
                "label": "shadow",
                "ending_assets": 69_000_000.0,
                "predicted_profit": 7_000_000.0,
                "risk_profit": 5_000_000.0,
            },
            {
                "label": "runaway",
                "ending_assets": 83_000_000.0,
                "predicted_profit": 21_000_000.0,
                "risk_profit": 18_000_000.0,
            },
        ]
        selected = _select_disruptor_early_candidate(
            candidates,
            leader_assets=100_000_000.0,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["label"], "shadow")
        self.assertLessEqual(
            abs(float(selected["ending_assets"]) / 100_000_000.0 - 0.70),
            0.05,
        )

    def test_late_attack_selector_prioritizes_player_and_secondary_denial(self) -> None:
        """Late self-destruction values human CPI and redistribution suppression."""
        from sim.bots import _select_disruptor_attack_candidate

        candidates = [
            {
                "label": "passive-profit",
                "predicted_profit": 50_000_000.0,
                "risk_profit": 45_000_000.0,
                "target_tier": 0,
                "target_is_human": False,
                "target_cpi_drop": 0.0,
                "target_secondary_denied": 0.0,
            },
            {
                "label": "hit-super",
                "predicted_profit": 5_000_000.0,
                "risk_profit": 0.0,
                "target_tier": 4,
                "target_is_human": False,
                "target_cpi_drop": 14.0,
                "target_secondary_denied": 8_000.0,
            },
            {
                "label": "hit-player-primary-only",
                "predicted_profit": -10_000_000.0,
                "risk_profit": -15_000_000.0,
                "target_tier": 5,
                "target_is_human": True,
                "target_cpi_drop": 10.0,
                "target_secondary_denied": 500.0,
            },
            {
                "label": "hit-player-both-pools",
                "predicted_profit": -30_000_000.0,
                "risk_profit": -40_000_000.0,
                "target_tier": 5,
                "target_is_human": True,
                "target_cpi_drop": 10.0,
                "target_secondary_denied": 12_000.0,
            },
        ]
        selected = _select_disruptor_attack_candidate(candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["label"], "hit-player-both-pools")
        self.assertTrue(selected["target_is_human"])
        self.assertGreater(float(selected["target_cpi_drop"]), 0.0)
        self.assertGreater(float(selected["target_secondary_denied"]), 0.0)

    def test_late_attack_selector_ignores_own_profit_when_pressure_is_stronger(
        self,
    ) -> None:
        """Flipping enormous own profits cannot overturn a stronger attack."""
        from sim.bots import _select_disruptor_attack_candidate

        stronger_pressure = {
            "label": "stronger-pressure",
            "target_tier": 5,
            "target_is_human": True,
            "target_cpi_drop": 10.01,
            "target_secondary_denied": 20_001.0,
            "predicted_profit": -9_000_000_000.0,
            "risk_profit": -9_500_000_000.0,
        }
        weaker_pressure = {
            "label": "weaker-pressure",
            "target_tier": 5,
            "target_is_human": True,
            "target_cpi_drop": 10.0,
            "target_secondary_denied": 20_000.0,
            "predicted_profit": 9_000_000_000.0,
            "risk_profit": 8_500_000_000.0,
        }
        selected = _select_disruptor_attack_candidate(
            [weaker_pressure, stronger_pressure]
        )
        self.assertEqual(selected["label"], "stronger-pressure")

        # Prove profit is not merely given a small weight: swap both profit
        # extremes while leaving the attack forecast untouched.  Selection
        # must remain identical because a late disruptor ranks pressure first.
        stronger_pressure = {
            **stronger_pressure,
            "predicted_profit": 9_000_000_000.0,
            "risk_profit": 8_500_000_000.0,
        }
        weaker_pressure = {
            **weaker_pressure,
            "predicted_profit": -9_000_000_000.0,
            "risk_profit": -9_500_000_000.0,
        }
        selected_after_profit_swap = _select_disruptor_attack_candidate(
            [weaker_pressure, stronger_pressure]
        )
        self.assertEqual(selected_after_profit_swap["label"], "stronger-pressure")

    def test_floor_lock_prices_only_one_disruptor_and_respects_inventory_reach(
        self,
    ) -> None:
        """One designated attacker floors only the cities its stock can cover."""
        from sim.bots import _disruptor_floor_lock_prices

        disruptor_ids = [101, 102, 103]
        designated_id = 102
        market_max_prices = {
            "广州": 25_000.0,
            "深圳": 23_000.0,
            "杭州": 21_000.0,
            "南京": 19_000.0,
        }
        coverable_cities = ["广州", "深圳"]
        price_min = 3_500.0
        prices_by_company = _disruptor_floor_lock_prices(
            disruptor_ids=disruptor_ids,
            designated_company_id=designated_id,
            market_max_prices=market_max_prices,
            coverable_cities=coverable_cities,
            price_min=price_min,
        )

        self.assertEqual(set(prices_by_company), set(disruptor_ids))
        expected_floor = price_min + 1.0
        for company_id in disruptor_ids:
            self.assertEqual(
                set(prices_by_company[company_id]),
                set(market_max_prices),
            )
            for city, maximum in market_max_prices.items():
                expected = (
                    expected_floor
                    if company_id == designated_id and city in coverable_cities
                    else maximum
                )
                self.assertEqual(
                    float(prices_by_company[company_id][city]),
                    float(expected),
                    (company_id, city),
                )

        floored_company_ids = {
            company_id
            for company_id, city_prices in prices_by_company.items()
            if any(float(price) == expected_floor for price in city_prices.values())
        }
        self.assertEqual(floored_company_ids, {designated_id})

    def test_joint_rebalance_refuses_partial_intelligent_batch(self) -> None:
        """A saved first group is a checkpoint, never permission to publish it."""
        from sim.ai_training import configure_ai_training_roster
        from sim.bots import rebalance_super_bot_decisions

        with self.db.connect() as conn:
            configure_ai_training_roster(
                conn,
                0,
                0,
                1,
                disruptor_bot_count=1,
            )
            conn.execute(
                "UPDATE rounds SET status='open',starts_at=? WHERE round_no=1",
                (self.db.now_iso(),),
            )
            standard = self.db.one(
                conn,
                "SELECT id FROM companies WHERE is_super_bot=1 "
                "AND is_disruptor_bot=0",
            )
            disruptor = self.db.one(
                conn,
                "SELECT id FROM companies WHERE is_disruptor_bot=1",
            )
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,submitted_at,is_draft) "
                "VALUES(?,1,?,1)",
                (int(standard["id"]), self.db.now_iso()),
            )
            conn.commit()

            with self.assertRaisesRegex(ValueError, "未提交|未完成"):
                rebalance_super_bot_decisions(conn, 1)
            checkpoint = self.db.one(
                conn,
                "SELECT submitted_at,is_draft FROM decisions "
                "WHERE company_id=? AND round_no=1",
                (int(standard["id"]),),
            )
            self.assertIsNotNone(checkpoint)
            self.assertEqual(int(checkpoint["is_draft"]), 1)
            self.assertIsNone(
                self.db.one(
                    conn,
                    "SELECT 1 FROM decisions WHERE company_id=? AND round_no=1",
                    (int(disruptor["id"]),),
                )
            )

            conn.execute(
                "INSERT INTO decisions(company_id,round_no,submitted_at,is_draft) "
                "VALUES(?,1,?,1)",
                (int(disruptor["id"]), self.db.now_iso()),
            )
            conn.commit()
            with patch("sim.bots._rebalance_super_bot_production") as rebalance:
                count = rebalance_super_bot_decisions(conn, 1)
            rebalance.assert_called_once()
            self.assertEqual(count, 2)
            self.assertEqual(
                int(
                    self.db.one(
                        conn,
                        "SELECT COUNT(*) AS n FROM decisions WHERE round_no=1"
                    )["n"]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
