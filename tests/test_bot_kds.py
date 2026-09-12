from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BotKDSRegressionTest(unittest.TestCase):
    """Exercise the real planner against small, isolated custom KDS games."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "bot-kds.db"
        # sim.db initializes on import, so isolate its environment first too.
        environment = patch.dict(os.environ, {"SIM_DB_PATH": str(path)})
        environment.start()
        self.addCleanup(environment.stop)
        from sim import bots, db

        database_path = patch.object(db, "DB_PATH", path)
        database_path.start()
        self.addCleanup(database_path.stop)
        db.init_db()
        self.db, self.bots = db, bots
        self.connection_context = db.connect()
        self.conn = self.connection_context.__enter__()
        self.addCleanup(self.connection_context.__exit__, None, None, None)
        self.conn.execute("DELETE FROM companies")
        self.conn.execute("DELETE FROM market_config WHERE city NOT IN ('广州','深圳')")

    def company(self, code, *, super_mode=False, ordinary=False, cash=15_000_000):
        cursor = self.conn.execute(
            "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,"
            "is_super_bot,bot_profile,setup_submitted_at,created_at) "
            "VALUES(?,?,?,'广州',?,?,?,0,?,?)",
            (code, code, "unused-test-password", cash, int(super_mode or ordinary),
             int(super_mode), self.db.now_iso(), self.db.now_iso()),
        )
        return int(cursor.lastrowid)

    def submitted_seller(self, code, price):
        company_id = self.company(code)
        self.conn.execute(
            "INSERT INTO decisions(company_id,round_no,production_volume,submitted_at) "
            "VALUES(?,1,100000,?)", (company_id, self.db.now_iso()),
        )
        self.conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)",
            (company_id,),
        )
        self.conn.execute(
            "INSERT INTO city_decisions(company_id,round_no,city,price) "
            "VALUES(?,1,'广州',?)", (company_id, price),
        )
        return company_id

    def test_final_capacity_forecast_uses_current_kds_price_power(self):
        self.conn.execute("DELETE FROM market_config WHERE city<>'广州'")
        self.conn.execute(
            "UPDATE market_config SET population=1000000,penetration=0.1,"
            "initial_avg_price=10000"
        )
        lower_price = self.submitted_seller("LOW", 6000)
        higher_price = self.submitted_seller("HIGH", 8000)
        markets = [dict(row) for row in self.db.all_rows(self.conn, "SELECT * FROM market_config")]
        # No MA/QI/MI: the 40% price pool is an independent arithmetic oracle.
        for power in (2, 11):
            with self.subTest(power=power):
                self.db.set_setting(self.conn, "cpi_price_power", power)
                capacities = self.bots._forecast_submitted_cpi_capacity(self.conn, 1, markets)
                expected_low = 40000 * 4000 ** power / (4000 ** power + 2000 ** power)
                self.assertAlmostEqual(capacities[lower_price], expected_low, places=7)
                self.assertAlmostEqual(capacities[higher_price], 40000 - expected_low, places=7)

    def test_super_bot_candidate_search_and_decision_forecast_use_current_power(self):
        self.conn.execute("DELETE FROM market_config WHERE city<>'广州'")
        self.conn.execute("UPDATE market_config SET initial_avg_price=20000")
        self.submitted_seller("RIVAL", 8000)
        super_id = self.company("SUPER", super_mode=True)
        self.conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (super_id,),
        )
        # Change KDS in the same process: neither evaluator may cache default 8
        # or the previous round's exponent. Wrapping retains real calculations.
        for power in (2, 11):
            with self.subTest(power=power):
                self.db.set_setting(self.conn, "cpi_price_power", power)
                with (
                    patch.object(
                        self.bots, "prepare_city_cpi_for_company",
                        wraps=self.bots.prepare_city_cpi_for_company,
                    ) as candidate_cpi,
                    patch.object(
                        self.bots, "allocate_city_cpi_for_company",
                        wraps=self.bots.allocate_city_cpi_for_company,
                    ) as decision_forecast,
                ):
                    self.assertEqual(self.bots.submit_super_bot_decisions(
                        self.conn, 1, replace_existing=True, defer_rebalance=True,
                    ), 1)
                for allocator in (candidate_cpi, decision_forecast):
                    self.assertGreater(allocator.call_count, 0)
                    self.assertEqual(
                        {call.kwargs.get("price_power") for call in allocator.call_args_list},
                        {power},
                    )
                decision = self.db.one(
                    self.conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=1", (super_id,),
                )
                self.assertIsNotNone(decision["submitted_at"])
                self.assertGreater(decision["production_volume"], 0)

    def test_ordinary_and_super_bot_agent_additions_obey_each_city_kds_limit(self):
        bot_id = self.company("BOT", ordinary=True, cash=300_000_000)
        self.db.set_setting(self.conn, "total_rounds", 10)
        self.conn.execute("INSERT INTO rounds(round_no,status) VALUES(7,'open')")
        # R7 wants at least four agents in each initially empty city. This
        # catches both an old hard-coded 3 and a mistaken global per-round cap.
        for super_mode in (False, True):
            self.conn.execute(
                "UPDATE companies SET is_super_bot=? WHERE id=?", (int(super_mode), bot_id),
            )
            for limit in (0, 1, 6):
                with self.subTest(super_mode=super_mode, limit=limit):
                    self.conn.execute("DELETE FROM decisions")
                    self.conn.execute("DELETE FROM city_decisions")
                    self.db.set_setting(self.conn, "max_agent_add_per_city_round", limit)
                    if super_mode:
                        count = self.bots.submit_super_bot_decisions(self.conn, 7, defer_rebalance=True)
                    else:
                        count = self.bots.submit_bot_decisions(self.conn, 7)
                    self.assertEqual(count, 1)
                    additions = [int(row["agent_delta"]) for row in self.db.all_rows(
                        self.conn, "SELECT agent_delta FROM city_decisions WHERE company_id=? "
                        "AND round_no=7 ORDER BY city", (bot_id,),
                    )]
                    self.assertEqual(len(additions), 2)
                    self.assertTrue(all(0 <= delta <= limit for delta in additions), additions)
                    if limit <= 1:
                        self.assertEqual(additions, [limit, limit])
                    else:
                        self.assertTrue(all(delta > 3 for delta in additions), additions)

    def test_global_saturation_uses_one_latest_round_not_historical_peaks(self):
        markets = [dict(row) for row in self.db.all_rows(
            self.conn, "SELECT * FROM market_config ORDER BY city",
        )]
        for round_no, utilization in ((1, 0.96), (2, 0.55)):
            for market in markets:
                size = float(market["population"]) * float(market["penetration"])
                self.conn.execute(
                    "INSERT INTO market_round_stats(city,round_no,base_average_price,"
                    "average_price,market_size,player_total_volume) VALUES(?,?,?,?,?,?)",
                    (market["city"], round_no, market["initial_avg_price"],
                     market["initial_avg_price"], size, size * utilization),
                )
        self.assertTrue(self.bots._all_markets_near_capacity(self.conn, 2, markets))
        self.assertFalse(self.bots._all_markets_near_capacity(self.conn, 3, markets))

    def test_joint_strategy_rereads_qi_safety_and_never_overcommits_cash(self):
        self.conn.execute("DELETE FROM market_config WHERE city<>'广州'")
        super_id = self.company("SUPER", super_mode=True, cash=1_000_000_000)
        self.conn.execute("UPDATE companies SET bot_profile=2 WHERE id=?", (super_id,))
        self.conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (super_id,),
        )
        self.conn.execute(
            "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,"
            "production_volume,submitted_at) VALUES(?,5,3300,6400,1000,?)",
            (super_id, self.db.now_iso()),
        )
        self.conn.execute(
            "INSERT INTO city_decisions(company_id,round_no,city,price) "
            "VALUES(?,5,'广州',6000)", (super_id,),
        )
        markets = [dict(row) for row in self.db.all_rows(
            self.conn, "SELECT * FROM market_config",
        )]
        indices = []
        for multiplier in (1.20, 1.80):
            self.db.set_setting(self.conn, "qi_safe_multiplier", multiplier)
            self.conn.execute(
                "UPDATE decisions SET management_investment=0,quality_investment=0 "
                "WHERE company_id=? AND round_no=5", (super_id,),
            )
            self.conn.execute(
                "UPDATE city_decisions SET price=6000,marketing_investment=0 "
                "WHERE company_id=? AND round_no=5", (super_id,),
            )
            self.assertTrue(self.bots._coordinate_super_bot_prices(self.conn, 5, markets))
            decision = self.db.one(
                self.conn, "SELECT quality_investment FROM decisions "
                "WHERE company_id=? AND round_no=5", (super_id,),
            )
            indices.append(float(decision["quality_investment"]) / 1000.0)
        self.assertAlmostEqual(indices[1] / indices[0], 1.80 / 1.20, places=6)

        # With production costs already above available cash, the final joint
        # pass must request no CPI investment instead of relying on settlement
        # to truncate MI/QI/MA silently.
        self.conn.execute("UPDATE companies SET cash=1000 WHERE id=?", (super_id,))
        self.conn.execute(
            "UPDATE decisions SET production_volume=100000,management_investment=0,"
            "quality_investment=0 WHERE company_id=? AND round_no=5", (super_id,),
        )
        self.conn.execute(
            "UPDATE city_decisions SET price=6000,marketing_investment=0 "
            "WHERE company_id=? AND round_no=5", (super_id,),
        )
        self.assertTrue(self.bots._coordinate_super_bot_prices(self.conn, 5, markets))
        decision = self.db.one(
            self.conn, "SELECT management_investment,quality_investment FROM decisions "
            "WHERE company_id=? AND round_no=5", (super_id,),
        )
        marketing = self.db.one(
            self.conn, "SELECT SUM(marketing_investment) AS total FROM city_decisions "
            "WHERE company_id=? AND round_no=5", (super_id,),
        )
        self.assertEqual(float(decision["management_investment"]), 0.0)
        self.assertEqual(float(decision["quality_investment"]), 0.0)
        self.assertEqual(float(marketing["total"] or 0), 0.0)


if __name__ == "__main__":
    unittest.main()
