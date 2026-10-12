"""Database-backed orchestration primitives for the optional AI training mode.

This module intentionally does not settle rounds or calculate Bot decisions.
It reports the next safe, idempotent action to the UI controller, while the
existing engine and Bot modules remain the sole owners of competition rules.
"""
from __future__ import annotations

import math
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import (
    all_rows,
    current_round,
    get_setting,
    hash_password,
    now_iso,
    one,
    ranked_bot_home_cities,
    set_setting,
)


AI_MODE = "ai_training"
MANUAL_MODE = "manual"
AI_TRAINING_API_VERSION = 1

ACTION_NONE = "none"
ACTION_WAIT = "wait"
ACTION_START = "start_competition"
ACTION_SUBMIT_REGULAR_BOTS = "submit_regular_bots"
ACTION_BEGIN_PROCESSING = "begin_processing"
ACTION_ANALYZE_SUPER_BOTS = "analyze_super_bots"
ACTION_FINALIZE_SUPER_BOTS = "finalize_super_bots"
ACTION_SETTLE = "settle_round"
ACTION_PREPARE_FIRST_ROUND = "prepare_first_round"
ACTION_OPEN_NEXT_ROUND = "open_next_round"
ACTION_RELEASE_FINAL = "release_final_results"
ACTION_COMPLETE = "complete"


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime(conn: sqlite3.Connection) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM ai_training_runtime WHERE id=1")
    if row is None:
        # init_db normally creates this row. Keeping this fallback makes the
        # public helpers safe against a connection opened during a hot deploy.
        conn.execute(
            "INSERT INTO ai_training_runtime("
            "id,enabled,player_count,bot_count,super_bot_count,disruptor_bot_count,updated_at"
            ") VALUES(1,0,0,0,0,0,?)",
            (now_iso(),),
        )
        row = one(conn, "SELECT * FROM ai_training_runtime WHERE id=1")
    assert row is not None
    return row


def get_ai_training_config(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return stored mode settings plus the live roster summary."""
    runtime = _runtime(conn)
    actual = one(
        conn,
        "SELECT COUNT(*) AS total,"
        "COALESCE(SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END),0) AS players,"
        "COALESCE(SUM(CASE WHEN is_bot=1 AND is_super_bot=0 THEN 1 ELSE 0 END),0) AS bots,"
        "COALESCE(SUM(CASE WHEN is_super_bot=1 AND is_disruptor_bot=0 THEN 1 ELSE 0 END),0) AS super_bots,"
        "COALESCE(SUM(CASE WHEN is_disruptor_bot=1 THEN 1 ELSE 0 END),0) AS disruptor_bots "
        "FROM companies",
    )
    configured = {
        "player_count": int(runtime["player_count"]),
        "bot_count": int(runtime["bot_count"]),
        "super_bot_count": int(runtime["super_bot_count"]),
        "disruptor_bot_count": int(runtime["disruptor_bot_count"]),
    }
    actual_counts = {
        "player_count": int(actual["players"] if actual else 0),
        "bot_count": int(actual["bots"] if actual else 0),
        "super_bot_count": int(actual["super_bots"] if actual else 0),
        "disruptor_bot_count": int(actual["disruptor_bots"] if actual else 0),
    }
    return {
        "enabled": bool(runtime["enabled"]),
        "mode": AI_MODE if bool(runtime["enabled"]) else MANUAL_MODE,
        "auto_enabled": bool(get_setting(conn, "ai_training_auto_enabled", 1, int)),
        "transition_seconds": max(
            3,
            int(get_setting(conn, "ai_training_transition_seconds", 5, int)),
        ),
        **configured,
        "actual_player_count": actual_counts["player_count"],
        "actual_bot_count": actual_counts["bot_count"],
        "actual_super_bot_count": actual_counts["super_bot_count"],
        "actual_disruptor_bot_count": actual_counts["disruptor_bot_count"],
        "actual_total": int(actual["total"] if actual else 0),
        "roster_matches": configured == actual_counts,
        "lease_owner": runtime["lease_owner"],
        "lease_phase": runtime["lease_phase"],
        "lease_expires_at": runtime["lease_expires_at"],
        "last_error": runtime["last_error"],
        "retry_not_before": runtime["retry_not_before"],
        "last_action": runtime["last_action"],
        "processing_round_no": int(
            get_setting(conn, "ai_training_processing_round", 0, int)
        ),
        "processing_started_at": get_setting(
            conn, "ai_training_processing_started_at", "", str
        ),
        "last_collection_at": get_setting(
            conn, "ai_training_last_collection_at", "", str
        ),
    }


def set_ai_training_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Switch controllers without changing teams or competition data."""
    _runtime(conn)
    conn.execute(
        "UPDATE ai_training_runtime SET enabled=?,lease_owner=NULL,lease_phase=NULL,"
        "lease_expires_at=NULL,retry_not_before=NULL,updated_at=? WHERE id=1",
        (int(bool(enabled)), now_iso()),
    )
    set_setting(conn, "operation_mode", AI_MODE if enabled else MANUAL_MODE)
    set_setting(conn, "ai_training_last_collection_at", "")
    if not enabled:
        set_setting(conn, "ai_training_processing_round", 0)
        set_setting(conn, "ai_training_processing_started_at", "")
    if one(conn, "SELECT 1 FROM settings WHERE key='ai_training_auto_enabled'") is None:
        set_setting(conn, "ai_training_auto_enabled", 1)


def clear_ai_training_runtime(conn: sqlite3.Connection, *, clear_error: bool = True) -> None:
    """Clear only orchestration state; never touch teams or round results."""
    _runtime(conn)
    conn.execute(
        "UPDATE ai_training_runtime SET lease_owner=NULL,lease_phase=NULL,lease_expires_at=NULL,"
        "retry_not_before=NULL,last_action=NULL,last_error=CASE WHEN ? THEN NULL ELSE last_error END,"
        "updated_at=? WHERE id=1",
        (int(bool(clear_error)), now_iso()),
    )
    set_setting(conn, "ai_training_processing_round", 0)
    set_setting(conn, "ai_training_processing_started_at", "")
    set_setting(conn, "ai_training_last_collection_at", "")


def claim_ai_training_collection(
    conn: sqlite3.Connection,
    interval_seconds: int = 60,
    *,
    now: datetime | None = None,
) -> bool:
    """Atomically allow at most one full submission scan per interval.

    The one-second processing heartbeat reads only the small runtime marker;
    player/Bot submissions are collected by this slower, durable cadence.
    """
    at = _utc_now(now)
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        runtime = _runtime(conn)
        if not bool(runtime["enabled"]):
            claimed = False
        elif not bool(get_setting(conn, "ai_training_auto_enabled", 1, int)):
            claimed = False
        elif int(get_setting(conn, "ai_training_processing_round", 0, int)) != 0:
            claimed = False
        else:
            last = _parse_time(
                get_setting(conn, "ai_training_last_collection_at", "", str)
            )
            due = last is None or at >= last + timedelta(
                seconds=max(1, int(interval_seconds))
            )
            if due:
                set_setting(conn, "ai_training_last_collection_at", at.isoformat())
            claimed = due
        if started_transaction:
            conn.commit()
        return bool(claimed)
    except Exception:
        if started_transaction:
            conn.rollback()
        raise


def begin_ai_training_processing(
    conn: sqlite3.Connection,
    round_no: int,
) -> None:
    """Lock one open round for calculation before any result is published."""
    target = int(round_no)
    row = one(
        conn,
        "SELECT status FROM rounds WHERE round_no=?",
        (target,),
    )
    if row is None or str(row["status"]) != "open":
        raise ValueError("当前回合已变化，未进入自动结算阶段。")
    set_setting(conn, "ai_training_processing_round", target)
    set_setting(conn, "ai_training_processing_started_at", now_iso())


def clear_ai_training_processing(
    conn: sqlite3.Connection,
    round_no: int | None = None,
) -> bool:
    """Clear the calculation lock, optionally only for its owning round."""
    current = int(get_setting(conn, "ai_training_processing_round", 0, int))
    if round_no is not None and current not in (0, int(round_no)):
        return False
    set_setting(conn, "ai_training_processing_round", 0)
    set_setting(conn, "ai_training_processing_started_at", "")
    set_setting(conn, "ai_training_last_collection_at", "")
    return True


def configure_ai_training_roster(
    conn: sqlite3.Connection,
    player_count: int,
    bot_count: int,
    super_bot_count: int,
    player_password: str = "1234",
    *,
    disruptor_bot_count: int = 0,
) -> dict[str, Any]:
    """Explicitly replace the waiting-room roster with exact requested counts.

    This is the only helper in this module that deletes team data. It refuses
    to run after a competition starts. Human players are left unready so they
    can choose a permanent home; Bots receive a valid home and one Agent.
    """
    players = int(player_count)
    bots = int(bot_count)
    super_bots = int(super_bot_count)
    disruptor_bots = int(disruptor_bot_count)
    if min(players, bots, super_bots, disruptor_bots) < 0:
        raise ValueError("玩家与 Bot 数量不能为负数。")
    if players + bots + super_bots + disruptor_bots < 1:
        raise ValueError("AI 训练至少需要一支队伍。")
    if players and not str(player_password):
        raise ValueError("玩家初始密码不能为空。")

    waiting = one(
        conn,
        "SELECT round_no,status FROM rounds ORDER BY round_no DESC LIMIT 1",
    )
    result_count = one(conn, "SELECT COUNT(*) AS n FROM results")
    if (
        waiting is None
        or int(waiting["round_no"]) != 1
        or str(waiting["status"]) != "waiting"
        or int(result_count["n"] if result_count else 0) > 0
    ):
        raise ValueError("只能在比赛尚未开始的等待状态重建 AI 训练阵容。")

    homes = [
        str(row["city"])
        for row in all_rows(
            conn,
            "SELECT city FROM market_config WHERE home_enabled=1 ORDER BY city",
        )
    ]
    if (bots or super_bots or disruptor_bots) and not homes:
        raise ValueError("请先设置至少一个可选主场，再创建 Bot。")

    # An explicit roster rebuild is a clean waiting-room operation. It leaves
    # KDS/settings intact but removes every prior team-owned artifact.
    for table in (
        "round_bonuses",
        "round_snapshots",
        "market_round_stats",
        "city_results",
        "results",
        "city_decisions",
        "decisions",
        "agents",
        "employee_cohorts",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM companies")
    conn.execute("DELETE FROM rounds")
    # Stable IDs make exported training datasets and UI labels repeatable.
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('companies','employee_cohorts')")

    initial_cash = float(get_setting(conn, "initial_cash", 15_000_000.0))
    player_hash = hash_password(str(player_password)) if players else ""
    bot_hash = hash_password(secrets.token_hex(24)) if bots or super_bots or disruptor_bots else ""
    created = now_iso()
    for index in range(1, players + 1):
        code = f"C{index:02d}"
        conn.execute(
            "INSERT INTO companies(code,name,password_hash,cash,created_at) VALUES(?,?,?,?,?)",
            (code, f"待命名-{code}", player_hash, initial_cash, created),
        )

    for index in range(1, bots + 1):
        home = homes[(index - 1) % len(homes)]
        cursor = conn.execute(
            "INSERT INTO companies("
            "code,name,password_hash,home_city,cash,setup_submitted_at,is_bot,is_super_bot,bot_profile,created_at"
            ") VALUES(?,?,?,?,?,?,1,0,?,?)",
            (f"BOT{index:02d}", f"Bot {index}", bot_hash, home, initial_cash, created, index, created),
        )
        conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,?,1)",
            (int(cursor.lastrowid), home),
        )

    ranked_homes = ranked_bot_home_cities(conn) or homes
    strong_pool_size = max(1, math.ceil(len(ranked_homes) / 3)) if ranked_homes else 0
    for index in range(1, super_bots + 1):
        home = ranked_homes[(index - 1) % strong_pool_size]
        cursor = conn.execute(
            "INSERT INTO companies("
            "code,name,password_hash,home_city,cash,setup_submitted_at,is_bot,is_super_bot,bot_profile,created_at"
            ") VALUES(?,?,?,?,?,?,1,1,?,?)",
            (
                f"SBOT{index:02d}",
                f"Super Auto Company {index}",
                bot_hash,
                home,
                initial_cash,
                created,
                index,
                created,
            ),
        )
        conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,?,1)",
            (int(cursor.lastrowid), home),
        )

    for index in range(1, disruptor_bots + 1):
        public_index = bots + index
        home = ranked_homes[(super_bots + index - 1) % strong_pool_size]
        cursor = conn.execute(
            "INSERT INTO companies("
            "code,name,password_hash,home_city,cash,setup_submitted_at,is_bot,is_super_bot,"
            "is_disruptor_bot,bot_profile,created_at"
            ") VALUES(?,?,?,?,?,?,1,1,1,?,?)",
            (
                f"BOT{public_index:02d}",
                f"Bot {public_index}",
                bot_hash,
                home,
                initial_cash,
                created,
                super_bots + index,
                created,
            ),
        )
        conn.execute(
            "INSERT INTO agents(company_id,city,count) VALUES(?,?,1)",
            (int(cursor.lastrowid), home),
        )

    conn.execute("INSERT INTO rounds(round_no,status) VALUES(1,'waiting')")
    set_setting(conn, "test_round_enabled", 0)
    set_setting(conn, "final_results_release_round", 0)
    set_setting(conn, "operation_mode", AI_MODE)
    set_setting(conn, "ai_training_processing_round", 0)
    set_setting(conn, "ai_training_processing_started_at", "")
    set_setting(conn, "ai_training_last_collection_at", "")
    if one(conn, "SELECT 1 FROM settings WHERE key='ai_training_auto_enabled'") is None:
        set_setting(conn, "ai_training_auto_enabled", 1)
    _runtime(conn)
    conn.execute(
        "UPDATE ai_training_runtime SET enabled=1,player_count=?,bot_count=?,super_bot_count=?,"
        "disruptor_bot_count=?,"
        "lease_owner=NULL,lease_phase=NULL,lease_expires_at=NULL,last_error=NULL,"
        "retry_not_before=NULL,last_action='configure_roster',updated_at=? WHERE id=1",
        (players, bots, super_bots, disruptor_bots, now_iso()),
    )
    return get_ai_training_config(conn)


def super_bot_analysis_order(
    conn: sqlite3.Connection,
    round_no: int,
) -> list[int]:
    """Return one stable order for strategic and disruptor Super Bots.

    During expansion rounds the strategic models establish their plans first,
    allowing disruptors to select a non-overlapping route from saved drafts.
    In the final two official rounds disruptors move first, so strategic models
    can react to their player-pressure decisions.  The optional test round is
    always treated as an early round.
    """
    total_rounds = max(1, int(get_setting(conn, "total_rounds", 5, int)))
    target_round = int(round_no)
    disruptors_first = (
        target_round > 0
        and target_round >= max(1, total_rounds - 1)
    )
    rows = all_rows(
        conn,
        "SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1 "
        "ORDER BY CASE WHEN is_disruptor_bot=? THEN 0 ELSE 1 END,id",
        (int(disruptors_first),),
    )
    return [int(row["id"]) for row in rows]


def ai_training_action(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    *,
    lease_owner: str | None = None,
) -> dict[str, Any]:
    """Describe the next safe controller action without mutating game state."""
    at = _utc_now(now)
    runtime = _runtime(conn)
    round_row = current_round(conn)
    round_no = int(round_row["round_no"]) if round_row is not None else None

    def response(action: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "action": action,
            "reason": reason,
            "round_no": round_no,
            "mode": AI_MODE if bool(runtime["enabled"]) else MANUAL_MODE,
            **extra,
        }

    if not bool(runtime["enabled"]):
        return response(ACTION_NONE, "disabled")
    if not bool(get_setting(conn, "ai_training_auto_enabled", 1, int)):
        return response(ACTION_NONE, "auto_advance_disabled")
    retry_at = _parse_time(runtime["retry_not_before"])
    if retry_at is not None and retry_at > at:
        return response(ACTION_WAIT, "retry_backoff", retry_not_before=retry_at.isoformat())
    lease_expires = _parse_time(runtime["lease_expires_at"])
    if (
        runtime["lease_owner"]
        and str(runtime["lease_owner"]) != str(lease_owner or "")
        and (lease_expires is None or lease_expires > at)
    ):
        return response(
            ACTION_WAIT,
            "lease_active",
            phase=runtime["lease_phase"],
            lease_expires_at=runtime["lease_expires_at"],
        )
    if round_row is None:
        return response(ACTION_WAIT, "round_missing")

    status = str(round_row["status"])
    if status == "waiting":
        total = one(conn, "SELECT COUNT(*) AS n FROM companies")
        ready = one(
            conn,
            "SELECT COUNT(*) AS n FROM companies "
            "WHERE home_city IS NOT NULL AND setup_submitted_at IS NOT NULL",
        )
        total_count = int(total["n"] if total else 0)
        ready_count = int(ready["n"] if ready else 0)
        if total_count > 0 and ready_count == total_count:
            return response(ACTION_START, "all_teams_ready")
        return response(
            ACTION_WAIT,
            "teams_not_ready",
            ready=ready_count,
            total=total_count,
        )
    if status == "paused":
        return response(ACTION_WAIT, "round_paused")
    if status == "open":
        counts = one(
            conn,
            "SELECT "
            "SUM(CASE WHEN c.is_bot=0 THEN 1 ELSE 0 END) AS player_total,"
            "SUM(CASE WHEN c.is_bot=0 AND d.submitted_at IS NOT NULL AND d.is_draft=0 THEN 1 ELSE 0 END) AS player_submitted,"
            "SUM(CASE WHEN c.is_bot=1 AND c.is_super_bot=0 THEN 1 ELSE 0 END) AS bot_total,"
            "SUM(CASE WHEN c.is_bot=1 AND c.is_super_bot=0 AND d.submitted_at IS NOT NULL AND d.is_draft=0 THEN 1 ELSE 0 END) AS bot_submitted,"
            "SUM(CASE WHEN c.is_super_bot=1 THEN 1 ELSE 0 END) AS super_total,"
            "SUM(CASE WHEN c.is_super_bot=1 AND d.company_id IS NOT NULL THEN 1 ELSE 0 END) AS super_saved,"
            "SUM(CASE WHEN c.is_super_bot=1 AND d.submitted_at IS NOT NULL AND d.is_draft=0 THEN 1 ELSE 0 END) AS super_submitted,"
            "SUM(CASE WHEN c.is_super_bot=1 AND c.is_disruptor_bot=0 THEN 1 ELSE 0 END) AS strategic_super_total,"
            "SUM(CASE WHEN c.is_disruptor_bot=1 THEN 1 ELSE 0 END) AS disruptor_total,"
            "SUM(CASE WHEN c.is_disruptor_bot=1 AND d.company_id IS NOT NULL THEN 1 ELSE 0 END) AS disruptor_saved,"
            "SUM(CASE WHEN c.is_disruptor_bot=1 AND d.submitted_at IS NOT NULL AND d.is_draft=0 THEN 1 ELSE 0 END) AS disruptor_submitted "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=?",
            (round_no,),
        )
        stats = {
            key: int(counts[key] or 0)
            for key in (
                "player_total",
                "player_submitted",
                "bot_total",
                "bot_submitted",
                "super_total",
                "super_saved",
                "super_submitted",
                "strategic_super_total",
                "disruptor_total",
                "disruptor_saved",
                "disruptor_submitted",
            )
        }
        if stats["bot_submitted"] < stats["bot_total"]:
            return response(ACTION_SUBMIT_REGULAR_BOTS, "regular_bots_missing", **stats)
        if stats["player_submitted"] < stats["player_total"]:
            return response(ACTION_WAIT, "players_not_submitted", **stats)
        processing_round = int(
            get_setting(conn, "ai_training_processing_round", 0, int)
        )
        if processing_round != int(round_no or 0):
            return response(
                ACTION_BEGIN_PROCESSING,
                "all_non_super_submitted",
                **stats,
            )
        if stats["super_saved"] < stats["super_total"]:
            return response(ACTION_ANALYZE_SUPER_BOTS, "super_bots_need_analysis", **stats)
        if stats["super_submitted"] < stats["super_total"]:
            return response(ACTION_FINALIZE_SUPER_BOTS, "super_bots_need_finalization", **stats)
        return response(ACTION_SETTLE, "all_teams_submitted", **stats)
    if status == "settled":
        transition_seconds = max(
            3,
            int(get_setting(conn, "ai_training_transition_seconds", 5, int)),
        )
        settled_at = _parse_time(round_row["settled_at"])
        reveal_at = settled_at + timedelta(seconds=transition_seconds) if settled_at else None
        if reveal_at is not None and reveal_at > at:
            return response(
                ACTION_WAIT,
                "transition_animation",
                transition_at=reveal_at.isoformat(),
            )
        if round_no is not None and round_no < 0:
            return response(ACTION_PREPARE_FIRST_ROUND, "test_round_settled")
        total_rounds = max(1, int(get_setting(conn, "total_rounds", 5, int)))
        if round_no is not None and round_no >= total_rounds:
            released = int(get_setting(conn, "final_results_release_round", 0, int))
            if released != round_no:
                return response(ACTION_RELEASE_FINAL, "final_results_locked")
            return response(ACTION_COMPLETE, "final_results_released")
        return response(ACTION_OPEN_NEXT_ROUND, "round_settled", next_round_no=(round_no or 0) + 1)
    return response(ACTION_WAIT, "unsupported_round_status")


def acquire_ai_training_lease(
    conn: sqlite3.Connection,
    phase: str,
    ttl_seconds: int = 1800,
    *,
    now: datetime | None = None,
) -> str | None:
    """Atomically claim one controller action; return an opaque owner token."""
    clean_phase = str(phase).strip()
    if not clean_phase:
        raise ValueError("AI 训练任务阶段不能为空。")
    at = _utc_now(now)
    expires = at + timedelta(seconds=max(1, int(ttl_seconds)))
    owner = f"{clean_phase}:{uuid.uuid4().hex}"
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _runtime(conn)
        cursor = conn.execute(
            "UPDATE ai_training_runtime SET lease_owner=?,lease_phase=?,lease_expires_at=?,"
            "last_action=?,updated_at=? WHERE id=1 AND enabled=1 "
            "AND (retry_not_before IS NULL OR retry_not_before<=?) "
            "AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=?)",
            (
                owner,
                clean_phase,
                expires.isoformat(),
                clean_phase,
                at.isoformat(),
                at.isoformat(),
                at.isoformat(),
            ),
        )
        acquired = cursor.rowcount == 1
        if started_transaction:
            conn.commit()
        return owner if acquired else None
    except Exception:
        if started_transaction:
            conn.rollback()
        raise


def release_ai_training_lease(
    conn: sqlite3.Connection,
    owner: str,
    error: str | None = None,
    retry_delay_seconds: int = 0,
    *,
    now: datetime | None = None,
) -> bool:
    """Release only the caller's lease and optionally record retry backoff."""
    at = _utc_now(now)
    retry_at = (
        at + timedelta(seconds=max(0, int(retry_delay_seconds)))
        if error and int(retry_delay_seconds) > 0
        else None
    )
    cursor = conn.execute(
        "UPDATE ai_training_runtime SET lease_owner=NULL,lease_phase=NULL,lease_expires_at=NULL,"
        "last_error=?,retry_not_before=?,updated_at=? WHERE id=1 AND lease_owner=?",
        (
            str(error)[:2000] if error else None,
            retry_at.isoformat() if retry_at else None,
            at.isoformat(),
            str(owner),
        ),
    )
    return cursor.rowcount == 1


def renew_ai_training_lease(
    conn: sqlite3.Connection,
    owner: str,
    ttl_seconds: int = 1800,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend a live expensive-analysis lease after each saved Bot segment."""
    at = _utc_now(now)
    expires = at + timedelta(seconds=max(1, int(ttl_seconds)))
    cursor = conn.execute(
        "UPDATE ai_training_runtime SET lease_expires_at=?,updated_at=? "
        "WHERE id=1 AND lease_owner=?",
        (expires.isoformat(), at.isoformat(), str(owner)),
    )
    return cursor.rowcount == 1


__all__ = [
    "AI_MODE",
    "MANUAL_MODE",
    "ACTION_NONE",
    "ACTION_WAIT",
    "ACTION_START",
    "ACTION_SUBMIT_REGULAR_BOTS",
    "ACTION_BEGIN_PROCESSING",
    "ACTION_ANALYZE_SUPER_BOTS",
    "ACTION_FINALIZE_SUPER_BOTS",
    "ACTION_SETTLE",
    "ACTION_PREPARE_FIRST_ROUND",
    "ACTION_OPEN_NEXT_ROUND",
    "ACTION_RELEASE_FINAL",
    "ACTION_COMPLETE",
    "get_ai_training_config",
    "set_ai_training_enabled",
    "clear_ai_training_runtime",
    "claim_ai_training_collection",
    "begin_ai_training_processing",
    "clear_ai_training_processing",
    "configure_ai_training_roster",
    "ai_training_action",
    "acquire_ai_training_lease",
    "renew_ai_training_lease",
    "release_ai_training_lease",
]
