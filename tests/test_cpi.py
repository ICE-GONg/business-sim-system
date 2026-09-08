from __future__ import annotations

import unittest

from sim.cpi import allocate_city_cpi, allocate_index_cpi, minimum_threshold


class CPIGeneratorPortTests(unittest.TestCase):
    def test_minimum_threshold_matches_javascript(self) -> None:
        self.assertEqual(minimum_threshold(500), 1)

    def test_equal_players_at_four_times_large_receive_full_index_pool(self) -> None:
        results = allocate_index_cpi(1, 500, [2000, 2000], [100, 100], 100)
        self.assertAlmostEqual(results[0]["cpi"], 10.0)
        self.assertAlmostEqual(results[1]["cpi"], 10.0)
        self.assertAlmostEqual(sum(row["cpi"] for row in results), 20.0)

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
        self.assertEqual(results[0]["thresholds"]["mi_large"], 8_000_000)


if __name__ == "__main__":
    unittest.main()

