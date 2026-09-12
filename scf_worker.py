"""Tencent SCF HTTP worker for the expensive Super Bot pass.

Package this file together with the ``sim`` package and deploy it as a Python
Web Function.  It deliberately accepts a database snapshot instead of any
cloud credentials; the Streamlit app keeps the authoritative database.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def _body(event):
    body = event.get("body", event) if isinstance(event, dict) else event
    if isinstance(body, str):
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return body


def main_handler(event, context):
    try:
        payload = _body(event)
        expected = os.environ.get("SUPER_BOT_REMOTE_TOKEN", "")
        supplied = str(payload.get("token") or "")
        if expected and supplied != expected:
            return {"statusCode": 403, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": False, "error": "forbidden"})}
        raw = base64.b64decode(payload["db_b64"])
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "sim.db"
            db_path.write_bytes(raw)
            os.environ["SIM_DB_PATH"] = str(db_path)
            # Import after SIM_DB_PATH is set so sim.db points at this request's
            # isolated snapshot rather than a persistent worker file.
            from sim.bots import submit_super_bot_decisions
            from sim import db as sim_db
            sim_db.DB_PATH = db_path
            connect = sim_db.connect
            database_bytes = sim_db.database_bytes

            with connect() as conn:
                submitted = submit_super_bot_decisions(
                    conn,
                    int(payload["round_no"]),
                    replace_existing=bool(payload.get("replace_existing", False)),
                )
            result = {
                "ok": True,
                "submitted": int(submitted),
                "db_b64": base64.b64encode(database_bytes()).decode("ascii"),
            }
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(result)}
    except Exception as exc:  # SCF serializes this into the app log only.
        return {"statusCode": 500, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": False, "error": str(exc)})}
