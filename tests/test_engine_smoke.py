from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path


class SettlementSmokeTest(unittest.TestCase):
    def test_round_settles_for_all_seeded_companies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "smoke.db")
            from sim import db
            from sim.engine import settle_round, weighted_market_average

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                for company in companies:
                    conn.execute(
                        "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                        (db.now_iso(), company["id"]),
                    )
                    conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company["id"],))
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,engineer_salary,"
                        "management_investment,production_volume,quality_investment,research_investment,submitted_at) "
                        "VALUES(?,1,1000000,3,3300,4,6400,9100,10,5000,?,?)",
                        (company["id"], 1_500_000 if company["id"] == companies[0]["id"] else 0, db.now_iso()),
                    )
                    conn.execute(
                        "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) VALUES(?,1,'广州',3,8000000,9800,1)",
                        (company["id"],),
                    )
                    conn.execute(
                        "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price) VALUES(?,1,'深圳',3,0,9800)",
                        (company["id"],),
                    )
                settle_round(conn, 1)
                results = db.all_rows(conn, "SELECT * FROM results ORDER BY company_id")
                self.assertEqual(len(results), 4)
                self.assertTrue(all(0 <= row["sold"] <= row["produced"] for row in results))
                self.assertTrue(all(0 < row["sold"] <= 10 for row in results))
                self.assertTrue(all(row["cash"] >= 0 for row in results))
                report = json.loads(results[0]["report_json"])
                self.assertIn("previous_workers", report["human_resources"])
                self.assertIn("component_storage_before", report["production"])
                self.assertIn("components", report["production"])
                self.assertAlmostEqual(report["human_resources"]["average_worker_salary"], 3300)
                self.assertAlmostEqual(report["human_resources"]["average_engineer_salary"], 6400)
                self.assertAlmostEqual(results[0]["debt"], 1_030_000)
                self.assertEqual(report["finance"]["interest"], 30_000)
                self.assertGreater(report["finance"]["research"], 0)
                self.assertGreater(report["finance"]["market_reports"], 0)
                self.assertAlmostEqual(results[0]["net_assets"], results[0]["total_assets"] - results[0]["debt"])
                self.assertAlmostEqual(
                    results[0]["net_assets"],
                    report["finance"]["round_ends"] + report["key_metrics"]["inventory_book_value"] - results[0]["debt"],
                )
                self.assertNotIn("market_average_price", report["sales"][0])
                self.assertNotIn('market_average_price', json.dumps(report["sales"]))
                self.assertTrue(next(item for item in report["sales"] if item["city"] == "广州")["report_purchased"])
                self.assertEqual(report["research"]["active_patents_this_round"], 0)
                self.assertEqual(report["research"]["effective_from_round"], 2)
                self.assertEqual(report["research"]["patents_after"], 1)
                finance = report["finance"]
                expected_cash = (
                    finance["round_begins"] + finance["loan_change"]
                    - finance["wages"] - finance["layoff"] - finance["training"]
                    - finance["materials"] - finance["storage"] - finance["agents"]
                    - finance["marketing"] - finance["quality"] - finance["management"]
                    + finance["sales_revenue"] - finance["research"] - finance["market_reports"] - finance["tax"]
                )
                self.assertAlmostEqual(finance["round_ends"], expected_cash)
                taxable_profit = report["key_metrics"]["sales_revenue"] - (report["key_metrics"]["cost"] - finance["tax"])
                self.assertAlmostEqual(finance["tax"], max(0, taxable_profit * 0.20))
                agents = db.all_rows(conn, "SELECT city,count FROM agents WHERE company_id=? AND city IN ('广州','深圳') ORDER BY city", (companies[0]["id"],))
                self.assertEqual({row["city"]: row["count"] for row in agents}, {"广州": 4, "深圳": 3})
                stats = db.one(conn, "SELECT * FROM market_round_stats WHERE city='广州' AND round_no=1")
                self.assertIsNotNone(stats)
                city_results = db.all_rows(conn, "SELECT price,sold FROM city_results WHERE city='广州' AND round_no=1")
                expected_average = weighted_market_average(9800, 80_000, [(row["price"], row["sold"]) for row in city_results])
                self.assertAlmostEqual(stats["average_price"], expected_average)
                hidden_rows = db.one(conn, "SELECT COUNT(*) AS n FROM city_results WHERE city='成都' AND round_no=1")
                self.assertEqual(hidden_rows["n"], 0)

    def test_weighted_market_average_blends_unserved_demand(self) -> None:
        from sim.engine import weighted_market_average

        average = weighted_market_average(100, 1_000, [(80, 100), (120, 200)])
        self.assertAlmostEqual(average, 102.0)
        self.assertEqual(weighted_market_average(100, 1_000, []), 100)

    def test_finance_helpers_follow_kds_rules(self) -> None:
        from sim.engine import available_loan_limit, spend, weighted_salary_average

        self.assertEqual(available_loan_limit(7_500_000, 15_000_000, 1_000_000, 6_000_000), 3_000_000)
        self.assertEqual(available_loan_limit(-1, 15_000_000, 1_000_000, 6_000_000), 1_000_000)
        self.assertEqual(available_loan_limit(50_000_000, 15_000_000, 0, 6_000_000), 6_000_000)
        self.assertEqual(weighted_salary_average([(10, 3900, 3300), (20, 3000, 3300)], 3300), 3300)
        self.assertEqual(spend(400_000, 600_000), (0, 400_000))

    def test_loan_limit_is_a_per_round_new_loan_amount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "loan.db")
            from sim import db
            from sim.engine import settle_round

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                for index, company in enumerate(companies):
                    conn.execute(
                        "UPDATE companies SET home_city='广州',setup_submitted_at=?,debt=? WHERE id=?",
                        (db.now_iso(), 2_000_000 if index == 0 else 0, company["id"]),
                    )
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,loan_change,worker_salary,engineer_salary,submitted_at) VALUES(?,1,?,?,?,?)",
                        (company["id"], 5_000_000 if index == 0 else 0, 3300, 6400, db.now_iso()),
                    )
                settle_round(conn, 1)
                first = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (companies[0]["id"],))["report_json"])
                self.assertAlmostEqual(first["finance"]["loan_base_net_assets"], 13_000_000)
                self.assertAlmostEqual(first["finance"]["loan_limit"], 5_200_000)
                self.assertAlmostEqual(first["finance"]["loan_change"], 5_000_000)
                self.assertAlmostEqual(first["key_metrics"]["debt"], 7_210_000)

    def test_patent_reduces_material_cost_starting_next_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "patent.db")
            from sim import db
            from sim.engine import settle_round

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                for company in companies:
                    conn.execute("UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?", (db.now_iso(), company["id"]))
                    conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company["id"],))
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,worker_delta,worker_salary,engineer_delta,engineer_salary,production_volume,research_investment,submitted_at) VALUES(?,1,3,3300,4,6400,1,?,?)",
                        (company["id"], 1_500_000 if company["id"] == companies[0]["id"] else 0, db.now_iso()),
                    )
                    conn.execute("INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,1,'广州',9800)", (company["id"],))
                settle_round(conn, 1)
                first = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=1", (companies[0]["id"],))["report_json"])
                self.assertTrue(first["research"]["success"])
                self.assertEqual(first["research"]["active_patents_this_round"], 0)

                conn.execute("INSERT INTO rounds(round_no,status) VALUES(2,'open')")
                for company in companies:
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,production_volume,submitted_at) VALUES(?,2,3300,6400,1,?)",
                        (company["id"], db.now_iso()),
                    )
                    conn.execute("INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,2,'广州',9800)", (company["id"],))
                settle_round(conn, 2)
                second = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no=2", (companies[0]["id"],))["report_json"])
                self.assertEqual(second["research"]["active_patents_this_round"], 1)
                self.assertAlmostEqual(first["finance"]["materials"], 2750)
                self.assertAlmostEqual(second["finance"]["materials"], 1925)

    def test_admin_can_delete_players_and_cities_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "delete.db")
            from sim import db

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                company = db.one(conn, "SELECT * FROM companies ORDER BY id LIMIT 1")
                conn.execute("UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?", (db.now_iso(), company["id"]))
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company["id"],))
                conn.execute(
                    "INSERT INTO city_results(company_id,round_no,city,cpi,cpi_units,sold,revenue,price,marketing,market_share,breakdown_json) "
                    "VALUES(?,1,'广州',1,1,1,1,1,1,1,'{}')",
                    (company["id"],),
                )
                db.delete_city(conn, "广州")
                refreshed = db.one(conn, "SELECT home_city,setup_submitted_at FROM companies WHERE id=?", (company["id"],))
                self.assertIsNone(refreshed["home_city"])
                self.assertIsNone(refreshed["setup_submitted_at"])
                self.assertIsNone(db.one(conn, "SELECT city FROM market_config WHERE city='广州'"))
                self.assertEqual(db.one(conn, "SELECT COUNT(*) AS n FROM city_results WHERE city='广州'")["n"], 0)
                self.assertEqual(db.one(conn, "SELECT COUNT(*) AS n FROM agents WHERE city='广州'")["n"], 0)

                db.delete_company(conn, int(company["id"]))
                self.assertIsNone(db.one(conn, "SELECT id FROM companies WHERE id=?", (company["id"],)))
                self.assertEqual(db.get_setting(conn, "total_rounds", 0, int), 5)


if __name__ == "__main__":
    unittest.main()
