from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
                research_balance REAL NOT NULL DEFAULT 0,
                component_inventory INTEGER NOT NULL DEFAULT 0,
                product_inventory INTEGER NOT NULL DEFAULT 0,
                is_bot INTEGER NOT NULL DEFAULT 0,
                bot_profile INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS round_snapshots(
                round_no INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS round_bonuses(
                company_id INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(company_id,round_no),
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
            );
            """
        )
        market_columns = {row["name"] for row in all_rows(conn, "PRAGMA table_info(market_config)")}
        if "min_loan" not in market_columns:
            conn.execute("ALTER TABLE market_config ADD COLUMN min_loan REAL NOT NULL DEFAULT 0")
        company_columns = {row["name"] for row in all_rows(conn, "PRAGMA table_info(companies)")}
        if "research_balance" not in company_columns:
            conn.execute("ALTER TABLE companies ADD COLUMN research_balance REAL NOT NULL DEFAULT 0")
        if "component_inventory" not in company_columns:
            conn.execute("ALTER TABLE companies ADD COLUMN component_inventory INTEGER NOT NULL DEFAULT 0")
        if "is_bot" not in company_columns:
            conn.execute("ALTER TABLE companies ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        if "bot_profile" not in company_columns:
            conn.execute("ALTER TABLE companies ADD COLUMN bot_profile INTEGER NOT NULL DEFAULT 0")
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


def capture_round_snapshot(conn: sqlite3.Connection, round_no: int) -> None:
    """Store the exact mutable competition state before a round is settled."""
    company_columns = (
        "id,code,name,home_city,cash,debt,patents,research_balance,component_inventory,product_inventory,is_bot,bot_profile,"
        "component_storage_capacity,product_storage_capacity,setup_submitted_at"
    )
    snapshot = {
        "companies": [dict(row) for row in all_rows(conn, f"SELECT {company_columns} FROM companies ORDER BY id")],
        "employee_cohorts": [
            dict(row)
            for row in all_rows(conn, "SELECT company_id,role,count,hire_round FROM employee_cohorts ORDER BY id")
        ],
        "agents": [dict(row) for row in all_rows(conn, "SELECT company_id,city,count FROM agents ORDER BY company_id,city")],
    }
    conn.execute(
        "INSERT INTO round_snapshots(round_no,snapshot_json,created_at) VALUES(?,?,?) "
        "ON CONFLICT(round_no) DO UPDATE SET snapshot_json=excluded.snapshot_json,created_at=excluded.created_at",
        (int(round_no), json.dumps(snapshot, ensure_ascii=False), now_iso()),
    )


def _reconstruct_pre_round_snapshot(conn: sqlite3.Connection, target_round: int) -> dict[str, Any]:
    """Best-effort migration path for rounds settled before snapshots existed."""
    companies: list[dict[str, Any]] = []
    cohorts: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    initial_cash = float(get_setting(conn, "initial_cash", 15_000_000.0))
    company_rows = all_rows(conn, "SELECT * FROM companies ORDER BY id")
    for company_row in company_rows:
        company = dict(company_row)
        company_id = int(company["id"])
        previous_result = one(
            conn,
            "SELECT * FROM results WHERE company_id=? AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (company_id, target_round),
        )
        previous_report: dict[str, Any] = {}
        if previous_result is not None:
            previous_report = json.loads(previous_result["report_json"])
        production = previous_report.get("production", {})
        research = previous_report.get("research", {})
        companies.append(
            {
                "id": company_id,
                "code": company["code"],
                "name": company["name"],
                "home_city": company["home_city"],
                "cash": float(previous_result["cash"]) if previous_result is not None else initial_cash,
                "debt": float(previous_result["debt"]) if previous_result is not None else 0.0,
                "patents": int(research.get("patents_after", 0)) if previous_result is not None else 0,
                "research_balance": float(research.get("accumulated_after", 0)) if previous_result is not None else 0,
                "component_inventory": int(production.get("component_surplus", 0)),
                "product_inventory": int(previous_result["inventory"]) if previous_result is not None else 0,
                "is_bot": int(company.get("is_bot", 0)),
                "bot_profile": int(company.get("bot_profile", 0)),
                "component_storage_capacity": int(production.get("component_storage_after", 0)),
                "product_storage_capacity": int(production.get("product_storage_after", 0)),
                "setup_submitted_at": company["setup_submitted_at"],
            }
        )

        replayed: list[dict[str, Any]] = []
        for decision in all_rows(
            conn,
            "SELECT round_no,worker_delta,engineer_delta FROM decisions WHERE company_id=? AND round_no>=1 AND round_no<? "
            "AND round_no IN (SELECT round_no FROM results WHERE company_id=?) ORDER BY round_no",
            (company_id, target_round, company_id),
        ):
            for role, field in (("worker", "worker_delta"), ("engineer", "engineer_delta")):
                delta = int(decision[field] or 0)
                if delta > 0:
                    replayed.append({"company_id": company_id, "role": role, "count": delta, "hire_round": int(decision["round_no"])})
                elif delta < 0:
                    remaining = -delta
                    for cohort in sorted(
                        (item for item in replayed if item["role"] == role and item["count"] > 0),
                        key=lambda item: item["hire_round"],
                        reverse=True,
                    ):
                        removed = min(remaining, int(cohort["count"]))
                        cohort["count"] -= removed
                        remaining -= removed
                        if remaining <= 0:
                            break
        cohorts.extend(item for item in replayed if int(item["count"]) > 0)

        if previous_report:
            agents.extend(
                {
                    "company_id": company_id,
                    "city": item["city"],
                    "count": int(item.get("agents", 0)),
                }
                for item in previous_report.get("sales", [])
                if int(item.get("agents", 0)) > 0
            )
        elif company.get("home_city"):
            agents.append({"company_id": company_id, "city": company["home_city"], "count": 1})
    return {"companies": companies, "employee_cohorts": cohorts, "agents": agents}


def _restore_snapshot_state(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    """Restore mutable team state without deleting historical reports."""
    conn.execute("DELETE FROM employee_cohorts")
    conn.execute("DELETE FROM agents")
    existing_company_ids = {int(row["id"]) for row in all_rows(conn, "SELECT id FROM companies")}
    existing_cities = {str(row["city"]) for row in all_rows(conn, "SELECT city FROM market_config")}
    company_fields = (
        "name=?,home_city=?,cash=?,debt=?,patents=?,research_balance=?,component_inventory=?,product_inventory=?,is_bot=?,bot_profile=?,"
        "component_storage_capacity=?,product_storage_capacity=?,setup_submitted_at=?"
    )
    for company in snapshot.get("companies", []):
        company_id = int(company["id"])
        if company_id not in existing_company_ids:
            continue
        restored_home = company.get("home_city") if company.get("home_city") in existing_cities else None
        conn.execute(
            f"UPDATE companies SET {company_fields} WHERE id=?",
            (
                company["name"], restored_home, float(company.get("cash", 0)),
                float(company.get("debt", 0)), int(company.get("patents", 0)),
                float(company.get("research_balance", 0)),
                int(company.get("component_inventory", 0)), int(company.get("product_inventory", 0)),
                int(company.get("is_bot", 0)), int(company.get("bot_profile", 0)), int(company.get("component_storage_capacity", 0)),
                int(company.get("product_storage_capacity", 0)),
                company.get("setup_submitted_at") if restored_home else None,
                company_id,
            ),
        )
    conn.executemany(
        "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,?)",
        [
            (int(item["company_id"]), str(item["role"]), int(item["count"]), int(item["hire_round"]))
            for item in snapshot.get("employee_cohorts", [])
            if int(item.get("count", 0)) > 0 and int(item["company_id"]) in existing_company_ids
        ],
    )
    conn.executemany(
        "INSERT INTO agents(company_id,city,count) VALUES(?,?,?)",
        [
            (int(item["company_id"]), str(item["city"]), int(item["count"]))
            for item in snapshot.get("agents", [])
            if int(item.get("count", 0)) > 0
            and int(item["company_id"]) in existing_company_ids
            and str(item["city"]) in existing_cities
        ],
    )


def start_competition(conn: sqlite3.Connection, duration_minutes: int, use_test_round: bool) -> int:
    """Start either the optional -1 test round or official round one."""
    waiting = one(conn, "SELECT * FROM rounds WHERE round_no=1 AND status='waiting'")
    if waiting is None:
        raise ValueError("比赛已经开始，不能再次选择测试轮。")
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=max(1, int(duration_minutes)))
    set_setting(conn, "test_round_enabled", int(bool(use_test_round)))
    if use_test_round:
        conn.execute("DELETE FROM rounds WHERE round_no=1")
        conn.execute(
            "INSERT INTO rounds(round_no,status,starts_at,ends_at,settled_at) VALUES(-1,'open',?,?,NULL)",
            (start.isoformat(), end.isoformat()),
        )
        return -1
    conn.execute(
        "UPDATE rounds SET status='open',starts_at=?,ends_at=?,settled_at=NULL WHERE round_no=1",
        (start.isoformat(), end.isoformat()),
    )
    return 1


def prepare_first_round_after_test(conn: sqlite3.Connection, duration_minutes: int) -> int:
    """Discard test-round state effects while retaining its decisions and reports."""
    test_round = one(conn, "SELECT status FROM rounds WHERE round_no=-1")
    if test_round is None or test_round["status"] != "settled":
        raise ValueError("测试轮尚未结算。")
    snapshot_row = one(conn, "SELECT snapshot_json FROM round_snapshots WHERE round_no=-1")
    if snapshot_row is None:
        raise ValueError("找不到测试轮赛前快照，无法安全开始第一轮。")
    _restore_snapshot_state(conn, json.loads(snapshot_row["snapshot_json"]))
    conn.execute("DELETE FROM rounds WHERE round_no>=1")
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=max(1, int(duration_minutes)))
    conn.execute(
        "INSERT INTO rounds(round_no,status,starts_at,ends_at,settled_at) VALUES(1,'open',?,?,NULL)",
        (start.isoformat(), end.isoformat()),
    )
    return 1


def rollback_latest_settled_round(conn: sqlite3.Connection, duration_minutes: int = 30) -> int:
    """Undo the latest settlement and reopen that round for fresh submissions."""
    latest = one(conn, "SELECT MAX(round_no) AS round_no FROM results")
    if latest is None or latest["round_no"] is None:
        raise ValueError("还没有已结算回合，无法回退。")
    target_round = int(latest["round_no"])
    snapshot_row = one(conn, "SELECT snapshot_json FROM round_snapshots WHERE round_no=?", (target_round,))
    snapshot = json.loads(snapshot_row["snapshot_json"]) if snapshot_row else _reconstruct_pre_round_snapshot(conn, target_round)

    _restore_snapshot_state(conn, snapshot)

    conn.execute("DELETE FROM market_round_stats WHERE round_no>=?", (target_round,))
    conn.execute("DELETE FROM city_results WHERE round_no>=?", (target_round,))
    conn.execute("DELETE FROM results WHERE round_no>=?", (target_round,))
    conn.execute("DELETE FROM city_decisions WHERE round_no>?", (target_round,))
    conn.execute("DELETE FROM decisions WHERE round_no>?", (target_round,))
    conn.execute("UPDATE decisions SET submitted_at=NULL WHERE round_no=?", (target_round,))
    conn.execute("DELETE FROM rounds WHERE round_no>=?", (target_round,))
    conn.execute("DELETE FROM round_snapshots WHERE round_no>=?", (target_round,))
    conn.execute("DELETE FROM round_bonuses WHERE round_no>?", (target_round,))
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=max(1, int(duration_minutes)))
    conn.execute(
        "INSERT INTO rounds(round_no,status,starts_at,ends_at,settled_at) VALUES(?,'open',?,?,NULL)",
        (target_round, start.isoformat(), end.isoformat()),
    )
    return target_round


def reset_competition(conn: sqlite3.Connection) -> None:
    for table in ("round_bonuses", "round_snapshots", "market_round_stats", "city_results", "results", "city_decisions", "decisions", "agents", "employee_cohorts", "rounds"):
        conn.execute(f"DELETE FROM {table}")
    initial_cash = get_setting(conn, "initial_cash", 15_000_000)
    homes = [str(row["city"]) for row in all_rows(conn, "SELECT city FROM market_config WHERE home_enabled=1 ORDER BY city")]
    for company in all_rows(conn, "SELECT id,code,is_bot,bot_profile FROM companies ORDER BY id"):
        if bool(company["is_bot"]) and homes:
            home = homes[int(company["bot_profile"] or 0) % len(homes)]
            conn.execute(
                "UPDATE companies SET name=?,home_city=?,setup_submitted_at=?,cash=?,debt=0,patents=0,research_balance=0,"
                "component_inventory=0,product_inventory=0,component_storage_capacity=0,product_storage_capacity=0 WHERE id=?",
                (f"Auto Company {company['id']}", home, now_iso(), initial_cash, company["id"]),
            )
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,1)", (company["id"], home))
        else:
            conn.execute(
                "UPDATE companies SET name=?,home_city=NULL,setup_submitted_at=NULL,cash=?,debt=0,patents=0,research_balance=0,"
                "component_inventory=0,product_inventory=0,component_storage_capacity=0,product_storage_capacity=0 WHERE id=?",
                (f"待命名-{company['code']}", initial_cash, company["id"]),
            )
    set_setting(conn, "test_round_enabled", 0)
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
