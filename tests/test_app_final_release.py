from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class FinalResultsReleaseAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-final-release-")
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"SIM_DB_PATH": str(Path(self.temp.name) / "final-release.db")},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        from sim import db
        from sim.engine import settle_round

        self.db = db
        previous_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", previous_path)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()
        with db.connect() as conn:
            db.set_setting(conn, "total_rounds", 1)
            conn.execute(
                "UPDATE rounds SET status='open',starts_at=? WHERE round_no=1",
                (db.now_iso(),),
            )
            companies = db.all_rows(conn, "SELECT * FROM companies ORDER BY id")
            self.company_id = int(companies[0]["id"])
            for company in companies:
                conn.execute(
                    "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                    (db.now_iso(), company["id"]),
                )
                conn.execute(
                    "INSERT INTO agents(company_id,city,count) VALUES(?,'广州',1)",
                    (company["id"],),
                )
                conn.execute(
                    "INSERT INTO decisions(company_id,round_no,worker_salary,engineer_salary,submitted_at) "
                    "VALUES(?,1,3300,6400,?)",
                    (company["id"], db.now_iso()),
                )
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,price) VALUES(?,1,'广州',25000)",
                    (company["id"],),
                )
            settle_round(conn, 1)

        self.app_path = str(Path(__file__).resolve().parents[1] / "app.py")

    def test_admin_releases_final_round_to_players(self) -> None:
        player_before = AppTest.from_file(self.app_path, default_timeout=30)
        player_before.session_state["auth"] = {
            "role": "player",
            "company_id": self.company_id,
        }
        player_before.run()
        self.assertFalse(player_before.exception)
        self.assertTrue(any("The End" in item.value for item in player_before.markdown))
        self.assertTrue(any("Initial Assets" in item.value for item in player_before.markdown))

        admin = AppTest.from_file(self.app_path, default_timeout=30)
        admin.session_state["auth"] = {"role": "admin"}
        admin.run()
        admin.sidebar.radio[0].set_value("回合控制").run()
        release_button = next(
            item for item in admin.button if item.label == "释放最终结果、排名与报表"
        )
        release_button.click().run()
        self.assertFalse(admin.exception)
        with self.db.connect() as conn:
            self.assertEqual(
                self.db.get_setting(conn, "final_results_release_round", 0, int),
                1,
            )

        player_after = AppTest.from_file(self.app_path, default_timeout=30)
        player_after.session_state["auth"] = {
            "role": "player",
            "company_id": self.company_id,
        }
        player_after.run()
        self.assertFalse(player_after.exception)
        self.assertFalse(any("The End" in item.value for item in player_after.markdown))
        self.assertTrue(any("Round 1 Summary" in item.value for item in player_after.markdown))


if __name__ == "__main__":
    unittest.main()
