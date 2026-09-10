from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class ExtendedRulesTest(unittest.TestCase):
    def fresh(self, filename: str):
        from sim import db

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        db.DB_PATH = Path(self.temp_dir.name) / filename
        os.environ["SIM_DB_PATH"] = str(db.DB_PATH)
        db.init_db()
        return db

    def one_company(self, db, cash: float = 15_000_000):
        with db.connect() as conn:
            company = db.one(conn, "SELECT * FROM companies ORDER BY id LIMIT 1")
            conn.execute("DELETE FROM companies WHERE id<>?", (company["id"],))
            conn.execute(
                "UPDATE companies SET name='Test',home_city='广州',setup_submitted_at=?,cash=? WHERE id=?",
                (db.now_iso(), cash, company["id"]),
            )
            return int(company["id"])

    def add_decision(self, db, company_id: int, round_no: int, *, worker_salary=3300, engineer_salary=6400, production=0, research=0):
        with db.connect() as conn:
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=?", (round_no,))
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,production_volume,research_investment,submitted_at) VALUES(?,?,?,?,?,?,?)",
                (company_id, round_no, worker_salary, engineer_salary, production, research, db.now_iso()),
            )

    def test_hidden_patent_cap_and_threshold(self):
        from sim.engine import effective_research_probability

        self.assertAlmostEqual(effective_research_probability(6_000_000, 1_500_000, 6_000_000, 0.43, 4 / 3), 0.43)
        self.assertAlmostEqual(effective_research_probability(8_000_000, 1_500_000, 6_000_000, 0.43, 4 / 3), 0.75 + 0.20 * 2 / 6)

    def test_unpaid_wages_remove_staff_and_compensation_becomes_debt(self):
        db = self.fresh("wages.db")
        company_id = self.one_company(db, 10_000)
        with db.connect() as conn:
            conn.execute("UPDATE market_config SET worker_initial_salary=1000,engineer_initial_salary=1000,interest_rate=0 WHERE city='广州'")
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'worker',10,0)", (company_id,))
        self.add_decision(db, company_id, 1, worker_salary=1000, engineer_salary=1000)
        from sim.engine import settle_round
        with db.connect() as conn:
            settle_round(conn, 1)
            self.assertEqual(db.employee_count(conn, company_id, "worker"), 3)
            result = db.one(conn, "SELECT * FROM results WHERE company_id=? AND round_no=1", (company_id,))
            report = json.loads(result["report_json"])
            self.assertEqual(report["finance"]["worker_wages"], 9_000)
            self.assertEqual(report["finance"]["quit_penalty_debt"], 13_000)
            self.assertEqual(result["debt"], 13_000)

    def test_components_remain_when_engineers_cannot_assemble_all_products(self):
        db = self.fresh("components.db")
        company_id = self.one_company(db)
        with db.connect() as conn:
            conn.execute("UPDATE market_config SET interest_rate=0 WHERE city='广州'")
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'worker',100,0)", (company_id,))
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'engineer',1,0)", (company_id,))
        self.add_decision(db, company_id, 1, production=10)
        from sim.engine import settle_round
        with db.connect() as conn:
            settle_round(conn, 1)
            result = db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (company_id,))
            production = json.loads(result["report_json"])["production"]
            self.assertLess(production["produced"], 10)
            self.assertEqual(production["component_used"], production["produced"] * 7)
            self.assertEqual(production["component_surplus"], production["components"] - production["component_used"])
            self.assertGreater(production["component_surplus"], 0)

    def test_existing_components_are_used_before_new_components_are_bought(self):
        db = self.fresh("existing-components.db")
        company_id = self.one_company(db)
        with db.connect() as conn:
            conn.execute(
                "UPDATE companies SET component_inventory=70,component_storage_capacity=70 WHERE id=?",
                (company_id,),
            )
            conn.execute(
                "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'engineer',4,0)",
                (company_id,),
            )
        self.add_decision(db, company_id, 1, production=10)
        from sim.engine import settle_round
        with db.connect() as conn:
            settle_round(conn, 1)
            report = json.loads(db.one(
                conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (company_id,)
            )["report_json"])
            self.assertEqual(report["production"]["components"], 0)
            self.assertEqual(report["production"]["produced"], 10)
            self.assertEqual(report["finance"]["component_material"], 0)
            self.assertEqual(report["production"]["component_surplus"], 0)

    def test_bot_submits_without_loan_or_market_reports(self):
        db = self.fresh("bots.db")
        company_id = self.one_company(db)
        with db.connect() as conn:
            conn.execute("UPDATE companies SET is_bot=1,bot_profile=2 WHERE id=?", (company_id,))
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company_id,))
            from sim.bots import submit_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 1), 1)
            decision = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=1", (company_id,))
            self.assertEqual(decision["loan_change"], 0)
            self.assertIsNotNone(decision["submitted_at"])
            reports = db.one(conn, "SELECT SUM(order_report) AS n FROM city_decisions WHERE company_id=? AND round_no=1", (company_id,))
            self.assertEqual(reports["n"], 0)

    def test_bot_opens_threshold_investments_in_order_and_prices_by_saturation(self):
        db = self.fresh("bot-strategy.db")
        company_id = self.one_company(db)
        from sim.bots import submit_bot_decisions
        from sim.engine import settle_round
        with db.connect() as conn:
            conn.execute("UPDATE companies SET is_bot=1,bot_profile=2 WHERE id=?", (company_id,))
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company_id,))
            submit_bot_decisions(conn, 1)
            first = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=1", (company_id,))
            city_first = db.one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=1 AND city='广州'", (company_id,))
            headcount = first["worker_delta"] + first["engineer_delta"]
            self.assertGreater(first["management_investment"] / max(1, headcount), 1300)
            self.assertEqual(first["quality_investment"], 0)
            self.assertEqual(city_first["marketing_investment"], 0)
            self.assertGreaterEqual(city_first["price"], 25_000 * 0.94)
            settle_round(conn, 1)
            first_report = json.loads(db.one(
                conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (company_id,)
            )["report_json"])
            conn.execute(
                "UPDATE market_round_stats SET player_total_volume=market_size*0.60,average_price=12000 "
                "WHERE city='广州' AND round_no=1"
            )
            conn.execute("INSERT INTO rounds(round_no,status) VALUES(2,'open')")
            submit_bot_decisions(conn, 2)
            second = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=2", (company_id,))
            city_second = db.one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=2 AND city='广州'", (company_id,))
            previous_average = first_report["human_resources"]["average_worker_salary"]
            self.assertGreaterEqual(second["worker_salary"], previous_average * 1.05 - 1e-6)
            self.assertLessEqual(second["worker_salary"], previous_average * 1.10 + 1e-6)
            self.assertGreater(second["quality_investment"], second["production_volume"] * 500)
            self.assertEqual(city_second["marketing_investment"], 0)
            self.assertLess(city_second["price"], 12000)
            settle_round(conn, 2)
            conn.execute("INSERT INTO rounds(round_no,status) VALUES(3,'open')")
            submit_bot_decisions(conn, 3)
            city_third = db.one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=3 AND city='广州'", (company_id,))
            self.assertGreater(city_third["marketing_investment"], 0)

    def test_bot_lays_off_surplus_staff_when_inventory_covers_plan(self):
        db = self.fresh("bot-layoff.db")
        company_id = self.one_company(db, 40_000_000)
        with db.connect() as conn:
            conn.execute(
                "UPDATE companies SET is_bot=1,bot_profile=1,product_inventory=10000 WHERE id=?",
                (company_id,),
            )
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'worker',100,0)", (company_id,))
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'engineer',100,0)", (company_id,))
            from sim.bots import submit_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 2), 1)
            decision = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=2", (company_id,))
            self.assertLess(decision["worker_delta"], 0)
            self.assertLess(decision["engineer_delta"], 0)

    def test_failed_research_accumulates_into_next_round(self):
        db = self.fresh("research.db")
        company_id = self.one_company(db)
        with db.connect() as conn:
            db.set_setting(conn, "research_probability_cap", 0)
            conn.execute("UPDATE market_config SET interest_rate=0 WHERE city='广州'")
        self.add_decision(db, company_id, 1, research=1_000_000)
        from sim.engine import settle_round
        with db.connect() as conn:
            settle_round(conn, 1)
            first = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (company_id,))["report_json"])
            self.assertEqual(first["research"]["accumulated_after"], 1_000_000)
            conn.execute("INSERT INTO rounds(round_no,status) VALUES(2,'open')")
        self.add_decision(db, company_id, 2, research=2_000_000)
        with db.connect() as conn:
            settle_round(conn, 2)
            second = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=2", (company_id,))["report_json"])
            self.assertEqual(second["research"]["accumulated_for_probability"], 3_000_000)
            self.assertEqual(second["research"]["accumulated_after"], 3_000_000)


if __name__ == "__main__":
    unittest.main()
