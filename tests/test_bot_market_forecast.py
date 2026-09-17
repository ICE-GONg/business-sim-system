from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_each_investment_pool_uses_only_its_qualifying_sellers(self):
        # Each investment pool has one qualifying seller. The very cheap
        # fourth seller has ample stock but must not move any pool's average.
        players = [
            {
                "company_id": company_id,
                "available": 100_000,
                "ma_index": ma,
                "qi_index": qi,
                "cities": {"A": {"agents": 1, "marketing": mi, "price": price}},
            }
            for company_id, ma, qi, mi, price in (
                (1, 1300, 0, 0, 6000),
                (2, 0, 500, 0, 14000),
                (3, 0, 0, 20000, 18000),
                (4, 1, 1, 1, 3500),
            )
        ]
        forecast = forecast_market_sales(
            players=players,
            markets=[{
                "city": "A", "market_size": 1000,
                "max_price": 25000, "base_average_price": 10000,
            }],
            ma_large_threshold=1300,
            price_power=8,
        )
        self.assertEqual(
            forecast["player_average_prices"]["A"],
            {"ma": 6000, "qi": 14000, "mi": 18000},
        )
        for row in forecast["companies"].values():
            self.assertEqual(
                row["breakdown"]["A"]["investment_average_prices"],
                {"ma": 6000, "qi": 14000, "mi": 18000},
            )

    def test_no_eligible_sales_uses_market_base_average_for_each_pool(self):
        forecast = forecast_market_sales(
            players=[{
                "company_id": 1, "available": 0,
                "ma_index": 13000, "qi_index": 5000,
                "cities": {"A": {"agents": 1, "marketing": 200000, "price": 3500}},
            }],
            markets=[{
                "city": "A", "market_size": 1000,
                "max_price": 25000, "base_average_price": 10000,
            }],
            ma_large_threshold=1300,
            price_power=8,
        )
        self.assertEqual(
            forecast["player_average_prices"]["A"],
            {"ma": 10000, "qi": 10000, "mi": 10000},
        )
        self.assertEqual(forecast["companies"][1]["sold_total"], 0)

    def test_heterogeneous_forecast_matches_real_settlement_with_secondary_sales(self):
        # Resolve already-affordable decisions into the forecast, then compare
        # against the real settlement across two cities and both CPI categories.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forecast-settlement.db"
            with patch.dict(os.environ, {"SIM_DB_PATH": str(path)}):
                from sim import db
                from sim.engine import settle_round

                with patch.object(db, "DB_PATH", path):
                    db.init_db()
                    with db.connect() as conn:
                        conn.execute("DELETE FROM companies")
                        conn.execute("DELETE FROM market_config WHERE city NOT IN ('广州','深圳')")
                        for city, size, average in (("广州", 1000, 10000), ("深圳", 1500, 8000)):
                            conn.execute(
                                "UPDATE market_config SET population=?,penetration=1,"
                                "initial_avg_price=? WHERE city=?",
                                (size, average, city),
                            )
                        players = []
                        for number, (stock, ma, qi, mi, prices) in enumerate((
                            (10, 10000, 2500, 200000, (6000, 5000)),
                            (10000, 1300, 0, 0, (8000, 6500)),
                            (50000, 0, 500, 0, (14000, 12000)),
                            (1200, 0, 0, 50000, (18000, 15000)),
                        ), 1):
                            cursor = conn.execute(
                                "INSERT INTO companies(code,name,password_hash,home_city,"
                                "cash,product_inventory,setup_submitted_at,created_at) "
                                "VALUES(?,?,?,'广州',1000000000,?,?,?)",
                                (str(number), str(number), "unused", stock, db.now_iso(), db.now_iso()),
                            )
                            company_id = int(cursor.lastrowid)
                            conn.execute(
                                "INSERT INTO employee_cohorts(company_id,role,count,hire_round) "
                                "VALUES(?,'worker',1,0)", (company_id,),
                            )
                            conn.execute(
                                "INSERT INTO decisions(company_id,round_no,worker_salary,"
                                "engineer_salary,management_investment,quality_investment,"
                                "submitted_at) VALUES(?,1,3300,6400,?,?,?)",
                                (company_id, ma, qi * stock * 1.2, db.now_iso()),
                            )
                            cities = {}
                            for city, price in zip(("广州", "深圳"), prices):
                                conn.execute(
                                    "INSERT INTO agents(company_id,city,count) VALUES(?,?,1)",
                                    (company_id, city),
                                )
                                conn.execute(
                                    "INSERT INTO city_decisions(company_id,round_no,city,"
                                    "marketing_investment,price) VALUES(?,1,?,?,?)",
                                    (company_id, city, mi, price),
                                )
                                cities[city] = {"agents": 1, "marketing": mi, "price": price}
                            players.append({
                                "company_id": company_id, "available": stock,
                                "ma_index": ma, "qi_index": qi, "cities": cities,
                            })

                        for power in (2, 11):
                            with self.subTest(price_power=power):
                                db.set_setting(conn, "cpi_price_power", power)
                                forecast = forecast_market_sales(
                                    players=players,
                                    markets=[
                                        {"city": "广州", "market_size": 1000, "max_price": 25000, "base_average_price": 10000},
                                        {"city": "深圳", "market_size": 1500, "max_price": 25000, "base_average_price": 8000},
                                    ],
                                    ma_large_threshold=1300,
                                    price_power=power,
                                )
                                conn.execute("SAVEPOINT settlement_check")
                                try:
                                    settle_round(conn, 1)
                                    self.assertGreater(
                                        sum(row["secondary_total"] for row in forecast["companies"].values()),
                                        0,
                                    )
                                    for row in db.all_rows(conn, "SELECT * FROM city_results WHERE round_no=1"):
                                        company = forecast["companies"][int(row["company_id"])]
                                        city = str(row["city"])
                                        breakdown = json.loads(row["breakdown_json"])
                                        self.assertEqual(company["sold_units"][city], row["sold"])
                                        self.assertAlmostEqual(company["visible"][city], row["cpi_units"], places=7)
                                        self.assertAlmostEqual(company["secondary"][city], breakdown["secondary_units"], places=7)
                                        self.assertEqual(
                                            forecast["player_average_prices"][city],
                                            breakdown["investment_average_prices"],
                                        )
                                finally:
                                    conn.execute("ROLLBACK TO settlement_check")
                                    conn.execute("RELEASE settlement_check")


if __name__ == "__main__":
    unittest.main()
