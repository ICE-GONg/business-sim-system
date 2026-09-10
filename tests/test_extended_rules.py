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
                "UPDATE companies SET is_bot=1,bot_profile=1,product_inventory=100000 WHERE id=?",
                (company_id,),
            )
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'worker',100,0)", (company_id,))
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'engineer',100,0)", (company_id,))
            from sim.bots import submit_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 2), 1)
            decision = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=2", (company_id,))
            self.assertLess(decision["worker_delta"], 0)
            self.assertLess(decision["engineer_delta"], 0)

    def test_super_bot_waits_reads_rivals_and_never_underfunds_patent(self):
        db = self.fresh("super-bot.db")
        human_id = self.one_company(db)
        with db.connect() as conn:
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (human_id,))
            cursor = conn.execute(
                "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,is_super_bot,bot_profile,"
                "setup_submitted_at,created_at) VALUES('SBOT01','Super',?,'深圳',15000000,1,1,3,?,?)",
                (db.hash_password("x"), db.now_iso(), db.now_iso()),
            )
            super_id = int(cursor.lastrowid)
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'深圳',1)", (super_id,))
            from sim.bots import submit_bot_decisions, submit_super_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 1), 0)
            with self.assertRaises(ValueError):
                submit_super_bot_decisions(conn, 1)
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,management_investment,"
                "quality_investment,submitted_at) VALUES(?,1,3300,6400,100000,100000,?)",
                (human_id, db.now_iso()),
            )
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,1,'广州',20000)",
                (human_id,),
            )
            self.assertEqual(submit_super_bot_decisions(conn, 1), 1)
            decision = db.one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=1", (super_id,))
            self.assertIn(decision["research_investment"], (0, 8_150_000))
            self.assertGreater(decision["production_volume"], 0)
            super_prices = db.all_rows(
                conn,
                "SELECT price FROM city_decisions WHERE company_id=? AND round_no=1 AND agent_delta>=0",
                (super_id,),
            )
            # It undercuts only when the resulting price still covers its own
            # full operating cost; otherwise profitability wins.
            self.assertTrue(all(3_500 <= float(row["price"]) <= 25_000 for row in super_prices))
            from sim.engine import settle_round
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
            settle_round(conn, 1)
            result = db.one(conn, "SELECT * FROM results WHERE company_id=? AND round_no=1", (super_id,))
            self.assertGreaterEqual(result["sold"], int(result["produced"] * 0.80))

    def test_regular_bots_use_diverse_prices_and_cpi_profiles(self):
        db = self.fresh("bot-diversity.db")
        first_id = self.one_company(db, 30_000_000)
        with db.connect() as conn:
            markets = [row["city"] for row in db.all_rows(conn, "SELECT city FROM market_config ORDER BY city")]
            for profile in range(7):
                if profile == 0:
                    company_id = first_id
                    conn.execute(
                        "UPDATE companies SET code='BOT01',name='Bot 1',is_bot=1,bot_profile=0,home_city=?,setup_submitted_at=? WHERE id=?",
                        (markets[0], db.now_iso(), company_id),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,bot_profile,setup_submitted_at,created_at) "
                        "VALUES(?,?,?,?,30000000,1,?,?,?)",
                        (f"BOT{profile + 1:02d}", f"Bot {profile + 1}", db.hash_password("x"), markets[profile], profile, db.now_iso(), db.now_iso()),
                    )
                    company_id = int(cursor.lastrowid)
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,1)", (company_id, markets[profile]))
            from sim.bots import submit_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 2), 7)
            decisions = db.all_rows(conn, "SELECT * FROM decisions WHERE round_no=2 ORDER BY company_id")
            prices = db.all_rows(
                conn,
                "SELECT cd.price FROM city_decisions cd JOIN companies c ON c.id=cd.company_id "
                "WHERE cd.round_no=2 AND cd.city=c.home_city ORDER BY c.id",
            )
            ma_indices = {
                round(float(row["management_investment"]) / max(1, int(row["worker_delta"]) + int(row["engineer_delta"])), 2)
                for row in decisions
            }
            self.assertGreaterEqual(len({round(float(row["price"]), 2) for row in prices}), 5)
            self.assertGreaterEqual(len(ma_indices), 5)
            self.assertTrue(all(float(row["research_investment"]) == 8_150_000 for row in decisions))

    def test_round_three_mi_is_above_large_threshold_and_varied(self):
        db = self.fresh("bot-mi-range.db")
        first_id = self.one_company(db, 30_000_000)
        with db.connect() as conn:
            markets = [row["city"] for row in db.all_rows(conn, "SELECT city FROM market_config ORDER BY city")]
            for profile in range(7):
                if profile == 0:
                    company_id = first_id
                    conn.execute(
                        "UPDATE companies SET code='BOT01',name='Bot 1',is_bot=1,bot_profile=0,home_city=?,setup_submitted_at=? WHERE id=?",
                        (markets[0], db.now_iso(), company_id),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,bot_profile,setup_submitted_at,created_at) "
                        "VALUES(?,?,?,?,30000000,1,?,?,?)",
                        (f"BOT{profile + 1:02d}", f"Bot {profile + 1}", db.hash_password("x"), markets[profile], profile, db.now_iso(), db.now_iso()),
                    )
                    company_id = int(cursor.lastrowid)
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,1)", (company_id, markets[profile]))
            from sim.bots import submit_bot_decisions
            self.assertEqual(submit_bot_decisions(conn, 3), 7)
            ratios = []
            for row in db.all_rows(
                conn,
                "SELECT cd.marketing_investment,m.population,m.penetration,m.max_price,"
                "COALESCE(a.count,0)+cd.agent_delta AS agents FROM city_decisions cd "
                "JOIN market_config m ON m.city=cd.city LEFT JOIN agents a "
                "ON a.company_id=cd.company_id AND a.city=cd.city "
                "WHERE cd.round_no=3 AND cd.marketing_investment>0",
            ):
                size = float(row["population"]) * float(row["penetration"]) * 1.10 ** 2
                large = (float(row["max_price"]) / 50.0) * size * 0.20
                large /= (1.0 + int(row["agents"]) * 0.10) * 1.5 * 2.0
                ratios.append(float(row["marketing_investment"]) / large)
            self.assertEqual(len(ratios), 7)
            self.assertGreaterEqual(min(ratios), 1.0)
            self.assertGreater(max(ratios) / min(ratios), 2.5)

    def test_all_super_bots_survive_and_sell_through_seven_rounds(self):
        db = self.fresh("all-super-seven-rounds.db")
        with db.connect() as conn:
            db.set_setting(conn, "total_rounds", 7)
            conn.execute("DELETE FROM companies")
            markets = [row["city"] for row in db.all_rows(conn, "SELECT city FROM market_config ORDER BY city")]
            for profile in range(7):
                cursor = conn.execute(
                    "INSERT INTO companies(code,name,password_hash,home_city,cash,is_bot,is_super_bot,bot_profile,setup_submitted_at,created_at) "
                    "VALUES(?,?,?,?,15000000,1,1,?,?,?)",
                    (f"SBOT{profile + 1:02d}", f"Super {profile + 1}", db.hash_password("x"), markets[profile], profile, db.now_iso(), db.now_iso()),
                )
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,1)", (cursor.lastrowid, markets[profile]))
            conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
            from sim.bots import submit_super_bot_decisions
            from sim.engine import settle_round
            for round_no in range(1, 8):
                if round_no > 1:
                    conn.execute("INSERT INTO rounds(round_no,status) VALUES(?,'open')", (round_no,))
                self.assertEqual(submit_super_bot_decisions(conn, round_no), 7)
                settle_round(conn, round_no)
                for result in db.all_rows(conn, "SELECT * FROM results WHERE round_no=?", (round_no,)):
                    report = json.loads(result["report_json"])
                    available = int(result["produced"]) + int(report["production"]["old_products"])
                    self.assertGreater(float(result["cash"]), 0)
                    self.assertGreater(float(result["net_assets"]), 0)
                    if available:
                        self.assertGreaterEqual(int(result["sold"]), int(available * 0.80))

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
