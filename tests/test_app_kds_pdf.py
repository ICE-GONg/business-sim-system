from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class PublicKDSPDFAppTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="business-sim-kds-pdf-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "kds-pdf.db"
        environment = patch.dict(os.environ, {"SIM_DB_PATH": str(path)})
        environment.start()
        self.addCleanup(environment.stop)
        from sim import db

        database_path = patch.object(db, "DB_PATH", path)
        database_path.start()
        self.addCleanup(database_path.stop)
        db.init_db()
        with db.connect() as conn:
            self.company_id = int(db.one(conn, "SELECT id FROM companies ORDER BY id LIMIT 1")["id"])
            conn.execute(
                "UPDATE companies SET home_city='广州',setup_submitted_at=? WHERE id=?",
                (db.now_iso(), self.company_id),
            )
            db.set_setting(conn, "initial_cash", 19_876_543)
            db.set_setting(conn, "research_probability_cap", 0.123456789)
        self.app_path = str(Path(__file__).resolve().parents[1] / "app.py")

    def assert_download(self, app, key):
        self.assertFalse(app.exception, [item.message for item in app.exception])
        buttons = app.get("download_button")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].proto.label, "下载公开 KDS PDF")
        self.assertTrue(buttons[0].proto.id.endswith(key))
        self.assertTrue(buttons[0].proto.url)
        self.assertFalse(buttons[0].proto.disabled)

    def test_admin_and_player_download_the_same_public_pdf(self):
        from sim.kds_pdf import build_public_kds_pdf

        generated = []

        def capture_pdf(*args, **kwargs):
            pdf = build_public_kds_pdf(*args, **kwargs)
            generated.append(pdf)
            return pdf

        with patch("sim.kds_pdf.build_public_kds_pdf", side_effect=capture_pdf):
            admin = AppTest.from_file(self.app_path, default_timeout=30)
            admin.session_state["auth"] = {"role": "admin"}
            admin.run()
            self.assertFalse(admin.exception)
            admin.sidebar.radio[0].set_value("KDS 设置").run()
            self.assert_download(admin, "admin_public_kds_pdf")

            player = AppTest.from_file(self.app_path, default_timeout=30)
            player.session_state["auth"] = {"role": "player", "company_id": self.company_id}
            player.session_state["player_navigation"] = "规则"
            player.run()
            self.assert_download(player, "player_public_kds_pdf")

        self.assertEqual(len(generated), 2)
        self.assertTrue(generated[0].startswith(b"%PDF-"))
        self.assertEqual(generated[0], generated[1])


if __name__ == "__main__":
    unittest.main()
