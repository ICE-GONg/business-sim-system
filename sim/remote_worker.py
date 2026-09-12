"""Incremental SCF transport; the application database stays authoritative."""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from .defaults import DEFAULT_SETTINGS

PROTOCOL = 2
TABLES = ("settings", "companies", "employee_cohorts", "market_config", "rounds",
          "decisions", "city_decisions", "agents", "results", "city_results", "market_round_stats")
_LOCK = threading.Lock()
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class RemoteWorkerError(ValueError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _state(conn: sqlite3.Connection) -> dict:
    """Only computation inputs: no login hashes, backups or deployment secrets."""
    state = {}
    for table in TABLES:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if schema is None:
            continue
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")') if r[1] != "password_hash"]
        quoted = ",".join('"' + c + '"' for c in columns)
        records = [list(row) for row in conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid')]
        if table == "settings":
            records = [r for r in records if r[columns.index("key")] in DEFAULT_SETTINGS]
        # Build a fresh DB on the worker; removed secrets cannot survive in
        # SQLite free pages as they could in an edited database backup.
        state[table] = {"schema": schema[0], "columns": columns, "rows": records}
    return state


def _snapshot(conn: sqlite3.Connection) -> tuple[bytes, str]:
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        raw = _json(_state(conn))
    finally:
        if own_transaction:
            conn.rollback()
    return raw, hashlib.sha256(raw).hexdigest()


def _check_round(conn: sqlite3.Connection, round_no: int) -> str:
    row = conn.execute("SELECT round_no,status,starts_at FROM rounds WHERE status IN ('waiting','open','paused') ORDER BY round_no DESC LIMIT 1").fetchone()
    if not row or int(row[0]) != round_no or row[1] not in ("open", "paused"):
        raise RemoteWorkerError("本轮已改变或已结算，请刷新后继续。")
    return str(row[2] or "")


def create_remote_request(conn: sqlite3.Connection, round_no: int, *, phase: str = "bot",
                          company_id: int | None = None, replace_existing: bool = False) -> dict:
    _check_round(conn, round_no)
    if phase not in ("bot", "rebalance") or (phase == "bot" and company_id is None):
        raise RemoteWorkerError("无效的远程计算阶段")
    raw, fingerprint = _snapshot(conn)
    return {"protocol": PROTOCOL, "request_id": uuid.uuid4().hex, "snapshot_hash": fingerprint,
            "snapshot_b64": base64.b64encode(gzip.compress(raw, compresslevel=6, mtime=0)).decode(),
            "round_no": int(round_no), "phase": phase, "company_id": company_id,
            "replace_existing": bool(replace_existing)}


def decode_snapshot(payload: dict) -> dict:
    packed = base64.b64decode(payload["snapshot_b64"], validate=True)
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise RemoteWorkerError("计算快照过大")
    if hashlib.sha256(raw).hexdigest() != payload.get("snapshot_hash"):
        raise RemoteWorkerError("计算快照校验失败")
    state = json.loads(raw)
    if set(state) - set(TABLES):
        raise RemoteWorkerError("计算快照含无关数据表")
    return state


def load_snapshot(conn: sqlite3.Connection, state: dict) -> None:
    for table, data in state.items():
        conn.execute(data["schema"])
        columns = list(data["columns"])
        rows = list(data["rows"])
        if table == "companies":
            columns.append("password_hash")
            rows = [list(row) + [""] for row in rows]
        quoted = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        conn.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in columns)})', rows)
    conn.commit()


def _rows(conn: sqlite3.Connection, table: str, round_no: int, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE round_no=? AND company_id IN ({",".join("?" for _ in ids)}) ORDER BY company_id', (round_no, *ids))
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


def run_remote_request(conn: sqlite3.Connection, payload: dict) -> dict:
    from .bots import rebalance_super_bot_decisions, submit_super_bot_decisions
    started = time.perf_counter()
    round_no = int(payload["round_no"])
    _check_round(conn, round_no)
    phase = payload["phase"]
    if phase == "bot":
        ids = [int(payload["company_id"])]
        if not conn.execute("SELECT 1 FROM companies WHERE id=? AND is_bot=1 AND is_super_bot=1", ids).fetchone():
            raise RemoteWorkerError("待计算超级 Bot 已不存在")
        submitted = submit_super_bot_decisions(conn, round_no,
            replace_existing=bool(payload.get("replace_existing")), target_ids=set(ids), defer_rebalance=True)
    elif phase == "rebalance":
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1 ORDER BY id")]
        rebalance_super_bot_decisions(conn, round_no)
        submitted = 0
    else:
        raise RemoteWorkerError("无效的远程计算阶段")
    conn.commit()
    return {"ok": True, "protocol": PROTOCOL, "request_id": payload["request_id"],
            "snapshot_hash": payload["snapshot_hash"], "round_no": round_no, "phase": phase,
            "company_ids": ids, "submitted": submitted,
            "decisions": _rows(conn, "decisions", round_no, ids),
            "city_decisions": _rows(conn, "city_decisions", round_no, ids),
            "timings": {"compute_seconds": time.perf_counter() - started}}


def apply_remote_result(conn: sqlite3.Connection, request: dict, result: dict,
                        *, commit: bool = True) -> int:
    if not result.get("ok"):
        raise RemoteWorkerError(str(result.get("error") or "远程计算失败"))
    for key in ("protocol", "request_id", "snapshot_hash", "round_no", "phase"):
        if result.get(key) != request.get(key):
            raise RemoteWorkerError("远程结果与本次任务不匹配，未写入")
    if conn.in_transaction:
        raise RemoteWorkerError("远程结果需使用独立短事务保存")
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_no = int(request["round_no"])
        _check_round(conn, round_no)
        if _snapshot(conn)[1] != request["snapshot_hash"]:
            raise RemoteWorkerError("分析期间玩家、管理员或轮次数据已改变，保留现有决策，请重新分析未完成的 Bot。")
        allowed_ids = ([int(request["company_id"])] if request["phase"] == "bot" else
                       [int(r[0]) for r in conn.execute("SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1 ORDER BY id")])
        if sorted(result["company_ids"]) != sorted(allowed_ids):
            raise RemoteWorkerError("远程结果超出本次 Bot 范围")
        if {int(r["company_id"]) for r in result["decisions"]} != set(allowed_ids) or len(result["decisions"]) != len(allowed_ids):
            raise RemoteWorkerError("远程决策不完整")
        expected_cities = {(company_id, r[0]) for company_id in allowed_ids
                           for r in conn.execute("SELECT city FROM market_config")}
        actual_cities = [(r["company_id"], r["city"]) for r in result["city_decisions"]]
        if set(actual_cities) != expected_cities or len(actual_cities) != len(expected_cities):
            raise RemoteWorkerError("远程城市决策不完整")
        for table in ("decisions", "city_decisions"):
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            for row in result[table]:
                if set(row) != set(columns) or int(row["company_id"]) not in allowed_ids or row["round_no"] != round_no:
                    raise RemoteWorkerError("远程决策字段或所属轮次不正确")
                if any(isinstance(v, float) and not math.isfinite(v) for v in row.values()):
                    raise RemoteWorkerError("远程决策含无效数值")
            for company_id in allowed_ids:
                conn.execute(f'DELETE FROM "{table}" WHERE company_id=? AND round_no=?', (company_id, round_no))
            quoted = ",".join('"' + c + '"' for c in columns)
            conn.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in columns)})',
                             [[r[c] for c in columns] for r in result[table]])
        if commit:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return int(result.get("submitted", 0))


def _job_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS super_bot_remote_jobs(round_no INTEGER PRIMARY KEY, round_start TEXT NOT NULL, pending_json TEXT NOT NULL, total INTEGER NOT NULL, replace_existing INTEGER NOT NULL, phase TEXT NOT NULL)")
    conn.commit()


def remote_pending(conn: sqlite3.Connection, round_no: int) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='super_bot_remote_jobs'").fetchone():
        return False
    row = conn.execute("SELECT phase,round_start FROM super_bot_remote_jobs WHERE round_no=?", (round_no,)).fetchone()
    current = conn.execute("SELECT starts_at FROM rounds WHERE round_no=? AND status IN ('open','paused')", (round_no,)).fetchone()
    return bool(row and current and row[0] != "done" and row[1] == str(current[0] or ""))


def _post(endpoint: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(endpoint, data=_json(dict(payload, token=token)),
        headers={"Content-Type": "application/json", "X-Super-Bot-Token": token}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=860) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RemoteWorkerError("远程计算连接中断；已保存的 Bot 决策不受影响，请继续未完成的分析。") from exc
    if "statusCode" in result and "body" in result:
        result = json.loads(result["body"]) if isinstance(result["body"], str) else result["body"]
    return result


def remote_submit(conn: sqlite3.Connection, endpoint: str, token: str, round_no: int,
                  replace_existing: bool = False,
                  progress_callback: Callable[[int, int, str], None] | None = None,
                  timing_callback: Callable[[dict], None] | None = None) -> int:
    if not token:
        raise RemoteWorkerError("未配置超级 Bot 远程鉴权令牌")
    if not endpoint.startswith("https://"):
        raise RemoteWorkerError("远程计算地址必须使用 HTTPS")
    if not _LOCK.acquire(blocking=False):
        raise RemoteWorkerError("已有超级 Bot 分析正在进行，请等待当前任务完成。")
    try:
        round_start = _check_round(conn, round_no)
        _job_table(conn)
        if not remote_pending(conn, round_no):
            query = "SELECT c.id FROM companies c WHERE c.is_bot=1 AND c.is_super_bot=1"
            if not replace_existing:
                query += " AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=?)"
            ids = [int(r[0]) for r in conn.execute(query + " ORDER BY c.id", () if replace_existing else (round_no,))]
            conn.execute("INSERT OR REPLACE INTO super_bot_remote_jobs VALUES(?,?,?,?,?,?)",
                         (round_no, round_start, json.dumps(ids), len(ids), int(replace_existing), "bot" if ids else "rebalance"))
            conn.commit()
        job = conn.execute("SELECT pending_json,total,replace_existing,phase FROM super_bot_remote_jobs WHERE round_no=?", (round_no,)).fetchone()
        pending, total, replace = json.loads(job[0]), int(job[1]), bool(job[2])
        # Admin deletion or conversion to a normal player must not strand a
        # persisted job forever on an id that no longer represents a Super Bot.
        eligible = {int(r[0]) for r in conn.execute("SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1")}
        active_pending = [company_id for company_id in pending if company_id in eligible]
        if active_pending != pending:
            total -= len(pending) - len(active_pending)
            pending = active_pending
            conn.execute("UPDATE super_bot_remote_jobs SET pending_json=?,total=? WHERE round_no=?", (json.dumps(pending), total, round_no))
            conn.commit()
        submitted = 0
        while True:
            phase = "bot" if pending else "rebalance"
            if progress_callback:
                stage = "联合复算中"
                if pending:
                    row = conn.execute("SELECT code FROM companies WHERE id=?", (pending[0],)).fetchone()
                    stage = f"{row[0] if row else pending[0]} 分析中"
                progress_callback(total - len(pending), max(1, total), stage)
            started = time.perf_counter()
            payload = create_remote_request(conn, round_no, phase=phase,
                company_id=pending[0] if pending else None, replace_existing=replace)
            sent = time.perf_counter()
            result = _post(endpoint, token, payload)
            received = time.perf_counter()
            submitted += apply_remote_result(conn, payload, result, commit=False)
            try:
                code = "联合复算"
                if pending:
                    code = str(conn.execute("SELECT code FROM companies WHERE id=?", (pending[0],)).fetchone()[0])
                    pending = pending[1:]
                conn.execute("UPDATE super_bot_remote_jobs SET pending_json=?,phase=? WHERE round_no=?",
                    (json.dumps(pending), "done" if phase == "rebalance" else "bot" if pending else "rebalance", round_no))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            timings = dict(result.get("timings", {}), phase=phase, company_id=payload["company_id"],
                request_bytes=len(payload["snapshot_b64"]), snapshot_seconds=sent-started,
                http_seconds=received-sent, save_seconds=time.perf_counter()-received,
                total_seconds=time.perf_counter()-started)
            if timing_callback:
                timing_callback(timings)
            if progress_callback:
                progress_callback(total - len(pending), max(1, total), code)
            if phase == "rebalance":
                return submitted
    finally:
        _LOCK.release()
