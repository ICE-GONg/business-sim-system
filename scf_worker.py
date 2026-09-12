"""Tencent SCF HTTP worker for the expensive Super Bot pass.

Package this file together with the ``sim`` package and deploy it as a Python
Web Function.  It deliberately accepts a database snapshot instead of any
cloud credentials; the Streamlit app keeps the authoritative database.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

# sim.db initializes its default database when first imported. SCF mounts the
# deployment directory read-only, so give that import a stable writable path.
# Every calculation still uses its own explicit, isolated SQLite connection.
os.environ["SIM_DB_PATH"] = str(Path(tempfile.gettempdir()) / "super-bot-worker-bootstrap" / "sim.db")


def _body(event):
    body = event.get("body", event) if isinstance(event, dict) else event
    if isinstance(body, str):
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return json.loads(body)
    return body


def main_handler(event, context):
    started = time.perf_counter()
    if isinstance(event, dict) and event.get("httpMethod", event.get("requestContext", {}).get("http", {}).get("method")) == "GET":
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": True, "protocol": 2, "service": "super-bot-worker"})}
    try:
        payload = _body(event)
        expected = os.environ.get("SUPER_BOT_REMOTE_TOKEN", "")
        supplied = str(payload.get("token") or "")
        if not expected:
            return {"statusCode": 503, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": False, "error": "worker authentication is not configured"})}
        if not hmac.compare_digest(supplied, expected):
            return {"statusCode": 403, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": False, "error": "forbidden"})}
        if payload.get("protocol") == 2:
            from sim.remote_worker import decode_snapshot, load_snapshot, run_remote_request
            with tempfile.TemporaryDirectory() as directory:
                with sqlite3.connect(Path(directory) / "sim.db") as conn:
                    conn.row_factory = sqlite3.Row
                    load_snapshot(conn, decode_snapshot(payload))
                    loaded = time.perf_counter()
                    result = run_remote_request(conn, payload)
                    result["timings"].update(decode_seconds=loaded-started, worker_seconds=time.perf_counter()-started)
                conn.close()
            return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(result)}
        raw = base64.b64decode(payload["db_b64"])
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "sim.db"
            db_path.write_bytes(raw)
            # Compatibility for existing app versions during cloud-first
            # rollout. New requests return only scoped decision rows.
            from sim.bots import submit_super_bot_decisions
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                submitted = submit_super_bot_decisions(
                    conn,
                    int(payload["round_no"]),
                    replace_existing=bool(payload.get("replace_existing", False)),
                )
                conn.commit()
            conn.close()  # Checkpoint a legacy snapshot that uses WAL mode.
            result = {
                "ok": True,
                "submitted": int(submitted),
                "db_b64": base64.b64encode(db_path.read_bytes()).decode("ascii"),
            }
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(result)}
    except Exception as exc:  # SCF serializes this into the app log only.
        return {"statusCode": 500, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"ok": False, "error": str(exc)})}
