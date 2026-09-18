from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class PlayerDecisionDraftAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="business-sim-player-draft-")
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(
            os.environ,
            {"SIM_DB_PATH": str(Path(self.temp.name) / "player-draft.db")},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        from sim import db

        self.db = db
        previous_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", previous_path)
        db.DB_PATH = Path(os.environ["SIM_DB_PATH"])
        db.init_db()
        with db.connect() as conn:
            company = db.one(conn, "SELECT id FROM companies ORDER BY id LIMIT 1")
            self.company_id = int(company["id"])
            conn.execute(
                "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                (db.now_iso(), self.company_id),
            )
            conn.execute(
                "UPDATE rounds SET status='open',starts_at=?,ends_at=NULL WHERE round_no=1",
                (db.now_iso(),),
            )
        self.app_path = str(Path(__file__).resolve().parents[1] / "app.py")

    def test_unsubmitted_inputs_survive_page_navigation(self) -> None:
        def navigation(app):
            return next(
                item for item in app.get("button_group")
                if item.key == "player_navigation"
            )

        app = AppTest.from_file(self.app_path, default_timeout=30)
        app.session_state["auth"] = {
            "role": "player",
            "company_id": self.company_id,
        }
        app.run()
        navigation(app).set_value(["决策"]).run()
        self.assertFalse(app.exception)

        prefix = f"player_decision_draft:{self.company_id}:1:"
        production = app.number_input(key=f"{prefix}production_volume")
        management = app.number_input(key=f"{prefix}management_investment")
        marketing = app.number_input(key=f"{prefix}city:广州:marketing_investment")
        navigation(app).set_value(["决策"])
        production.set_value(4321).run()
        navigation(app).set_value(["决策"])
        management.set_value(765_432.0).run()
        navigation(app).set_value(["决策"])
        marketing.set_value(234_567.0).run()

        navigation(app).set_value(["报表"]).run()
        self.assertFalse(app.exception)
        navigation(app).set_value(["决策"]).run()
        self.assertFalse(app.exception)

        self.assertEqual(
            app.number_input(key=f"{prefix}production_volume").value,
            4321,
        )
        self.assertEqual(
            app.number_input(key=f"{prefix}management_investment").value,
            765_432.0,
        )
        self.assertEqual(
            app.number_input(key=f"{prefix}city:广州:marketing_investment").value,
            234_567.0,
        )
        with self.db.connect() as conn:
            self.assertIsNone(
                self.db.one(
                    conn,
                    "SELECT 1 FROM decisions WHERE company_id=? AND round_no=1",
                    (self.company_id,),
                )
            )


if __name__ == "__main__":
    unittest.main()
