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
                        "INSERT INTO decisions(company_id,round_no,worker_delta,worker_salary,engineer_delta,engineer_salary,"
                        "management_investment,production_volume,quality_investment,research_investment,submitted_at) "
                        "VALUES(?,1,3,3300,4,6400,9100,10,5000,0,?)",
                        (company["id"], db.now_iso()),
                    )
                    conn.execute(
                        "INSERT INTO city_decisions(company_id,round_no,city,marketing_investment,price) VALUES(?,1,'广州',8000000,9800)",
                        (company["id"],),
                    )
                settle_round(conn, 1)
                results = db.all_rows(conn, "SELECT * FROM results ORDER BY company_id")
                self.assertEqual(len(results), 4)
                self.assertTrue(all(0 <= row["sold"] <= row["produced"] for row in results))
                self.assertTrue(all(row["sold"] == 10 for row in results))
                report = json.loads(results[0]["report_json"])
                self.assertIn("previous_workers", report["human_resources"])
                self.assertIn("component_storage_before", report["production"])
                self.assertIn("components", report["production"])
                stats = db.one(conn, "SELECT * FROM market_round_stats WHERE city='广州' AND round_no=1")
                self.assertIsNotNone(stats)
                expected_average = weighted_market_average(9800, 80_000, [(9800, 10)] * 4)
                self.assertAlmostEqual(stats["average_price"], expected_average)

    def test_weighted_market_average_blends_unserved_demand(self) -> None:
        from sim.engine import weighted_market_average

        average = weighted_market_average(100, 1_000, [(80, 100), (120, 200)])
        self.assertAlmostEqual(average, 102.0)
        self.assertEqual(weighted_market_average(100, 1_000, []), 100)


if __name__ == "__main__":
    unittest.main()
