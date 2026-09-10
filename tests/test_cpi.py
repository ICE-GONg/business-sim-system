from __future__ import annotations

import random
import unittest

from sim.cpi import (
    agent_mi_benefit,
    allocate_city_cpi,
    allocate_city_cpi_for_company,
    allocate_index_cpi,
    minimum_threshold,
)


class CPIGeneratorPortTests(unittest.TestCase):
    def test_minimum_threshold_matches_javascript(self) -> None:
        self.assertEqual(minimum_threshold(500), 1)

    def test_equal_players_at_four_times_large_receive_full_index_pool(self) -> None:
        results = allocate_index_cpi(1, 500, [2000, 2000], [100, 100], 100)
        self.assertAlmostEqual(results[0]["cpi"], 10.0)
        self.assertAlmostEqual(results[1]["cpi"], 10.0)
        self.assertAlmostEqual(sum(row["cpi"] for row in results), 20.0)

    def test_zero_investment_gets_no_gift_and_only_ma_is_exactly_twenty(self) -> None:
        empty = allocate_index_cpi(1, 500, [0], [9_800], 9_800)
        self.assertEqual(empty[0]["cpi"], 0)
        self.assertEqual(empty[0]["breakdown"]["gift_cpi"], 0)

        result = allocate_city_cpi(
            [{"company_id": 1, "qi_index": 0, "ma_index": 5_200, "mi_investment": 0, "price": 9_800, "agents": 1}],
            market_size=80_000,
            max_price=25_000,
            ma_large_threshold=1_300,
            average_price=9_800,
            market_average_price=9_800,
        )[0]
        self.assertAlmostEqual(result["ma_cpi"], 20.0)
        self.assertEqual(result["qi_cpi"], 0)
        self.assertEqual(result["mi_cpi"], 0)
        self.assertEqual(result["price_cpi"], 0)
        self.assertAlmostEqual(result["total_cpi"], 20.0)

    def test_each_component_pool_respects_twenty_twenty_twenty_forty_caps(self) -> None:
        entries = [
            {"company_id": index, "qi_index": 5_000 + index, "ma_index": 13_000 + index, "mi_investment": 20_000_000 + index, "price": 8_000 + index * 100, "agents": index}
            for index in range(1, 9)
        ]
        results = allocate_city_cpi(
            entries,
            market_size=80_000,
            max_price=25_000,
            ma_large_threshold=1_300,
            average_price=9_000,
            market_average_price=9_500,
        )
        self.assertLessEqual(sum(row["ma_cpi"] for row in results), 20.0 + 1e-9)
        self.assertLessEqual(sum(row["qi_cpi"] for row in results), 20.0 + 1e-9)
        self.assertLessEqual(sum(row["mi_cpi"] for row in results), 20.0 + 1e-9)
        self.assertLessEqual(sum(row["price_cpi"] for row in results), 40.0 + 1e-9)

    def test_price_cpi_uses_eighth_power_and_city_is_independent(self) -> None:
        entries = [
            {"company_id": 1, "qi_index": 2000, "ma_index": 5200, "mi_investment": 32_000_000, "price": 80},
            {"company_id": 2, "qi_index": 2000, "ma_index": 5200, "mi_investment": 32_000_000, "price": 100},
        ]
        results = allocate_city_cpi(
            entries,
            market_size=80_000,
            max_price=25_000,
            ma_large_threshold=1_300,
            average_price=90,
            market_average_price=90,
            price_power=8,
        )
        self.assertAlmostEqual(results[0]["price_cpi"], 40.0)
        self.assertAlmostEqual(results[1]["price_cpi"], 0.0)
        self.assertEqual(results[0]["thresholds"]["qi_large"], 500.0)
        self.assertAlmostEqual(results[0]["thresholds"]["mi_large"], 8_000_000 / 3)
        self.assertAlmostEqual(sum(item["qi_cpi"] for item in results), 20.0)

    def test_agent_count_increases_mi_effect_and_reduces_player_threshold(self) -> None:
        entries = [
            {"company_id": 1, "qi_index": 0, "ma_index": 0, "mi_investment": 1_000_000, "price": 9_800, "agents": 1},
            {"company_id": 2, "qi_index": 0, "ma_index": 0, "mi_investment": 1_000_000, "price": 9_800, "agents": 3},
        ]
        results = allocate_city_cpi(
            entries,
            market_size=80_000,
            max_price=25_000,
            ma_large_threshold=1_300,
            average_price=9_800,
            market_average_price=9_800,
        )
        self.assertAlmostEqual(agent_mi_benefit(1), 1.1)
        self.assertAlmostEqual(agent_mi_benefit(3), 1.3)
        self.assertLess(results[1]["thresholds"]["mi_large"], results[0]["thresholds"]["mi_large"])
        self.assertGreater(results[1]["mi_cpi"], results[0]["mi_cpi"])

    def test_target_only_allocator_is_equivalent_to_full_allocator(self) -> None:
        rng = random.Random(20260910)
        for player_count in (1, 2, 7, 25, 510):
            entries = [
                {
                    "company_id": index + 1,
                    "qi_index": rng.uniform(0, 5000),
                    "ma_index": rng.uniform(0, 13000),
                    "mi_investment": rng.uniform(0, 30_000_000),
                    "price": rng.uniform(3500, 25000),
                    "agents": rng.randint(0, 8),
                }
                for index in range(player_count)
            ]
            average_price = rng.uniform(7000, 22000)
            market_average = rng.uniform(7000, 22000)
            full = allocate_city_cpi(
                entries,
                market_size=88_000,
                max_price=25_000,
                ma_large_threshold=1300,
                average_price=average_price,
                market_average_price=market_average,
            )
            for target in (entries[0], entries[-1]):
                target_only = allocate_city_cpi_for_company(
                    entries,
                    target_company_id=int(target["company_id"]),
                    market_size=88_000,
                    max_price=25_000,
                    ma_large_threshold=1300,
                    average_price=average_price,
                    market_average_price=market_average,
                )
                expected = next(
                    row["total_cpi"] for row in full
                    if row["company_id"] == target["company_id"]
                )
                self.assertAlmostEqual(target_only, expected, places=10)


if __name__ == "__main__":
    unittest.main()
