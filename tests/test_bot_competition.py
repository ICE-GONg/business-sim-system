from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sim.bot_market_forecast import forecast_market_sales


class BotCompetitionTest(unittest.TestCase):
    def setUp(self):
        # Importing the planner initializes sim.db, so isolate that first import.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ,
            {"SIM_DB_PATH": str(Path(directory.name) / "competition.db")},
        )
        environment.start()
        self.addCleanup(environment.stop)
        from sim import bots

        self.bots = bots

    def test_target_priority_preserves_team_and_rank_order(self):
        cases = (
            ("higher human", True, False, 1, 0.0, 5),
            ("higher Super Bot", False, True, 2, 0.0, 4),
            ("profitable normal Bot", False, False, 3, 0.5, 3),
            ("lower human", True, False, 9, 0.9, 2),
            ("lower Super Bot", False, True, 9, 0.9, 1),
            ("unprofitable normal Bot", False, False, 1, -0.2, 1),
        )
        priorities = []
        for label, is_human, is_super, opponent_rank, margin, expected in cases:
            with self.subTest(target=label):
                actual = self.bots._super_attack_priority(
                    is_human=is_human,
                    is_super=is_super,
                    opponent_rank=opponent_rank,
                    own_rank=4,
                    predicted_margin=margin,
                )
                self.assertEqual(actual, expected)
                priorities.append(actual)
        self.assertEqual(priorities, [5, 4, 3, 2, 1, 1])

    def test_attacker_counts_protect_leaders_and_change_by_phase(self):
        cases = (
            ([], 6, True, set()),
            ([11], 6, True, set()),
            ([11, 12], 6, False, {12}),
            ([11, 12, 13], 6, True, set()),
            ([11, 12, 13, 14], 6, False, {14}),
            ([1, 2, 3, 4, 5], 3, True, {5}),
            ([1, 2, 3, 4, 5, 6], 3, True, {5, 6}),
            (list(range(1, 10)), 3, False, set()),
            (list(range(1, 10)), 6, False, {7, 8, 9}),
            (list(range(1, 10)), 7, False, {7, 8, 9}),
        )
        for ranked, round_no, saturated, expected in cases:
            with self.subTest(ranked=ranked, round_no=round_no, saturated=saturated):
                self.assertEqual(
                    self.bots._competitive_super_attackers(
                        ranked,
                        official_round=round_no,
                        total_rounds=7,
                        markets_saturated=saturated,
                    ),
                    expected,
                )

    def test_losing_field_does_not_enable_unapproved_capital_sacrifice(self):
        choices = [
            {"name": "least-loss", "predicted_profit": -50, "starting_assets": 10000},
            {"name": "extra-loss", "predicted_profit": -500, "starting_assets": 10000,
             "targeted_damage": 1000, "target_tier": 5},
        ]
        chosen = self.bots._select_empirical_super_candidate(
            choices, 0, tactical_price_allowed=True,
            competitive_mode=True, sacrifice_allowed=False,
        )
        self.assertEqual(chosen["name"], "least-loss")

    def test_large_visible_cpi_drop_can_cause_no_target_surplus(self):
        markets = [{
            "city": "A", "market_size": 100_000,
            "max_price": 25_000, "base_average_price": 10_000,
        }]
        target = {
            "company_id": 1, "available": 30_000, "ma_index": 0, "qi_index": 0,
            "attack_weight": 5,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 8_000}},
        }
        attacker = {
            "company_id": 2, "available": 1_000, "ma_index": 0, "qi_index": 0,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 6_000}},
        }
        before_forecast = forecast_market_sales(
            players=[target], markets=markets,
            ma_large_threshold=1_300, price_power=8,
        )
        after_forecast = forecast_market_sales(
            players=[target, attacker], markets=markets,
            ma_large_threshold=1_300, price_power=8,
        )
        before = before_forecast["companies"][1]
        after = after_forecast["companies"][1]

        # Visible CPI alone would claim almost 30,000 newly unsold units.
        claimed_loss = min(target["available"], before["visible_total"]) - min(
            target["available"], after["visible_total"],
        )
        self.assertGreater(claimed_loss, 29_000)
        # Unused price capacity returns through settlement's secondary pool.
        self.assertGreater(after["secondary_total"], 29_000)
        self.assertEqual(before["sold_total"], 30_000)
        self.assertEqual(after["sold_total"], 30_000)
        self.assertEqual(after["available"] - after["sold_total"], 0)
        pressure = self.bots._super_target_pressure(before_forecast, after_forecast, [target])
        self.assertEqual(pressure["targeted_damage"], 0)
        self.assertEqual(pressure["target_surplus"], 0)

    def test_real_denial_values_lost_revenue_once(self):
        markets = [{"city": "A", "market_size": 100_000, "max_price": 25_000, "base_average_price": 10_000}]
        target = {
            "company_id": 1, "available": 40_000, "ma_index": 0, "qi_index": 0,
            "attack_weight": 5,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 8_000}},
        }
        attacker = {
            "company_id": 2, "available": 40_000, "ma_index": 0, "qi_index": 0,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 6_000}},
        }
        before = forecast_market_sales(players=[target], markets=markets, ma_large_threshold=1300, price_power=8)
        after = forecast_market_sales(players=[target, attacker], markets=markets, ma_large_threshold=1300, price_power=8)
        pressure = self.bots._super_target_pressure(before, after, [target])
        lost_units = before["companies"][1]["sold_total"] - after["companies"][1]["sold_total"]
        self.assertGreater(lost_units, 39_000)
        self.assertEqual(pressure["targeted_damage"], lost_units * 8_000)
        self.assertEqual(pressure["target_surplus"], lost_units)
        self.assertEqual(pressure["target_tier"], 5)
        self.assertEqual(pressure["target_company_id"], 1)

    def test_sales_relocated_to_another_city_are_not_denial(self):
        markets = [
            {"city": city, "market_size": 100_000, "max_price": 25_000, "base_average_price": 10_000}
            for city in ("A", "B")
        ]
        target = {
            "company_id": 1, "available": 30_000, "ma_index": 0, "qi_index": 0,
            "attack_weight": 5,
            "cities": {city: {"agents": 1, "marketing": 0, "price": 8_000} for city in ("A", "B")},
        }
        attacker = {
            "company_id": 2, "available": 60_000, "ma_index": 0, "qi_index": 0,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 6_000}},
        }
        before = forecast_market_sales(players=[target], markets=markets, ma_large_threshold=1300, price_power=8)
        after = forecast_market_sales(players=[target, attacker], markets=markets, ma_large_threshold=1300, price_power=8)
        first, last = before["companies"][1], after["companies"][1]
        self.assertLess(last["sold_units"]["A"], first["sold_units"]["A"])
        self.assertGreater(last["sold_units"]["B"], first["sold_units"]["B"])
        self.assertEqual(last["sold_total"], first["sold_total"])
        pressure = self.bots._super_target_pressure(before, after, [target])
        self.assertEqual(pressure["targeted_damage"], 0)
        self.assertEqual(pressure["target_surplus"], 0)

    def test_target_tier_precedes_larger_damage_to_lower_priority_company(self):
        markets = [{"city": "A", "market_size": 100_000, "max_price": 25_000, "base_average_price": 10_000}]
        targets = [
            {
                "company_id": company_id, "available": stock, "ma_index": 0, "qi_index": 0,
                "attack_weight": tier,
                "cities": {"A": {"agents": 1, "marketing": 0, "price": price}},
            }
            for company_id, stock, price, tier in ((1, 20_000, 8_000, 4), (2, 10_000, 9_000, 5))
        ]
        attacker = {
            "company_id": 3, "available": 40_000, "ma_index": 0, "qi_index": 0,
            "cities": {"A": {"agents": 1, "marketing": 0, "price": 6_000}},
        }
        before = forecast_market_sales(players=targets, markets=markets, ma_large_threshold=1300, price_power=8)
        after = forecast_market_sales(players=[*targets, attacker], markets=markets, ma_large_threshold=1300, price_power=8)
        first = self.bots._super_target_pressure(before, after, [targets[0]])
        second = self.bots._super_target_pressure(before, after, [targets[1]])
        self.assertGreater(first["targeted_damage"], second["targeted_damage"])
        self.assertGreater(second["targeted_damage"], 0)
        chosen = self.bots._super_target_pressure(before, after, targets)
        self.assertEqual(chosen["target_company_id"], 2)
        self.assertEqual(chosen["target_tier"], 5)


if __name__ == "__main__":
    unittest.main()
