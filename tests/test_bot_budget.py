from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class SuperBotActualBudgetTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "actual-budget.db"
        environment = patch.dict(os.environ, {"SIM_DB_PATH": str(path)})
        environment.start()
        self.addCleanup(environment.stop)
        from sim import bots, db, engine

        database_path = patch.object(db, "DB_PATH", path)
        database_path.start()
        self.addCleanup(database_path.stop)
        db.init_db()
        self.db, self.bots, self.engine = db, bots, engine
        context = db.connect()
        self.conn = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.conn.execute("DELETE FROM companies")
        self.conn.execute("DELETE FROM market_config WHERE city<>'广州'")
        db.set_setting(self.conn, "global_max_loan", 0)
        self.conn.execute("INSERT INTO rounds(round_no,status) VALUES(5,'open')")

    def plan_and_settle(self, cash):
        cursor = self.conn.execute(
            "INSERT INTO companies(code,name,password_hash,home_city,cash,"
            "product_inventory,is_bot,is_super_bot,setup_submitted_at,created_at) "
            "VALUES('SUPER','SUPER','unused','广州',?,100,1,1,?,?)",
            (cash, self.db.now_iso(), self.db.now_iso()),
        )
        company_id = int(cursor.lastrowid)
        for role, count in (("worker", 1000), ("engineer", 500)):
            self.conn.execute(
                "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,1)",
                (company_id, role, count),
            )
        self.conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company_id,))
        original_selector = self.bots._select_empirical_super_candidate

        def selected_plan(*args, **kwargs):
            candidate = dict(original_selector(*args, **kwargs) or {})
            # Isolate the application boundary: a normalized candidate may
            # omit the severance bill from these larger existing cohorts.
            candidate.update({
                "groups": 20, "ma": 8000, "qi": 3000,
                "marketing": {0: 0.0}, "price_ratio": 0.8,
            })
            return candidate

        with patch.object(self.bots, "_select_empirical_super_candidate", side_effect=selected_plan):
            self.bots.submit_super_bot_decisions(self.conn, 5, defer_rebalance=True)
        self.bots.finalize_super_bot_decisions(self.conn, 5)
        decision = dict(self.db.one(self.conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=5", (company_id,)))
        city = dict(self.db.one(self.conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=5", (company_id,)))
        self.engine.settle_round(self.conn, 5)
        result = dict(self.db.one(self.conn, "SELECT * FROM results WHERE company_id=? AND round_no=5", (company_id,)))
        return decision, city, result, json.loads(result["report_json"])

    def test_cohort_layoffs_reduce_groups_and_keep_selected_indices_affordable(self):
        decision, city, result, report = self.plan_and_settle(12_000_000)
        self.assertGreater(decision["production_volume"], 0)
        self.assertLess(decision["production_volume"], 20 * 72)
        self.assertEqual(decision["production_volume"] % 72, 0)
        self.assertEqual(result["produced"], decision["production_volume"])
        self.assertGreater(report["finance"]["layoff"], 0)
        self.assertAlmostEqual(report["production"]["ma_index"], 8000)
        self.assertAlmostEqual(report["production"]["qi_index"], 3000)
        for requested, paid in (("management_investment", "management"), ("quality_investment", "quality")):
            self.assertAlmostEqual(decision[requested], report["finance"][paid])
        self.assertAlmostEqual(city["marketing_investment"], report["finance"]["marketing"])
        pre_sales_cost = sum(report["finance"][name] for name in (
            "wages", "layoff_cash", "quit_penalty_cash", "training", "materials",
            "storage", "agents", "marketing", "quality", "management",
        ))
        self.assertLessEqual(pre_sales_cost, 12_000_000 + 1e-6)

    def test_insufficient_severance_cash_requests_no_optional_spending(self):
        decision, city, result, report = self.plan_and_settle(1000)
        self.assertEqual(decision["production_volume"], 0)
        self.assertEqual(decision["management_investment"], 0)
        self.assertEqual(decision["quality_investment"], 0)
        self.assertEqual(decision["research_investment"], 0)
        self.assertEqual(city["marketing_investment"], 0)
        self.assertEqual(city["agent_delta"], 0)
        self.assertGreater(report["finance"]["layoff_debt"], 0)
        self.assertGreaterEqual(result["cash"], 0)

    def test_split_and_full_reanalysis_match_across_selected_and_unselected_cities(self):
        from sim.defaults import DEFAULT_MARKETS

        self.conn.executemany(
            "INSERT OR IGNORE INTO market_config(city,home_enabled,max_loan,min_loan,"
            "interest_rate,worker_initial_salary,engineer_initial_salary,component_material,"
            "product_material,component_storage,product_storage,population,penetration,"
            "initial_avg_price,max_price,transport_cost,worker_training_cost,engineer_training_cost) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            DEFAULT_MARKETS,
        )
        company_ids = []
        for number in range(4):
            cursor = self.conn.execute(
                "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,"
                "is_super_bot,bot_profile,setup_submitted_at,created_at) "
                "VALUES(?,?,?,'广州',200000000,1,1,?,?,?)",
                (str(number), str(number), "unused", number, self.db.now_iso(), self.db.now_iso()),
            )
            company_ids.append(int(cursor.lastrowid))
        self.bots.submit_super_bot_decisions(self.conn, 1, defer_rebalance=True)
        self.conn.commit()
        outputs = []
        for split in (False, True):
            with sqlite3.connect(":memory:") as copy:
                copy.row_factory = sqlite3.Row
                self.conn.backup(copy)
                for target in company_ids if split else (None,):
                    self.bots.submit_super_bot_decisions(
                        copy, 1, replace_existing=True, defer_rebalance=True,
                        target_ids={target} if target is not None else None,
                    )
                outputs.append({
                    table: [
                        {key: row[key] for key in row.keys() if key != "submitted_at"}
                        for row in self.db.all_rows(copy, f"SELECT * FROM {table} ORDER BY company_id")
                    ]
                    for table in ("decisions", "city_decisions")
                })
        self.assertEqual(outputs[0], outputs[1])

    def test_early_all_super_undercut_is_saved_at_the_price_used_by_search(self):
        cursor = self.conn.execute(
            "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,"
            "is_super_bot,setup_submitted_at,created_at) "
            "VALUES('UNDERCUT','UNDERCUT','unused','广州',15000000,1,1,?,?)",
            (self.db.now_iso(), self.db.now_iso()),
        )
        company_id = int(cursor.lastrowid)
        original_selector = self.bots._select_empirical_super_candidate
        chosen_ratios = []

        def undercut(*args, **kwargs):
            candidate = dict(original_selector(*args, **kwargs))
            candidate["price_ratio"] = 0.25
            chosen_ratios.append(candidate["price_ratio"])
            return candidate

        with patch.object(self.bots, "_select_empirical_super_candidate", side_effect=undercut):
            self.bots.submit_super_bot_decisions(self.conn, 1, defer_rebalance=True)
        decision = self.db.one(self.conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=1", (company_id,))
        city = self.db.one(self.conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=1", (company_id,))
        direct_cost = (
            7 * 300 + 650
            + 7 * 3 * 7 / 504 * float(decision["worker_salary"]) * 3
            + 4 * 14 / 504 * float(decision["engineer_salary"]) * 3
        )
        expected = max(3500, chosen_ratios[0] * 25000, direct_cost * 1.03)
        self.assertAlmostEqual(float(city["price"]), expected)
        self.assertLess(float(city["price"]), 25000 * 0.75)


if __name__ == "__main__":
    unittest.main()
