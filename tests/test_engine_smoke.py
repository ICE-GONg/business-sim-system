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
            from sim.report_pdf import build_round_report_pdf

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
                pdf_bytes = build_round_report_pdf(dict(companies[0]), 1, report, 1, [])
                self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
                self.assertGreater(len(pdf_bytes), 7_000)
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
        from sim.engine import weighted_market_average, weighted_player_average

        average = weighted_market_average(100, 1_000, [(80, 100), (120, 200)])
        self.assertAlmostEqual(average, 102.0)
        self.assertEqual(weighted_market_average(100, 1_000, []), 100)
        self.assertAlmostEqual(weighted_player_average([(80, 100), (120, 200)], 100), 106.6666666667)
        self.assertEqual(weighted_player_average([], 100), 100)

    def test_cpi_uses_sales_weighted_player_price_not_market_base_price(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "player-average.db")
            from sim import db
            from sim.engine import settle_round

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                for index, company in enumerate(companies):
                    conn.execute(
                        "UPDATE companies SET home_city='广州',setup_submitted_at=?,product_inventory=? WHERE id=?",
                        (db.now_iso(), 200 if index < 2 else 0, company["id"]),
                    )
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,submitted_at) VALUES(?,1,3300,6400,?)",
                        (company["id"], db.now_iso()),
                    )
                    if index < 2:
                        conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'无锡',1)", (company["id"],))
                        conn.execute(
                            "INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,1,'无锡',24888)",
                            (company["id"],),
                        )

                settle_round(conn, 1)

                rows = db.all_rows(conn, "SELECT breakdown_json FROM city_results WHERE city='无锡' AND round_no=1 ORDER BY company_id")
                self.assertEqual(len(rows), 2)
                for row in rows:
                    breakdown = json.loads(row["breakdown_json"])
                    self.assertAlmostEqual(breakdown["average_price"], 24_888)
                    self.assertAlmostEqual(breakdown["market_average_price"], 7_600)

    def test_one_product_split_across_positive_cpi_markets_is_not_lost(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "whole-unit-sales.db")
            from sim import db
            from sim.engine import allocate_integer_sales, settle_round

            self.assertEqual(allocate_integer_sales({"南京": 0.7, "无锡": 0.3}, 1), {"南京": 1, "无锡": 0})
            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                for index, company in enumerate(companies):
                    conn.execute(
                        "UPDATE companies SET home_city='广州',setup_submitted_at=?,product_inventory=? WHERE id=?",
                        (db.now_iso(), 1 if index == 0 else 0, company["id"]),
                    )
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,submitted_at) VALUES(?,1,3300,6400,?)",
                        (company["id"], db.now_iso()),
                    )
                first_id = int(companies[0]["id"])
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'南京',1)", (first_id,))
                conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'无锡',1)", (first_id,))

                settle_round(conn, 1)

                result = db.one(conn, "SELECT sold,inventory FROM results WHERE company_id=? AND round_no=1", (first_id,))
                self.assertEqual(result["sold"], 1)
                self.assertEqual(result["inventory"], 0)
                city_rows = db.all_rows(conn, "SELECT cpi,sold FROM city_results WHERE company_id=? AND round_no=1", (first_id,))
                self.assertTrue(all(float(row["cpi"]) > 0 for row in city_rows))
                self.assertEqual(sum(int(row["sold"]) for row in city_rows), 1)

    def test_finance_helpers_follow_kds_rules(self) -> None:
        from sim.engine import available_loan_limit, proportional_quits, redistribute_unused_cpi, spend, weighted_salary_average

        self.assertEqual(available_loan_limit(7_500_000, 15_000_000, 1_000_000, 6_000_000), 3_000_000)
        self.assertEqual(available_loan_limit(-1, 15_000_000, 1_000_000, 6_000_000), 1_000_000)
        self.assertEqual(available_loan_limit(50_000_000, 15_000_000, 0, 6_000_000), 6_000_000)
        self.assertEqual(weighted_salary_average([(10, 3900, 3300), (20, 3000, 3300)], 3300), 3300)
        self.assertEqual(proportional_quits(100, 2_300, 3_300), 30)
        self.assertEqual(spend(400_000, 600_000), (0, 400_000))
        secondary = redistribute_unused_cpi(
            {1: 22, 2: 18, 3: 0},
            {1: 8, 2: 18, 3: 0},
            {1: 0, 2: 50, 3: 50},
        )
        self.assertAlmostEqual(secondary[2], 14)
        self.assertEqual(secondary[1], 0)
        self.assertEqual(secondary[3], 0)

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

                conn.execute("INSERT INTO rounds(round_no,status) VALUES(2,'open')")
                for company in companies:
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,loan_change,worker_salary,engineer_salary,submitted_at) "
                        "VALUES(?,2,20000000,3300,6400,?)",
                        (company["id"], db.now_iso()),
                    )
                settle_round(conn, 2)
                second = json.loads(db.one(
                    conn,
                    "SELECT report_json FROM results WHERE company_id=? AND round_no=2",
                    (companies[0]["id"],),
                )["report_json"])
                self.assertAlmostEqual(second["finance"]["loan_ceiling"], 10_000_000)
                self.assertAlmostEqual(
                    second["finance"]["loan_limit"],
                    min(10_000_000, first["key_metrics"]["net_assets"] / 15_000_000 * 10_000_000),
                )

    def test_salary_average_and_quits_are_independent_per_home_market(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "salary-home.db")
            from sim import db
            from sim.engine import settle_round

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                conn.execute("UPDATE rounds SET status='open' WHERE round_no=1")
                homes = ["广州", "广州", "大连", "大连"]
                worker_salaries = [2_300, 4_300, 2_300, 2_300]
                for company, home, worker_salary in zip(companies, homes, worker_salaries):
                    conn.execute(
                        "UPDATE companies SET home_city=?,setup_submitted_at=? WHERE id=?",
                        (home, db.now_iso(), company["id"]),
                    )
                    conn.execute(
                        "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,'worker',100,0)",
                        (company["id"],),
                    )
                    engineer_salary = 6_400 if home == "广州" else 4_400
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,submitted_at) VALUES(?,1,?,?,?)",
                        (company["id"], worker_salary, engineer_salary, db.now_iso()),
                    )
                settle_round(conn, 1)
                guangzhou = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=?", (companies[0]["id"],))["report_json"])
                dalian = json.loads(db.one(conn, "SELECT report_json FROM results WHERE company_id=?", (companies[2]["id"],))["report_json"])
                self.assertAlmostEqual(guangzhou["human_resources"]["average_worker_salary"], 3_300)
                self.assertAlmostEqual(dalian["human_resources"]["average_worker_salary"], 2_300)
                self.assertEqual(guangzhou["human_resources"]["workers"], 70)
                self.assertEqual(sum(row["quitted"] for row in guangzhou["human_resources"]["rows"]), 30)
                self.assertEqual(guangzhou["finance"]["quit_penalty"], 138_000)
                self.assertEqual(dalian["human_resources"]["workers"], 100)

    def test_optional_test_round_restores_state_before_official_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "test-round.db")
            from sim import db
            from sim.engine import current_company_net_assets, settle_round

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
                for company in companies:
                    conn.execute(
                        "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                        (db.now_iso(), company["id"]),
                    )
                    conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)", (company["id"],))
                self.assertEqual(db.start_competition(conn, 5, True), -1)
                for company in companies:
                    conn.execute(
                        "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_salary,submitted_at) "
                        "VALUES(?,-1,1000000,2,3300,6400,?)",
                        (company["id"], db.now_iso()),
                    )
                    conn.execute(
                        "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,price) VALUES(?,-1,'广州',1,9800)",
                        (company["id"],),
                    )
                settle_round(conn, -1)
                first_id = int(companies[0]["id"])
                self.assertEqual(db.employee_count(conn, first_id, "worker"), 2)
                self.assertEqual(db.one(conn, "SELECT count FROM agents WHERE company_id=? AND city='广州'", (first_id,))["count"], 2)

                self.assertEqual(db.prepare_first_round_after_test(conn, 30), 1)
                restored = db.one(conn, "SELECT * FROM companies WHERE id=?", (first_id,))
                self.assertEqual(restored["cash"], 15_000_000)
                self.assertEqual(restored["debt"], 0)
                self.assertEqual(db.employee_count(conn, first_id, "worker"), 0)
                self.assertEqual(db.one(conn, "SELECT count FROM agents WHERE company_id=? AND city='广州'", (first_id,))["count"], 1)
                self.assertIsNotNone(db.one(conn, "SELECT * FROM results WHERE company_id=? AND round_no=-1", (first_id,)))
                self.assertEqual(db.one(conn, "SELECT status FROM rounds WHERE round_no=1")["status"], "open")
                self.assertEqual(current_company_net_assets(conn, restored), 15_000_000)

    def test_kds_png_contains_live_market_costs_at_high_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "kds.db")
            from PIL import Image
            from sim import db
            from sim.kds_image import build_kds_png
            from io import BytesIO

            db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
            db.init_db()
            with db.connect() as conn:
                conn.execute("UPDATE market_config SET transport_cost=88,worker_training_cost=500,engineer_training_cost=900 WHERE city='广州'")
                image_bytes = build_kds_png(db.settings_dict(conn), db.all_rows(conn, "SELECT * FROM market_config ORDER BY city"))
            self.assertTrue(image_bytes.startswith(b"\x89PNG"))
            image = Image.open(BytesIO(image_bytes))
            self.assertEqual(image.width, 2400)
            self.assertGreater(image.height, 2000)

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

    def test_admin_can_rollback_latest_settlement_and_restore_exact_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["SIM_DB_PATH"] = str(Path(temp_dir) / "rollback.db")
            from sim import db
            from sim.engine import settle_round

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
                        "VALUES(?,1,1000000,3,3300,4,6400,5000,2,4000,0,?)",
                        (company["id"], db.now_iso()),
                    )
                    conn.execute(
                        "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price) "
                        "VALUES(?,1,'广州',2,1000,9800)",
                        (company["id"],),
                    )

                first_company_id = int(companies[0]["id"])
                before = dict(db.one(conn, "SELECT * FROM companies WHERE id=?", (first_company_id,)))
                before_agents = [dict(row) for row in db.all_rows(conn, "SELECT city,count FROM agents WHERE company_id=? ORDER BY city", (first_company_id,))]
                settle_round(conn, 1)
                self.assertGreater(db.employee_count(conn, first_company_id, "worker"), 0)
                self.assertEqual(db.one(conn, "SELECT count FROM agents WHERE company_id=? AND city='广州'", (first_company_id,))["count"], 3)
                conn.execute("INSERT INTO rounds(round_no,status) VALUES(2,'open')")
                conn.execute(
                    "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,submitted_at) VALUES(?,2,3300,6400,?)",
                    (first_company_id, db.now_iso()),
                )

                reopened = db.rollback_latest_settled_round(conn, 17)

                self.assertEqual(reopened, 1)
                restored = db.one(conn, "SELECT * FROM companies WHERE id=?", (first_company_id,))
                for field in ("cash", "debt", "patents", "product_inventory", "component_storage_capacity", "product_storage_capacity"):
                    self.assertEqual(restored[field], before[field])
                self.assertEqual(db.employee_count(conn, first_company_id, "worker"), 0)
                self.assertEqual(
                    [dict(row) for row in db.all_rows(conn, "SELECT city,count FROM agents WHERE company_id=? ORDER BY city", (first_company_id,))],
                    before_agents,
                )
                self.assertEqual(db.one(conn, "SELECT status FROM rounds WHERE round_no=1")["status"], "open")
                self.assertIsNone(db.one(conn, "SELECT round_no FROM rounds WHERE round_no=2"))
                self.assertEqual(db.one(conn, "SELECT COUNT(*) AS n FROM results")["n"], 0)
                self.assertIsNone(db.one(conn, "SELECT submitted_at FROM decisions WHERE company_id=? AND round_no=1", (first_company_id,))["submitted_at"])
                self.assertIsNone(db.one(conn, "SELECT round_no FROM decisions WHERE company_id=? AND round_no=2", (first_company_id,)))

                # Existing cloud databases may contain settled rounds created
                # before snapshot support. They still have a safe migration
                # path reconstructed from the previous reports and decisions.
                conn.execute("UPDATE decisions SET submitted_at=? WHERE round_no=1", (db.now_iso(),))
                settle_round(conn, 1)
                conn.execute("DELETE FROM round_snapshots WHERE round_no=1")
                self.assertEqual(db.rollback_latest_settled_round(conn, 9), 1)
                self.assertEqual(db.employee_count(conn, first_company_id, "worker"), 0)
                self.assertEqual(db.one(conn, "SELECT count FROM agents WHERE company_id=? AND city='广州'", (first_company_id,))["count"], 1)


if __name__ == "__main__":
    unittest.main()
