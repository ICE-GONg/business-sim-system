from __future__ import annotations

import unittest

from sim.bot_market_forecast import (
    forecast_market_sales,
    redistribute_secondary_sales,
)


class BotMarketForecastTest(unittest.TestCase):
    def test_secondary_categories_never_cross_eligibility(self):
        sales, secondary = redistribute_secondary_sales(
            company_available={1: 10, 2: 100, 3: 100},
            city_primary_sales={
                1: {"A": 10},
                2: {"A": 0},
                3: {"A": 0},
            },
            city_category_capacities={
                1: {"A": {"price": 40, "investment": 0}},
                2: {"A": {"price": 20, "investment": 0}},
                3: {"A": {"price": 0, "investment": 20}},
            },
            city_order=["A"],
        )

        self.assertGreater(secondary[2]["A"]["price"], 0)
        self.assertEqual(secondary[2]["A"]["investment"], 0)
        self.assertEqual(secondary[3]["A"]["price"], 0)
        self.assertGreater(secondary[3]["A"]["investment"], 0)
        self.assertEqual(sales[1]["A"], 10)
        self.assertLessEqual(sum(company["A"] for company in sales.values()), 90)

    def test_price_pool_uses_explicit_live_kds_power(self):
        players = [
            {
                "company_id": 1,
                "available": 1_000_000,
                "ma_index": 0,
                "qi_index": 0,
                "cities": {"A": {"agents": 1, "marketing": 0, "price": 6000}},
            },
            {
                "company_id": 2,
                "available": 1_000_000,
                "ma_index": 0,
                "qi_index": 0,
                "cities": {"A": {"agents": 1, "marketing": 0, "price": 8000}},
            },
        ]
        markets = [{
            "city": "A",
            "market_size": 100_000,
            "max_price": 25_000,
            "base_average_price": 10_000,
        }]

        for power in (8, 20):
            with self.subTest(power=power):
                forecast = forecast_market_sales(
                    players=players,
                    markets=markets,
                    ma_large_threshold=1300,
                    price_power=power,
                )
                denominator = 4000 ** power + 2000 ** power
                expected_low = 40_000 * 4000 ** power / denominator
                expected_high = 40_000 * 2000 ** power / denominator
                self.assertEqual(forecast["price_power"], power)
                self.assertAlmostEqual(
                    forecast["companies"][1]["visible_total"], expected_low, places=7
                )
                self.assertAlmostEqual(
                    forecast["companies"][2]["visible_total"], expected_high, places=7
                )

    def test_forecast_respects_market_and_company_inventory_limits(self):
        players = [
            {
                "company_id": 1,
                "available": 7,
                "ma_index": 10_000,
                "qi_index": 5_000,
                "cities": {"A": {"agents": 3, "marketing": 1_000_000, "price": 6000}},
            },
            {
                "company_id": 2,
                "available": 10_000,
                "ma_index": 10_000,
                "qi_index": 5_000,
                "cities": {"A": {"agents": 3, "marketing": 1_000_000, "price": 8000}},
            },
        ]
        forecast = forecast_market_sales(
            players=players,
            markets=[{
                "city": "A",
                "market_size": 1000,
                "max_price": 25_000,
                "base_average_price": 10_000,
            }],
            ma_large_threshold=1300,
            price_power=20,
        )

        companies = forecast["companies"]
        self.assertLessEqual(companies[1]["sold_total"], 7)
        self.assertLessEqual(companies[2]["sold_total"], 10_000)
        self.assertLessEqual(sum(row["sold_total"] for row in companies.values()), 1000)
        self.assertLessEqual(
            sum(row["visible_total"] for row in companies.values()), 1000 + 1e-7
        )


if __name__ == "__main__":
    unittest.main()
