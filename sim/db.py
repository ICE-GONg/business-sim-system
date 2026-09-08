from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .defaults import DEFAULT_MARKETS, DEFAULT_SETTINGS


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("SIM_DB_PATH", BASE_DIR / "data" / "sim.db"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds_text, salt_hex, expected_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_text)
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None, cast: type = float) -> Any:
    row = one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return default
    value = row["value"]
    if cast is str:
        return value
    if cast is int:
        return int(float(value))
    if cast is float:
        return float(value)
    return value


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def settings_dict(conn: sqlite3.Connection) -> dict[str, float | int | str]:
    result: dict[str, float | int | str] = {}
    for key, default in DEFAULT_SETTINGS.items():
        result[key] = get_setting(conn, key, default, type(default))
    return result


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS companies(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL COLLATE NOCASE,
                name TEXT NOT NULL COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                home_city TEXT,
                cash REAL NOT NULL DEFAULT 0,
                debt REAL NOT NULL DEFAULT 0,
                patents INTEGER NOT NULL DEFAULT 0,
                product_inventory INTEGER NOT NULL DEFAULT 0,
                component_storage_capacity INTEGER NOT NULL DEFAULT 0,
                product_storage_capacity INTEGER NOT NULL DEFAULT 0,
                setup_submitted_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS employee_cohorts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('worker','engineer')),
                count INTEGER NOT NULL CHECK(count >= 0),
                hire_round INTEGER NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS market_config(
                city TEXT PRIMARY KEY,
                home_enabled INTEGER NOT NULL DEFAULT 1,
                max_loan REAL NOT NULL,
                min_loan REAL NOT NULL DEFAULT 0,
                interest_rate REAL NOT NULL,
                worker_initial_salary REAL NOT NULL,
                engineer_initial_salary REAL NOT NULL,
                component_material REAL NOT NULL,
                product_material REAL NOT NULL,
                component_storage REAL NOT NULL,
                product_storage REAL NOT NULL,
                population REAL NOT NULL,
                penetration REAL NOT NULL,
                initial_avg_price REAL NOT NULL,
                max_price REAL NOT NULL,
                transport_cost REAL NOT NULL DEFAULT 0,
                worker_training_cost REAL NOT NULL DEFAULT 0,
                engineer_training_cost REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS rounds(
                round_no INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('waiting','open','paused','settled')),
                starts_at TEXT,
                ends_at TEXT,
                settled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions(
                company_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                loan_change REAL NOT NULL DEFAULT 0,
                worker_delta INTEGER NOT NULL DEFAULT 0,
                worker_salary REAL NOT NULL DEFAULT 0,
                engineer_delta INTEGER NOT NULL DEFAULT 0,
                engineer_salary REAL NOT NULL DEFAULT 0,
                management_investment REAL NOT NULL DEFAULT 0,
                production_volume INTEGER NOT NULL DEFAULT 0,
                quality_investment REAL NOT NULL DEFAULT 0,
                research_investment REAL NOT NULL DEFAULT 0,
                submitted_at TEXT,
                PRIMARY KEY(company_id,round_no),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS city_decisions(
                company_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                city TEXT NOT NULL,
                agent_delta INTEGER NOT NULL DEFAULT 0,
                marketing_investment REAL NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0,
                order_report INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(company_id,round_no,city),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(city) REFERENCES market_config(city) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS agents(
                company_id INTEGER NOT NULL,
                city TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
                PRIMARY KEY(company_id,city),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(city) REFERENCES market_config(city) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS results(
                company_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                total_assets REAL NOT NULL,
                debt REAL NOT NULL,
                net_assets REAL NOT NULL,
                cash REAL NOT NULL,
                sales_revenue REAL NOT NULL,
                total_cost REAL NOT NULL,
                net_profit REAL NOT NULL,
                produced INTEGER NOT NULL,
                sold INTEGER NOT NULL,
                inventory INTEGER NOT NULL,
                ma_index REAL NOT NULL,
                qi_index REAL NOT NULL,
                research_success INTEGER NOT NULL DEFAULT 0,
                report_json TEXT NOT NULL,
                PRIMARY KEY(company_id,round_no),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS city_results(
                company_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                city TEXT NOT NULL,
                cpi REAL NOT NULL,
                cpi_units REAL NOT NULL,
                sold INTEGER NOT NULL,
                revenue REAL NOT NULL,
                price REAL NOT NULL,
                marketing REAL NOT NULL,
                market_share REAL NOT NULL,
                breakdown_json TEXT NOT NULL,
                PRIMARY KEY(company_id,round_no,city),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS market_round_stats(
                city TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                base_average_price REAL NOT NULL,
                average_price REAL NOT NULL,
                market_size REAL NOT NULL,
                player_total_volume REAL NOT NULL,
                PRIMARY KEY(city,round_no),
                FOREIGN KEY(city) REFERENCES market_config(city) ON DELETE CASCADE
            );
            """
        )
        market_columns = {row["name"] for row in all_rows(conn, "PRAGMA table_info(market_config)")}
        if "min_loan" not in market_columns:
            conn.execute("ALTER TABLE market_config ADD COLUMN min_loan REAL NOT NULL DEFAULT 0")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
        count = one(conn, "SELECT COUNT(*) AS n FROM market_config")
        if count and int(count["n"]) == 0:
            conn.executemany(
                "INSERT INTO market_config(city,home_enabled,max_loan,min_loan,interest_rate,worker_initial_salary,"
                "engineer_initial_salary,component_material,product_material,component_storage,product_storage,"
                "population,penetration,initial_avg_price,max_price,transport_cost,worker_training_cost,engineer_training_cost) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                DEFAULT_MARKETS,
            )
        count = one(conn, "SELECT COUNT(*) AS n FROM companies")
        if count and int(count["n"]) == 0:
            initial_cash = float(DEFAULT_SETTINGS["initial_cash"])
            for index in range(1, 5):
                conn.execute(
                    "INSERT INTO companies(code,name,password_hash,cash,created_at) VALUES(?,?,?,?,?)",
                    (f"C{index:02d}", f"待命名-C{index:02d}", hash_password("1234"), initial_cash, now_iso()),
                )
        count = one(conn, "SELECT COUNT(*) AS n FROM rounds")
        if count and int(count["n"]) == 0:
            conn.execute("INSERT INTO rounds(round_no,status) VALUES(1,'waiting')")


def employee_count(conn: sqlite3.Connection, company_id: int, role: str) -> int:
    row = one(
        conn,
        "SELECT COALESCE(SUM(count),0) AS n FROM employee_cohorts WHERE company_id=? AND role=?",
        (company_id, role),
    )
    return int(row["n"] if row else 0)


def effective_employee_count(conn: sqlite3.Connection, company_id: int, role: str, round_no: int) -> float:
    total = 0.0
    for row in all_rows(
        conn,
        "SELECT count,hire_round FROM employee_cohorts WHERE company_id=? AND role=?",
        (company_id, role),
    ):
        experience = 1.10 if round_no - int(row["hire_round"]) >= 2 else 1.0
        total += int(row["count"]) * experience
    return total


def remove_employees(conn: sqlite3.Connection, company_id: int, role: str, count: int) -> None:
    remaining = max(0, int(count))
    cohorts = all_rows(
        conn,
        "SELECT id,count FROM employee_cohorts WHERE company_id=? AND role=? ORDER BY hire_round DESC,id DESC",
        (company_id, role),
    )
    for cohort in cohorts:
        if remaining <= 0:
            break
        take = min(remaining, int(cohort["count"]))
        left = int(cohort["count"]) - take
        if left:
            conn.execute("UPDATE employee_cohorts SET count=? WHERE id=?", (left, cohort["id"]))
        else:
            conn.execute("DELETE FROM employee_cohorts WHERE id=?", (cohort["id"],))
        remaining -= take


def setup_status(conn: sqlite3.Connection) -> dict[str, int | bool]:
    total_row = one(conn, "SELECT COUNT(*) AS n FROM companies")
    ready_row = one(
        conn,
        "SELECT COUNT(*) AS n FROM companies WHERE home_city IS NOT NULL AND setup_submitted_at IS NOT NULL",
    )
    total = int(total_row["n"] if total_row else 0)
    ready = int(ready_row["n"] if ready_row else 0)
    return {"total": total, "ready": ready, "all_ready": total > 0 and total == ready}


def submission_status(conn: sqlite3.Connection, round_no: int) -> dict[str, int | bool]:
    total_row = one(conn, "SELECT COUNT(*) AS n FROM companies")
    submitted_row = one(
        conn,
        "SELECT COUNT(*) AS n FROM decisions WHERE round_no=? AND submitted_at IS NOT NULL",
        (round_no,),
    )
    total = int(total_row["n"] if total_row else 0)
    submitted = int(submitted_row["n"] if submitted_row else 0)
    return {"total": total, "submitted": submitted, "all_submitted": total > 0 and total == submitted}


def current_round(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return one(
        conn,
        "SELECT * FROM rounds WHERE status IN ('waiting','open','paused') ORDER BY round_no DESC LIMIT 1",
    ) or one(conn, "SELECT * FROM rounds ORDER BY round_no DESC LIMIT 1")


def reset_competition(conn: sqlite3.Connection) -> None:
    for table in ("market_round_stats", "city_results", "results", "city_decisions", "decisions", "agents", "employee_cohorts", "rounds"):
        conn.execute(f"DELETE FROM {table}")
    initial_cash = get_setting(conn, "initial_cash", 15_000_000)
    for company in all_rows(conn, "SELECT id,code FROM companies ORDER BY id"):
        conn.execute(
            "UPDATE companies SET name=?,home_city=NULL,setup_submitted_at=NULL,cash=?,debt=0,patents=0,"
            "product_inventory=0,component_storage_capacity=0,product_storage_capacity=0 WHERE id=?",
            (f"待命名-{company['code']}", initial_cash, company["id"]),
        )
    conn.execute("INSERT INTO rounds(round_no,status) VALUES(1,'waiting')")


def delete_company(conn: sqlite3.Connection, company_id: int) -> None:
    """Delete one team and all of its dependent competition data."""
    company = one(conn, "SELECT id FROM companies WHERE id=?", (company_id,))
    if company is None:
        raise ValueError("玩家不存在或已经被删除。")
    total = one(conn, "SELECT COUNT(*) AS n FROM companies")
    if total and int(total["n"]) <= 1:
        raise ValueError("至少需要保留一个玩家。")
    conn.execute("DELETE FROM companies WHERE id=?", (company_id,))


def delete_city(conn: sqlite3.Connection, city: str) -> None:
    """Delete one city and detach any team that used it as its home market."""
    market = one(conn, "SELECT city FROM market_config WHERE city=?", (city,))
    if market is None:
        raise ValueError("城市不存在或已经被删除。")
    total = one(conn, "SELECT COUNT(*) AS n FROM market_config")
    if total and int(total["n"]) <= 1:
        raise ValueError("至少需要保留一个城市。")
    conn.execute(
        "UPDATE companies SET home_city=NULL,setup_submitted_at=NULL WHERE home_city=?",
        (city,),
    )
    # Older databases created city_results without a city foreign key.
    conn.execute("DELETE FROM city_results WHERE city=?", (city,))
    conn.execute("DELETE FROM market_config WHERE city=?", (city,))


def database_bytes() -> bytes:
    if not DB_PATH.exists():
        return b""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            temp_path = Path(handle.name)
        source = sqlite3.connect(DB_PATH, timeout=30)
        destination = sqlite3.connect(temp_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        return temp_path.read_bytes()
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def restore_database_bytes(payload: bytes) -> None:
    if not payload:
        raise ValueError("备份文件为空。")
    required_tables = {"settings", "companies", "market_config", "rounds", "decisions", "results"}
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        source = sqlite3.connect(temp_path)
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise ValueError("备份文件完整性检查失败。")
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required_tables.issubset(tables):
                raise ValueError("文件不是本系统的完整数据库备份。")
            city_result_columns = {row[1] for row in source.execute("PRAGMA table_info(city_results)")}
            if not {"cpi_units", "breakdown_json"}.issubset(city_result_columns):
                raise ValueError("备份版本不兼容，请上传本 Streamlit 系统导出的备份。")
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            destination = sqlite3.connect(DB_PATH, timeout=30)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
        finally:
            source.close()
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    # Older backups remain valid; running the idempotent initializer adds any
    # tables introduced by newer versions without changing restored data.
    init_db()


init_db()
