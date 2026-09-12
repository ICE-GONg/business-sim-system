"""Incremental SCF transport; the application database stays authoritative."""
from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
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
from concurrent.futures import ThreadPoolExecutor

from .defaults import DEFAULT_SETTINGS

PROTOCOL = 2
REMOTE_API_VERSION = 3
TABLES = ("settings", "companies", "employee_cohorts", "market_config", "rounds",
          "decisions", "city_decisions", "agents", "results", "city_results", "market_round_stats")
_LOCK = threading.Lock()
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_RETRYABLE_HTTP_STATUSES = {408, 429, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530}


class RemoteWorkerError(ValueError):
    pass


class RemoteTransportError(RemoteWorkerError):
    """Only connection failures or a temporarily unavailable worker may retry."""


class RemoteAnalysisChangedError(RemoteWorkerError):
    """Authoritative calculation inputs changed while an analysis was running."""


def resolve_remote_endpoints(primary: str = "", *, urls: Any = None, fallback: str = "") -> list[str]:
    """Keep configured priority; accept a Secrets array or comma/JSON string."""
    values = urls
    if isinstance(values, str):
        values = values.strip()
        if values.startswith("["):
            try:
                values = json.loads(values)
            except json.JSONDecodeError as exc:
                raise RemoteWorkerError("超级 Bot 计算地址列表格式不正确") from exc
        else:
            values = values.split(",") if values else []
    if values is None:
        values = []
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
        raise RemoteWorkerError("超级 Bot 计算地址列表必须是字符串数组或逗号分隔地址")
    endpoints = [value.strip() for value in values if value.strip()]
    if not endpoints and primary.strip():
        endpoints.append(primary.strip())
    if fallback.strip():
        endpoints.append(fallback.strip())
    return list(dict.fromkeys(endpoints))


def remote_health(endpoints: str | list[str] | tuple[str, ...], timeout: float = 4.0) -> list[dict]:
    """Read public health only; never send an auth token or competition data."""
    urls = resolve_remote_endpoints(urls=endpoints)

    def check(item: tuple[int, str]) -> dict:
        index, url = item
        started = time.perf_counter()
        status = {"line": index + 1, "ok": False, "message": "连接失败或超时"}
        try:
            if not url.startswith("https://"):
                raise ValueError("HTTPS required")
            request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read(8192))
            if isinstance(result, dict) and "statusCode" in result and "body" in result:
                result = json.loads(result["body"]) if isinstance(result["body"], str) else result["body"]
            if isinstance(result, dict) and result.get("ok") and result.get("protocol") == PROTOCOL:
                status.update(ok=True, message="已连通")
            else:
                status["message"] = "响应不是兼容的计算服务"
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, http.client.HTTPException):
            pass
        status["seconds"] = time.perf_counter() - started
        return status

    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        return list(pool.map(check, enumerate(urls)))


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


def _job_fingerprints(conn: sqlite3.Connection, round_no: int) -> tuple[str, str]:
    """Return stable analysis-input and exact-state fingerprints.

    A segmented job intentionally changes current-round Super Bot decisions after
    each completed segment.  Those rows therefore cannot be part of the stable
    input fingerprint, but the state checkpoint still records them so an
    administrator edit to an already saved Bot is detected as well. Round timer
    and pause metadata are ignored because they do not affect Bot calculations;
    the round incarnation itself is checked separately through ``starts_at``.
    """
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        state = _state(conn)
        fingerprint_state = dict(state)
        round_data = state.get("rounds")
        if round_data:
            ignored = {"status", "starts_at", "ends_at", "settled_at"}
            kept_indexes = [
                index for index, column in enumerate(round_data["columns"])
                if column not in ignored
            ]
            fingerprint_state["rounds"] = {
                "schema": round_data["schema"],
                "columns": [round_data["columns"][index] for index in kept_indexes],
                "rows": [
                    [row[index] for index in kept_indexes]
                    for row in round_data["rows"]
                ],
            }
        state_raw = _json(fingerprint_state)
        super_ids = {
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1"
            )
        }
        input_state = dict(fingerprint_state)
        for table in ("decisions", "city_decisions"):
            data = fingerprint_state.get(table)
            if not data:
                continue
            columns = list(data["columns"])
            round_index = columns.index("round_no")
            company_index = columns.index("company_id")
            rows = [
                row for row in data["rows"]
                if not (
                    int(row[round_index]) == int(round_no)
                    and int(row[company_index]) in super_ids
                )
            ]
            input_state[table] = {
                "schema": data["schema"],
                "columns": columns,
                "rows": rows,
            }
        input_hash = hashlib.sha256(_json(input_state)).hexdigest()
        state_hash = hashlib.sha256(state_raw).hexdigest()
    finally:
        if own_transaction:
            conn.rollback()
    return input_hash, state_hash


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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS super_bot_remote_jobs("
        "round_no INTEGER PRIMARY KEY,round_start TEXT NOT NULL,pending_json TEXT NOT NULL,"
        "total INTEGER NOT NULL,replace_existing INTEGER NOT NULL,phase TEXT NOT NULL,"
        "input_fingerprint TEXT NOT NULL DEFAULT '',state_fingerprint TEXT NOT NULL DEFAULT '')"
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(super_bot_remote_jobs)")
    }
    if "input_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE super_bot_remote_jobs "
            "ADD COLUMN input_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "state_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE super_bot_remote_jobs "
            "ADD COLUMN state_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


def remote_pending(conn: sqlite3.Connection, round_no: int) -> bool:
    current = conn.execute("SELECT starts_at FROM rounds WHERE round_no=? AND status IN ('open','paused')", (round_no,)).fetchone()
    if not current:
        return False
    tracked = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='super_bot_remote_jobs'"
    ).fetchone()
    if not tracked:
        return bool(conn.execute(
            "SELECT 1 FROM companies c JOIN decisions d ON d.company_id=c.id "
            "WHERE c.is_bot=1 AND c.is_super_bot=1 AND d.round_no=? "
            "AND d.submitted_at IS NOT NULL LIMIT 1",
            (round_no,),
        ).fetchone())
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(super_bot_remote_jobs)")
    }
    fingerprint_columns = (
        "input_fingerprint,state_fingerprint"
        if {"input_fingerprint", "state_fingerprint"} <= columns
        else "'' AS input_fingerprint,'' AS state_fingerprint"
    )
    row = conn.execute(
        f"SELECT phase,round_start,{fingerprint_columns} "
        "FROM super_bot_remote_jobs WHERE round_no=?",
        (round_no,),
    ).fetchone()
    if not row or row[1] != str(current[0] or ""):
        return bool(conn.execute(
            "SELECT 1 FROM companies c JOIN decisions d ON d.company_id=c.id "
            "WHERE c.is_bot=1 AND c.is_super_bot=1 AND d.round_no=? "
            "AND d.submitted_at IS NOT NULL LIMIT 1",
            (round_no,),
        ).fetchone())
    if row[0] != "done":
        return True
    # A completed analysis becomes pending again if KDS, player decisions, or
    # any other authoritative input changes before settlement.
    if not row[2] or not row[3]:
        return True
    input_hash, state_hash = _job_fingerprints(conn, round_no)
    return input_hash != row[2] or state_hash != row[3]


def mark_remote_job_complete(
    conn: sqlite3.Connection,
    round_no: int,
    *,
    expected_input_fingerprint: str | None = None,
) -> None:
    """Close a persisted remote job after a successful full local fallback."""
    if conn.in_transaction:
        raise RemoteWorkerError("完成状态需使用独立短事务保存")
    _job_table(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        round_start = _check_round(conn, round_no)
        input_hash, state_hash = _job_fingerprints(conn, round_no)
        if (
            expected_input_fingerprint is not None
            and input_hash != expected_input_fingerprint
        ):
            raise RemoteAnalysisChangedError(
                "本地分析期间 KDS 或比赛输入已改变，全部超级 Bot 需要重新分析。"
            )
        total = int(conn.execute(
            "SELECT COUNT(*) FROM companies WHERE is_bot=1 AND is_super_bot=1"
        ).fetchone()[0])
        conn.execute(
            "INSERT INTO super_bot_remote_jobs("
            "round_no,round_start,pending_json,total,replace_existing,phase,"
            "input_fingerprint,state_fingerprint) VALUES(?,?,?,?,?,'done',?,?) "
            "ON CONFLICT(round_no) DO UPDATE SET "
            "round_start=excluded.round_start,pending_json=excluded.pending_json,"
            "total=excluded.total,replace_existing=excluded.replace_existing,"
            "phase=excluded.phase,input_fingerprint=excluded.input_fingerprint,"
            "state_fingerprint=excluded.state_fingerprint",
            (round_no, round_start, "[]", total, 1, input_hash, state_hash),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def submit_local_super_bots(
    conn: sqlite3.Connection,
    round_no: int,
    progress_callback: Callable[[int, int, str], None] | None = None,
    *,
    replace_existing: bool = False,
    max_restarts: int = 2,
) -> int:
    """Run local fallback without accepting a mixed-input batch."""
    from .bots import submit_super_bot_decisions

    if not _LOCK.acquire(blocking=False):
        raise RemoteWorkerError("已有超级 Bot 分析正在进行，请等待当前任务完成。")
    try:
        if conn.in_transaction:
            raise RemoteWorkerError("本地分析需使用独立连接执行")
        _job_table(conn)
        force_replace = bool(replace_existing)
        for attempt in range(max(0, int(max_restarts)) + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                round_start = _check_round(conn, round_no)
                input_hash, state_hash = _job_fingerprints(conn, round_no)
                ids = [
                    int(row[0])
                    for row in conn.execute(
                        "SELECT id FROM companies "
                        "WHERE is_bot=1 AND is_super_bot=1 ORDER BY id"
                    )
                ]
                conn.execute(
                    "INSERT INTO super_bot_remote_jobs("
                    "round_no,round_start,pending_json,total,replace_existing,phase,"
                    "input_fingerprint,state_fingerprint) VALUES(?,?,?,?,?,'bot',?,?) "
                    "ON CONFLICT(round_no) DO UPDATE SET "
                    "round_start=excluded.round_start,pending_json=excluded.pending_json,"
                    "total=excluded.total,replace_existing=excluded.replace_existing,"
                    "phase=excluded.phase,input_fingerprint=excluded.input_fingerprint,"
                    "state_fingerprint=excluded.state_fingerprint",
                    (round_no, round_start, json.dumps(ids), len(ids),
                     int(force_replace), input_hash, state_hash),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

            submitted = submit_super_bot_decisions(
                conn,
                round_no,
                progress_callback,
                replace_existing=force_replace,
            )
            # The Bot implementation commits each expensive per-team segment,
            # then performs its final joint rebalance. Persist that final stage
            # before opening the short fingerprint transaction below.
            conn.commit()
            try:
                mark_remote_job_complete(
                    conn,
                    round_no,
                    expected_input_fingerprint=input_hash,
                )
                return submitted
            except RemoteAnalysisChangedError:
                force_replace = True
                if progress_callback:
                    progress_callback(
                        0, max(1, len(ids)),
                        "KDS 或比赛输入已变化，全部超级 Bot 重新分析中",
                    )
                if attempt >= max(0, int(max_restarts)):
                    raise
        raise RemoteWorkerError("超级 Bot 本地分析未完成。")
    finally:
        _LOCK.release()


def assert_remote_job_current(conn: sqlite3.Connection, round_no: int) -> None:
    """Require a completed, current fingerprint inside the caller's write transaction."""
    if not conn.in_transaction:
        raise RemoteWorkerError("结算前校验必须在写事务内执行")
    _check_round(conn, round_no)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='super_bot_remote_jobs'"
    ).fetchone():
        raise RemoteAnalysisChangedError("超级 Bot 决策缺少完整分析记录，请重新分析。")
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(super_bot_remote_jobs)")
    }
    if not {"input_fingerprint", "state_fingerprint"} <= columns:
        raise RemoteAnalysisChangedError("超级 Bot 分析记录需要升级，请重新分析。")
    row = conn.execute(
        "SELECT phase,round_start,input_fingerprint,state_fingerprint "
        "FROM super_bot_remote_jobs WHERE round_no=?",
        (round_no,),
    ).fetchone()
    current_start = _check_round(conn, round_no)
    if (
        not row
        or row[0] != "done"
        or row[1] != current_start
        or not row[2]
        or not row[3]
    ):
        raise RemoteAnalysisChangedError("超级 Bot 分析尚未完整结束，请继续分析。")
    input_hash, state_hash = _job_fingerprints(conn, round_no)
    if input_hash != row[2] or state_hash != row[3]:
        raise RemoteAnalysisChangedError(
            "KDS 或比赛输入在分析后发生变化，请重新分析超级 Bot 后再结算。"
        )


def _post(endpoint: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(endpoint, data=_json(dict(payload, token=token)),
        headers={"Content-Type": "application/json", "X-Super-Bot-Token": token}, method="POST")
    try:
        # Each request computes one Bot only. A half-open local tunnel should
        # not hold the whole round for the old 14-minute batch timeout.
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in _RETRYABLE_HTTP_STATUSES:
            raise RemoteTransportError("计算节点暂不可用或忙碌，请继续未完成的分析。") from exc
        # Authentication, input and computation errors are not a reason to
        # send the same business request to a second worker.
        raise RemoteWorkerError(f"远程计算返回 HTTP {exc.code}，请检查计算节点；已保存的决策不受影响。") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
        raise RemoteTransportError("远程计算连接中断；已保存的 Bot 决策不受影响，请继续未完成的分析。") from exc
    if not isinstance(result, dict):
        raise RemoteWorkerError("远程计算返回格式不正确，未写入决策。")
    if "statusCode" in result and "body" in result:
        if int(result["statusCode"]) in _RETRYABLE_HTTP_STATUSES:
            raise RemoteTransportError("计算节点暂不可用或忙碌，请继续未完成的分析。")
        if int(result["statusCode"]) >= 400:
            raise RemoteWorkerError(f"远程计算返回 HTTP {result['statusCode']}，未写入决策。")
        result = json.loads(result["body"]) if isinstance(result["body"], str) else result["body"]
    if not isinstance(result, dict):
        raise RemoteWorkerError("远程计算返回格式不正确，未写入决策。")
    return result


def remote_submit(conn: sqlite3.Connection, endpoint: str | list[str] | tuple[str, ...], token: str, round_no: int,
                  replace_existing: bool = False,
                  progress_callback: Callable[[int, int, str], None] | None = None,
                  timing_callback: Callable[[dict], None] | None = None) -> int:
    if not token:
        raise RemoteWorkerError("未配置超级 Bot 远程鉴权令牌")
    endpoints = resolve_remote_endpoints(urls=endpoint)
    if not endpoints or any(not url.startswith("https://") for url in endpoints):
        raise RemoteWorkerError("远程计算地址必须使用 HTTPS")
    if not _LOCK.acquire(blocking=False):
        raise RemoteWorkerError("已有超级 Bot 分析正在进行，请等待当前任务完成。")
    try:
        round_start = _check_round(conn, round_no)
        _job_table(conn)
        existing_job = conn.execute(
            "SELECT round_start,phase,input_fingerprint,state_fingerprint "
            "FROM super_bot_remote_jobs WHERE round_no=?",
            (round_no,),
        ).fetchone()
        current_input_hash, current_state_hash = _job_fingerprints(conn, round_no)
        resume_existing = bool(
            existing_job
            and existing_job[0] == round_start
            and (
                existing_job[1] != "done"
                or not existing_job[2]
                or not existing_job[3]
                or existing_job[2] != current_input_hash
                or existing_job[3] != current_state_hash
            )
        )
        if not resume_existing:
            has_untracked_decisions = bool(conn.execute(
                "SELECT 1 FROM companies c JOIN decisions d ON d.company_id=c.id "
                "WHERE c.is_bot=1 AND c.is_super_bot=1 AND d.round_no=? LIMIT 1",
                (round_no,),
            ).fetchone())
            replace_existing = bool(
                replace_existing
                or has_untracked_decisions
                or (existing_job and existing_job[1] == "done")
            )
            query = "SELECT c.id FROM companies c WHERE c.is_bot=1 AND c.is_super_bot=1"
            if not replace_existing:
                query += " AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=?)"
            ids = [int(r[0]) for r in conn.execute(query + " ORDER BY c.id", () if replace_existing else (round_no,))]
            conn.execute(
                "INSERT INTO super_bot_remote_jobs("
                "round_no,round_start,pending_json,total,replace_existing,phase,"
                "input_fingerprint,state_fingerprint) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(round_no) DO UPDATE SET "
                "round_start=excluded.round_start,pending_json=excluded.pending_json,"
                "total=excluded.total,replace_existing=excluded.replace_existing,"
                "phase=excluded.phase,input_fingerprint=excluded.input_fingerprint,"
                "state_fingerprint=excluded.state_fingerprint",
                (round_no, round_start, json.dumps(ids), len(ids), int(replace_existing),
                 "bot" if ids else "rebalance", current_input_hash, current_state_hash),
            )
            conn.commit()
        job = conn.execute(
            "SELECT pending_json,total,replace_existing,phase,input_fingerprint,state_fingerprint "
            "FROM super_bot_remote_jobs WHERE round_no=?",
            (round_no,),
        ).fetchone()
        pending, total, replace = json.loads(job[0]), int(job[1]), bool(job[2])
        eligible = {int(r[0]) for r in conn.execute("SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1")}
        input_hash, state_hash = _job_fingerprints(conn, round_no)
        expected_input_hash, expected_state_hash = str(job[4] or ""), str(job[5] or "")
        if (
            not expected_input_hash
            or not expected_state_hash
            or input_hash != expected_input_hash
            or state_hash != expected_state_hash
        ):
            # KDS/player/admin state changed between segments (or this is a
            # legacy job without fingerprints). Keep every saved decision as
            # a recoverable checkpoint, but make every current Super Bot
            # pending so settlement can never mix analysis generations.
            pending = sorted(eligible)
            total = len(pending)
            replace = True
            conn.execute(
                "UPDATE super_bot_remote_jobs SET pending_json=?,total=?,"
                "replace_existing=1,phase=?,input_fingerprint=?,state_fingerprint=? "
                "WHERE round_no=?",
                (json.dumps(pending), total, "bot" if pending else "rebalance",
                 input_hash, state_hash, round_no),
            )
            conn.commit()
            if progress_callback:
                progress_callback(
                    0, max(1, total),
                    "KDS 或比赛输入已变化，全部超级 Bot 正在重新分析",
                )
            expected_input_hash, expected_state_hash = input_hash, state_hash
        submitted = 0
        active_endpoint = 0
        while True:
            input_hash, state_hash = _job_fingerprints(conn, round_no)
            if input_hash != expected_input_hash or state_hash != expected_state_hash:
                eligible = {
                    int(row[0])
                    for row in conn.execute(
                        "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1"
                    )
                }
                pending = sorted(eligible)
                total = len(pending)
                replace = True
                submitted = 0
                expected_input_hash, expected_state_hash = input_hash, state_hash
                conn.execute(
                    "UPDATE super_bot_remote_jobs SET pending_json=?,total=?,"
                    "replace_existing=1,phase=?,input_fingerprint=?,state_fingerprint=? "
                    "WHERE round_no=?",
                    (json.dumps(pending), total, "bot" if pending else "rebalance",
                     expected_input_hash, expected_state_hash, round_no),
                )
                conn.commit()
                if progress_callback:
                    progress_callback(
                        0, max(1, total),
                        "KDS 或比赛输入已变化，全部超级 Bot 正在重新分析",
                    )
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
            # Retry only this uncommitted Bot/stage, using the identical
            # request id and snapshot. Previously saved Bots stay untouched.
            for attempt in range(len(endpoints)):
                selected_endpoint = (active_endpoint + attempt) % len(endpoints)
                try:
                    result = _post(endpoints[selected_endpoint], token, payload)
                except RemoteTransportError:
                    if attempt + 1 >= len(endpoints):
                        raise
                    if progress_callback:
                        progress_callback(total - len(pending), max(1, total), "计算线路暂不可用，备用线路连接中")
                else:
                    active_endpoint = selected_endpoint
                    break
            received = time.perf_counter()
            submitted += apply_remote_result(conn, payload, result, commit=False)
            try:
                code = "联合复算"
                if pending:
                    code = str(conn.execute("SELECT code FROM companies WHERE id=?", (pending[0],)).fetchone()[0])
                    pending = pending[1:]
                input_hash, state_hash = _job_fingerprints(conn, round_no)
                conn.execute(
                    "UPDATE super_bot_remote_jobs SET pending_json=?,phase=?,"
                    "input_fingerprint=?,state_fingerprint=? WHERE round_no=?",
                    (json.dumps(pending),
                     "done" if phase == "rebalance" else "bot" if pending else "rebalance",
                     input_hash, state_hash, round_no),
                )
                conn.commit()
                expected_input_hash, expected_state_hash = input_hash, state_hash
            except BaseException:
                conn.rollback()
                raise
            timings = dict(result.get("timings", {}), phase=phase, company_id=payload["company_id"],
                endpoint=endpoints[active_endpoint],
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
