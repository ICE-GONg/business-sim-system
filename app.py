from __future__ import annotations

import html
import importlib
import json
import logging
import os
import base64
import sqlite3
import sys
import typing
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

# Altair 5 uses the PEP 728 ``closed`` keyword before Python's built-in
# TypedDict supports it. Streamlit Cloud currently runs this app on Python 3.14,
# so use typing_extensions' compatible implementation on that runtime.
if sys.version_info[:2] == (3, 14):
    from typing_extensions import TypedDict as _TypedDict

    typing.TypedDict = _TypedDict

import altair as alt
import streamlit as st

from sim import APP_NAME
from sim import cpi as _cpi_module
from sim import db as _db_module
from sim import engine as _engine_module
from sim import bots as _bots_module

# Streamlit Community Cloud can rerun a freshly downloaded app.py inside a
# process that still has the previous internal modules cached. Refresh only
# when a newly introduced API is missing, so both hot updates and cold starts
# import one consistent version of the application.
if (
    not hasattr(_db_module, "delete_city")
    or getattr(_db_module, "DB_API_VERSION", 0) < 4
    or not hasattr(_engine_module, "current_company_net_assets")
    or not hasattr(_db_module, "rollback_latest_settled_round")
    or not hasattr(_db_module, "prepare_first_round_after_test")
    or getattr(_cpi_module, "CPI_API_VERSION", 0) < 3
    or getattr(_engine_module, "ENGINE_API_VERSION", 0) < 9
    or getattr(_bots_module, "BOT_API_VERSION", 0) < 19
):
    importlib.invalidate_caches()
    importlib.reload(_db_module)
    importlib.reload(_cpi_module)
    importlib.reload(_engine_module)
    importlib.reload(_bots_module)

from sim.bots import submit_bot_decisions, submit_super_bot_decisions

from sim.db import (
    all_rows,
    connect,
    current_round,
    database_bytes,
    delete_city,
    delete_company,
    delete_companies,
    employee_count,
    get_setting,
    hash_password,
    now_iso,
    one,
    prepare_first_round_after_test,
    reset_competition,
    rollback_latest_settled_round,
    restore_database_bytes,
    set_setting,
    settings_dict,
    setup_status,
    start_competition,
    submission_status,
    verify_password,
)
from sim.defaults import GLOBAL_SETTING_LABELS, MARKET_COLUMNS
from sim.engine import available_loan_limit, current_company_net_assets, loan_ceiling_for_round, market_size, settle_round, weighted_market_average
from sim.report_pdf import build_round_report_pdf


LOGGER = logging.getLogger(__name__)


def _remote_super_bot_submit(round_no: int, replace_existing: bool = False) -> bool:
    """Run the expensive Super Bot pass in an optional Tencent SCF worker.

    The worker receives a point-in-time SQLite backup and returns the updated
    backup.  If no URL is configured, callers transparently use the local
    implementation.  A shared token can be supplied through
    ``SUPER_BOT_REMOTE_TOKEN``; it is never stored in the repository.
    """
    endpoint = os.environ.get("SUPER_BOT_REMOTE_URL", "").strip()
    if not endpoint:
        return False
    payload = {
        "db_b64": base64.b64encode(database_bytes()).decode("ascii"),
        "round_no": int(round_no),
        "replace_existing": bool(replace_existing),
        "token": os.environ.get("SUPER_BOT_REMOTE_TOKEN", ""),
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Super-Bot-Token": os.environ.get("SUPER_BOT_REMOTE_TOKEN", "")},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=860) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok") or not result.get("db_b64"):
        raise RuntimeError(str(result.get("error") or "远程超级 Bot 未返回结果"))
    restore_database_bytes(base64.b64decode(result["db_b64"]))
    return True

st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide", initial_sidebar_state="expanded")
st.markdown(
    """
    <style>
    :root { --brand:#d94141; --brand-dark:#b82f35; --ink:#252a34; --muted:#727782; --line:#e8e6e3; }
    .stApp { background: #f7f7f6; color: var(--ink); }
    .block-container { max-width: 1180px; padding-top: 2.5rem; padding-bottom: 3rem; }
    [data-testid="stSidebar"] { background: #2b303a; }
    [data-testid="stSidebar"] * { color: #f7f7f6; }
    [data-testid="stMetric"] { background: white; border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; box-shadow: 0 2px 8px rgba(25,25,25,.035); }
    div[data-testid="stForm"] { background: transparent; border: 0; padding: 0; }
    [data-testid="stExpander"] { background: white; border: 1px solid var(--line); border-radius: 12px; overflow: hidden; margin-bottom: .65rem; }
    [data-testid="stExpander"] summary { font-weight: 650; color: #85464b; }
    div[data-baseweb="input"] > div, div[data-baseweb="select"] > div { background: #f1f1ef; border-color: transparent; }
    .hero { padding: 18px 20px; border-radius: 12px; color: var(--ink); background: white;
            border: 1px solid var(--line); border-left: 5px solid var(--brand); margin-bottom: 14px; }
    .hero h1 { margin: 0 0 3px 0; font-size: 1.55rem; }
    .hero p { margin: 0; color: var(--muted); }
    .round-strip { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:15px 18px;
                   color:white; background:linear-gradient(100deg,var(--brand-dark),var(--brand)); border-radius:12px; margin-bottom:10px; }
    .round-strip .eyebrow { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; opacity:.8; }
    .round-strip .value { font-size:1.35rem; font-weight:750; }
    .round-strip .right { text-align:right; }
    .player-meta { color:var(--muted); font-size:.88rem; padding:.25rem 0 .5rem; }
    .section-note { color:var(--muted); font-size:.88rem; margin-top:-.35rem; margin-bottom:.7rem; }
    .hint { background:#fff4f2; border-left:4px solid var(--brand); padding:11px 13px; border-radius:8px; }
    .danger { background:#fff1f2; border-left:4px solid #e11d48; padding:12px 14px; border-radius:8px; }
    .report-title { border-bottom:2px solid #3f4650; padding-bottom:.35rem; margin:1.25rem 0 .6rem; font-weight:750; font-size:1.05rem; }
    .report-note { color:var(--muted); font-size:.82rem; margin:.25rem 0 .7rem; }
    .overview-card { background:#fff; border:1px solid var(--line); border-radius:14px; overflow:hidden; box-shadow:0 4px 18px rgba(30,35,45,.045); }
    .overview-main { display:grid; grid-template-columns:minmax(150px,.8fr) minmax(360px,2.2fr); gap:28px; align-items:center; padding:30px 34px 22px; }
    .current-rank { text-align:center; color:var(--brand); border-right:1px solid var(--line); padding-right:28px; }
    .current-rank .label { font-size:1rem; letter-spacing:.06em; }
    .current-rank .position { font-size:3.25rem; line-height:1.05; font-weight:760; margin-top:5px; }
    .podium { display:flex; align-items:flex-end; justify-content:center; gap:26px; min-height:142px; }
    .podium-item { flex:1; max-width:150px; text-align:center; color:var(--ink); font-weight:700; min-width:0; }
    .podium-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-bottom:8px; font-size:.88rem; }
    .podium-bar { display:flex; align-items:flex-end; justify-content:center; padding-bottom:10px; color:#fff; background:linear-gradient(180deg,#ee5149,#d93f3d); border-radius:7px 7px 0 0; font-size:1.05rem; }
    .podium-item.rank-1 .podium-bar { height:88px; }
    .podium-item.rank-2 .podium-bar { height:68px; opacity:.90; }
    .podium-item.rank-3 .podium-bar { height:54px; opacity:.80; }
    .company-profile { text-align:center; padding:4px 24px 28px; }
    .company-profile h2 { margin:0 0 12px; font-size:1.8rem; }
    .company-meta { display:flex; flex-wrap:wrap; justify-content:center; gap:10px; }
    .company-pill { background:#f7f5f2; border:1px solid var(--line); border-radius:999px; padding:7px 13px; color:#4d535d; font-size:.9rem; }
    .summary-title { background:#ef7772; color:#fff; text-align:center; font-size:1.15rem; padding:8px 12px; }
    .asset-summary { display:grid; grid-template-columns:repeat(3,1fr); padding:20px 28px 24px; }
    .asset-item { text-align:center; border-right:1px solid var(--line); }
    .asset-item:last-child { border-right:0; }
    .asset-label { color:var(--muted); font-size:.86rem; margin-bottom:5px; }
    .asset-value { color:var(--ink); font-size:1.25rem; font-weight:720; }
    .reports-strip { margin-top:16px; background:#3f76b8; color:#fff; text-align:center; padding:15px; border-radius:12px 12px 0 0; font-size:1.25rem; }
    .reports-note { background:#fff; border:1px solid var(--line); border-top:0; border-radius:0 0 12px 12px; text-align:center; color:var(--muted); padding:13px; margin-bottom:14px; }
    .ranking-card { background:#fff; border:1px solid var(--line); border-radius:14px; overflow:hidden; box-shadow:0 4px 18px rgba(30,35,45,.045); }
    .ranking-round { padding:22px 20px 16px; text-align:center; color:var(--brand); font-size:1.45rem; font-weight:800; letter-spacing:.035em; }
    .ranking-head, .ranking-row { display:grid; grid-template-columns:72px minmax(76px,.7fr) minmax(86px,.8fr) minmax(150px,1.7fr); align-items:center; column-gap:12px; padding:0 24px; }
    .ranking-head { min-height:54px; color:#9297a0; border-bottom:1px solid var(--line); font-size:.86rem; }
    .ranking-row { min-height:68px; color:var(--ink); border-bottom:1px solid #f1f2f4; }
    .ranking-row:last-child { border-bottom:0; }
    .ranking-row.is-me { background:#fff6f4; box-shadow:inset 4px 0 0 var(--brand); }
    .ranking-avatar { width:34px; height:34px; margin:auto; border-radius:50%; background:#f7f5f2; color:var(--brand); display:flex; align-items:center; justify-content:center; font-size:1rem; }
    .ranking-position { font-size:1.18rem; font-weight:800; color:var(--brand); }
    .ranking-team { font-weight:720; }
    .ranking-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:620; }
    @media (max-width: 720px) {
      .block-container { padding-left: .75rem; padding-right: .75rem; }
      .block-container h2 { font-size:1.65rem; }
      .hero { padding:14px; }
      .hero h1 { font-size:1.3rem; }
      .round-strip .value { font-size:1.1rem; }
      .overview-main { grid-template-columns:1fr; padding:22px 16px 18px; gap:20px; }
      .current-rank { border-right:0; border-bottom:1px solid var(--line); padding:0 0 20px; }
      .current-rank .position { font-size:2.7rem; }
      .podium { gap:10px; min-height:120px; }
      .podium-name { font-size:.76rem; }
      .asset-summary { padding:16px 6px 20px; }
      .asset-label { font-size:.72rem; }
      .asset-value { font-size:.92rem; }
      .ranking-head, .ranking-row { grid-template-columns:42px 62px 70px minmax(100px,1fr); column-gap:7px; padding:0 10px; }
      .ranking-head { font-size:.73rem; }
      .ranking-row { min-height:62px; font-size:.9rem; }
      .ranking-avatar { width:30px; height:30px; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


STATUS_LABELS = {"waiting": "等待赛前设置", "open": "决策开放", "paused": "已暂停", "settled": "已结算"}


def money(value: float | int | None) -> str:
    return f"¥{float(value or 0):,.0f}"


def number(value: float | int | None) -> str:
    return f"{float(value or 0):,.0f}"


def percentage(value: float | int | None) -> str:
    return f"{float(value or 0) * 100:.2f}%"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def secret_value(name: str) -> str:
    if os.environ.get(name):
        return str(os.environ[name])
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


def flash(level: str, message: str) -> None:
    st.session_state["flash"] = (level, message)


def show_flash() -> None:
    item = st.session_state.pop("flash", None)
    if not item:
        return
    level, message = item
    getattr(st, level, st.info)(message)


def hero(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="hero"><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>',
        unsafe_allow_html=True,
    )


def rank_rows(conn: sqlite3.Connection, round_no: int | None = None) -> list[dict[str, Any]]:
    if round_no is None:
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results WHERE round_no>=1")
        round_no = int(latest["n"] or 0) if latest else 0
    if round_no == 0:
        return []
    rows = all_rows(
        conn,
        "SELECT c.id,c.code,c.name,c.home_city,r.* FROM results r JOIN companies c ON c.id=r.company_id "
        "WHERE r.round_no=? ORDER BY r.net_assets DESC,c.id",
        (round_no,),
    )
    return [dict(row) | {"rank": index + 1} for index, row in enumerate(rows)]


def render_login() -> None:
    left, center, right = st.columns([1, 1.25, 1])
    with center:
        st.markdown("## 📈 阿思丹商赛模拟系统")
        st.caption("玩家决策 · 多城市 CPI 结算 · 管理员控制台")
        with st.form("login_form"):
            role_label = st.segmented_control("登录身份", ["玩家", "管理员"], default="玩家")
            account = st.text_input("账号", placeholder="例如 C01")
            password = st.text_input("密码", type="password")
            submitted = st.form_submit_button("登录", type="primary", use_container_width=True)
        if submitted:
            if role_label == "管理员":
                expected_user = secret_value("SIM_ADMIN_USER")
                expected_password = secret_value("SIM_ADMIN_PASSWORD")
                if not expected_user or not expected_password:
                    st.error("管理员登录尚未安全配置，请在部署后台设置管理员账号和密码。")
                    return
                if account == expected_user and password == expected_password:
                    st.session_state["auth"] = {"role": "admin"}
                    st.rerun()
                st.error("管理员账号或密码错误。")
            else:
                with connect() as conn:
                    company = one(conn, "SELECT * FROM companies WHERE code=?", (account.strip(),))
                    if company and verify_password(password, company["password_hash"]):
                        st.session_state["auth"] = {"role": "player", "company_id": int(company["id"])}
                        st.rerun()
                st.error("玩家账号或密码错误。")
        st.caption("初始玩家账号：C01–C04；初始密码：1234。部署前请在 Secrets 中修改管理员密码。")


def sidebar(role: str, company: sqlite3.Row | None = None) -> str:
    with st.sidebar:
        st.markdown("## 📊 商赛控制台")
        if role == "admin":
            st.caption("管理员")
            options = ["总览", "队伍管理", "决策管理", "KDS 设置", "回合控制", "赛后报表", "财富曲线", "备份与重置"]
        else:
            st.caption(f"{company['code']} · {company['name']}" if company else "玩家")
            options = ["概览", "本轮决策", "排行榜", "赛后报表", "财富曲线", "KDS"]
        page = st.radio("导航", options, label_visibility="collapsed")
        st.divider()
        if st.button("退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
    return page


def render_setup(company: sqlite3.Row) -> None:
    hero("赛前设置", "选择主场并提交公司名称；提交后由管理员统一开启第一轮。")
    if not company["home_city"]:
        with connect() as conn:
            markets = all_rows(conn, "SELECT * FROM market_config WHERE home_enabled=1 ORDER BY city")
        if not markets:
            st.error("管理员尚未配置可选主场。")
            return
        with st.form("home_setup"):
            city_names = [str(m["city"]) for m in markets]
            selected = st.radio("选择主场（确认后永久锁定）", city_names, horizontal=True)
            selected_market = markets[city_names.index(selected)]
            st.caption(
                f"{selected}：第一轮最高贷款 {money(selected_market['max_loan'])} · "
                f"零件/产品材料 {money(selected_market['component_material'])}/{money(selected_market['product_material'])}"
            )
            confirmed = st.form_submit_button("确认主场", type="primary")
        if confirmed:
            city = selected
            with connect() as conn:
                conn.execute("UPDATE companies SET home_city=?,setup_submitted_at=NULL WHERE id=?", (city, company["id"]))
                conn.execute(
                    "INSERT INTO agents(company_id,city,count) VALUES(?,?,1) "
                    "ON CONFLICT(company_id,city) DO UPDATE SET count=MAX(count,1)",
                    (company["id"], city),
                )
            flash("success", f"主场已锁定为 {city}。")
            st.rerun()
        return

    st.success(f"已锁定主场：{company['home_city']}")
    if not company["setup_submitted_at"]:
        with st.form("name_setup"):
            name = st.text_input("公司名称", value="" if str(company["name"]).startswith("待命名-") else company["name"], max_chars=40)
            confirmed = st.form_submit_button("提交并进入等待区", type="primary")
        if confirmed:
            clean_name = name.strip()
            if len(clean_name) < 2:
                st.error("公司名称至少需要 2 个字符。")
                return
            with connect() as conn:
                duplicate = one(conn, "SELECT id FROM companies WHERE lower(name)=lower(?) AND id<>?", (clean_name, company["id"]))
                if duplicate:
                    st.error("公司名称已被其他队伍使用。")
                    return
                conn.execute(
                    "UPDATE companies SET name=?,setup_submitted_at=? WHERE id=?",
                    (clean_name, now_iso(), company["id"]),
                )
            flash("success", "赛前设置已提交。")
            st.rerun()


def round_banner(round_row: sqlite3.Row | None) -> None:
    if not round_row:
        st.info("管理员尚未创建回合。")
        return
    cols = st.columns(3)
    round_label = "测试轮 -1" if int(round_row["round_no"]) < 0 else f"第 {round_row['round_no']} 轮"
    cols[0].metric("当前轮次", round_label)
    cols[1].metric("状态", STATUS_LABELS.get(round_row["status"], round_row["status"]))
    end = parse_time(round_row["ends_at"])
    if end and round_row["status"] == "open":
        remaining = end - datetime.now(timezone.utc)
        seconds = max(0, int(remaining.total_seconds()))
        cols[2].metric("剩余时间", f"{seconds // 60:02d}:{seconds % 60:02d}")
    else:
        cols[2].metric("截止时间", end.astimezone().strftime("%H:%M:%S") if end else "—")


def player_navigation(company: sqlite3.Row) -> str:
    with connect() as conn:
        round_row = current_round(conn)
    if round_row:
        end = parse_time(round_row["ends_at"])
        if end and round_row["status"] == "open":
            seconds = max(0, int((end - datetime.now(timezone.utc)).total_seconds()))
            time_value = f"{seconds // 60:02d}:{seconds % 60:02d}"
            time_label = "剩余时间"
        else:
            time_value = STATUS_LABELS.get(round_row["status"], str(round_row["status"]))
            time_label = "回合状态"
        round_value = "测试轮 -1" if int(round_row["round_no"]) < 0 else f"第 {round_row['round_no']} 轮"
    else:
        round_value, time_label, time_value = "未开始", "回合状态", "等待管理员"
    st.markdown(
        '<div class="round-strip">'
        f'<div><div class="eyebrow">Round</div><div class="value">{html.escape(round_value)}</div></div>'
        f'<div class="right"><div class="eyebrow">{html.escape(time_label)}</div><div class="value">{html.escape(time_value)}</div></div>'
        "</div>",
        unsafe_allow_html=True,
    )
    left, right = st.columns([6, 1])
    left.markdown(
        f'<div class="player-meta"><b>{html.escape(str(company["code"]))}</b> · '
        f'{html.escape(str(company["name"]))} · 主场 {html.escape(str(company["home_city"] or "未选择"))}</div>',
        unsafe_allow_html=True,
    )
    if right.button("退出", use_container_width=True, key="player_logout"):
        st.session_state.clear()
        st.rerun()
    page = st.segmented_control(
        "玩家导航",
        ["概览", "决策", "排名", "报表", "规则"],
        default="概览",
        key="player_navigation",
        label_visibility="collapsed",
    )
    st.divider()
    return str(page or "概览")


def player_setup_header(company: sqlite3.Row) -> None:
    left, right = st.columns([6, 1])
    left.caption(f"{company['code']} · 赛前设置")
    if right.button("退出", use_container_width=True, key="setup_logout"):
        st.session_state.clear()
        st.rerun()


def render_player_overview(company: sqlite3.Row) -> None:
    with connect() as conn:
        round_row = current_round(conn)
        latest = one(conn, "SELECT * FROM results WHERE company_id=? AND round_no>=1 ORDER BY round_no DESC LIMIT 1", (company["id"],))
        ranking = rank_rows(conn, int(latest["round_no"]) if latest else 0)
        my_rank = next((row["rank"] for row in ranking if row["id"] == company["id"]), None)
        workers = employee_count(conn, company["id"], "worker")
        engineers = employee_count(conn, company["id"], "engineer")
        ready = setup_status(conn)
        wealth_rows = all_rows(
            conn,
            "SELECT round_no,net_assets FROM results WHERE company_id=? AND round_no>=1 ORDER BY round_no",
            (company["id"],),
        )

    podium_items = []
    for position in range(1, 4):
        ranked = next((row for row in ranking if int(row["rank"]) == position), None)
        podium_name = f"{ranked['code']} · {ranked['name']}" if ranked else "待结算"
        podium_items.append(
            f'<div class="podium-item rank-{position}"><div class="podium-name">{html.escape(podium_name)}</div>'
            f'<div class="podium-bar">第 {position} 名</div></div>'
        )
    if latest:
        summary_title = f"Round {int(latest['round_no'])} Summary"
        total_assets = float(latest["total_assets"])
        debt = float(latest["debt"])
        net_assets = float(latest["net_assets"])
    else:
        summary_title = "Initial Assets"
        total_assets = float(company["cash"])
        debt = float(company["debt"])
        net_assets = total_assets - debt
    rank_display = f"{my_rank}" if my_rank else "—"
    st.markdown(
        '<div class="overview-card">'
        '<div class="overview-main">'
        f'<div class="current-rank"><div class="label">CURRENTLY · 当前</div><div class="position">#{rank_display}</div></div>'
        f'<div class="podium">{"".join(podium_items)}</div>'
        '</div>'
        '<div class="company-profile">'
        f'<h2>{html.escape(str(company["name"]))}</h2>'
        '<div class="company-meta">'
        f'<span class="company-pill">🪪 {html.escape(str(company["code"]))}</span>'
        f'<span class="company-pill">🏠 {html.escape(str(company["home_city"] or "未选择"))}</span>'
        f'<span class="company-pill">👥 员工 {workers + engineers:,}</span>'
        f'<span class="company-pill">📦 库存 {int(company["product_inventory"]):,}</span>'
        '</div></div>'
        f'<div class="summary-title">{html.escape(summary_title)}</div>'
        '<div class="asset-summary">'
        f'<div class="asset-item"><div class="asset-label">Total Assets · 总资产</div><div class="asset-value">{money(total_assets)}</div></div>'
        f'<div class="asset-item"><div class="asset-label">Debt · 负债</div><div class="asset-value">{money(debt)}</div></div>'
        f'<div class="asset-item"><div class="asset-label">Net Assets · 净资产</div><div class="asset-value">{money(net_assets)}</div></div>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="reports-strip">Reports · 赛后报表</div><div class="reports-note">完整回合报表请在顶部“报表”页面直接查看。</div>', unsafe_allow_html=True)
    if round_row and round_row["status"] == "waiting":
        st.info(f"已有 {ready['ready']}/{ready['total']} 支队伍完成赛前设置。全部就绪后管理员才能开始第一轮。")
    if wealth_rows:
        with st.expander("查看资产变化", expanded=False):
            wealth_frame = pd.DataFrame([dict(row) for row in wealth_rows]).rename(
                columns={"round_no": "轮次", "net_assets": "净资产"}
            )
            st.line_chart(wealth_frame.set_index("轮次"), y="净资产", y_label="净资产")


def decision_helper(conn: sqlite3.Connection, company: sqlite3.Row, round_no: int) -> dict[str, Any]:
    home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
    previous_result = one(conn, "SELECT report_json FROM results WHERE company_id=? AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1", (company["id"], round_no))
    previous_report = json.loads(previous_result["report_json"]) if previous_result else {}
    previous_hr = previous_report.get("human_resources", {})
    avg_worker = float(previous_hr.get("average_worker_salary", home["worker_initial_salary"]))
    avg_engineer = float(previous_hr.get("average_engineer_salary", home["engineer_initial_salary"]))
    salary_max = get_setting(conn, "salary_max", 10_000.0)
    research = get_setting(conn, "research_75", 6_000_000.0) / 0.75 * 1.10 + get_setting(conn, "research_buffer", 150_000.0)
    return {
        "worker_wage": min(salary_max, (avg_worker + 100) * 1.10),
        "engineer_wage": min(salary_max, (avg_engineer + 100) * 1.10),
        "research": research,
    }


def previous_salary(conn: sqlite3.Connection, company: sqlite3.Row, round_no: int, field: str, fallback: float) -> float:
    if field not in {"worker_salary", "engineer_salary"}:
        raise ValueError("工资字段无效。")
    row = one(
        conn,
        f"SELECT d.{field} AS salary FROM decisions d JOIN results r ON r.company_id=d.company_id AND r.round_no=d.round_no "
        "WHERE d.company_id=? AND d.round_no>=1 AND d.round_no<? ORDER BY d.round_no DESC LIMIT 1",
        (company["id"], round_no),
    )
    return float(row["salary"]) if row else float(fallback)


def salary_bounds(settings: dict[str, Any], previous: float) -> tuple[float, float]:
    minimum = float(settings["salary_min"])
    maximum = float(settings["salary_max"])
    limit = max(0.0, float(settings["salary_change_limit"]))
    reference = min(max(float(previous), minimum), maximum)
    return max(minimum, reference - limit), min(maximum, reference + limit)


def render_submitted_decision(
    decision: dict[str, Any],
    city_values: dict[str, dict[str, Any]],
    loan_base_net_assets: float,
    loan_limit: float,
) -> None:
    """Show the player's locked submission without rendering editable widgets."""
    st.success(f"本轮决策已提交并锁定；提交时间：{str(decision['submitted_at'])[:19].replace('T', ' ')} UTC")
    loan_cols = st.columns(3)
    loan_cols[0].metric("贷款计算净资产", money(loan_base_net_assets))
    loan_cols[1].metric("本轮最高新增贷款", money(loan_limit))
    loan_cols[2].metric("本轮贷款变化", money(decision["loan_change"]))
    st.markdown("#### 人力与生产")
    st.dataframe(
        pd.DataFrame(
            [
                {"岗位": "工人", "增减": int(decision["worker_delta"]), "月薪": float(decision["worker_salary"])},
                {"岗位": "工程师", "增减": int(decision["engineer_delta"]), "月薪": float(decision["engineer_salary"])},
            ]
        ),
        hide_index=True,
        use_container_width=True,
        column_config={"月薪": st.column_config.NumberColumn(format="¥ %.0f")},
    )
    investment_cols = st.columns(4)
    investment_cols[0].metric("计划生产", number(decision["production_volume"]))
    investment_cols[1].metric("MA", money(decision["management_investment"]))
    investment_cols[2].metric("QI", money(decision["quality_investment"]))
    investment_cols[3].metric("专利投入", money(decision["research_investment"]))
    st.markdown("#### 城市销售决策")
    city_rows = [
        {
            "城市": city,
            "Agent 增减": int(values.get("agent_delta", 0)),
            "营销投入": float(values.get("marketing_investment", 0.0)),
            "售价": float(values.get("price", 0.0)),
            "购买市场报告": bool(values.get("order_report", 0)),
        }
        for city, values in city_values.items()
    ]
    st.dataframe(
        pd.DataFrame(city_rows),
        hide_index=True,
        use_container_width=True,
        column_config={
            "营销投入": st.column_config.NumberColumn(format="¥ %.0f"),
            "售价": st.column_config.NumberColumn(format="¥ %.0f"),
        },
    )


def render_player_decision(company: sqlite3.Row) -> None:
    hero("本轮决策", "提交前请仔细确认；提交后系统会锁定决策，管理员仍可在后台代为修正。")
    with connect() as conn:
        round_row = current_round(conn)
        if not round_row:
            st.info("暂无回合。")
            return
        end = parse_time(round_row["ends_at"])
        round_no = int(round_row["round_no"])
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
        home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        current_workers = employee_count(conn, company["id"], "worker")
        current_engineers = employee_count(conn, company["id"], "engineer")
        previous_worker_salary = previous_salary(conn, company, round_no, "worker_salary", float(home["worker_initial_salary"]))
        previous_engineer_salary = previous_salary(conn, company, round_no, "engineer_salary", float(home["engineer_initial_salary"]))
        decision_row = one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        decision = dict(decision_row) if decision_row else {
            "loan_change": 0.0,
            "worker_delta": 0,
            "worker_salary": previous_worker_salary,
            "engineer_delta": 0,
            "engineer_salary": previous_engineer_salary,
            "management_investment": 0.0,
            "production_volume": 0,
            "quality_investment": 0.0,
            "research_investment": 0.0,
            "submitted_at": None,
        }
        city_values: dict[str, dict[str, Any]] = {}
        for market in markets:
            city = str(market["city"])
            saved = one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=? AND city=?", (company["id"], round_no, city))
            agent = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company["id"], city))
            city_values[city] = dict(saved) if saved else {
                "agent_delta": 0,
                "marketing_investment": 0.0,
                "price": float(market["initial_avg_price"]),
                "order_report": 0,
            }
            city_values[city]["current_agents"] = int(agent["count"]) if agent else 0
        helper = decision_helper(conn, company, round_no)
        settings = settings_dict(conn)
        worker_salary_low, worker_salary_high = salary_bounds(settings, previous_worker_salary)
        engineer_salary_low, engineer_salary_high = salary_bounds(settings, previous_engineer_salary)
        loan_base_net_assets = current_company_net_assets(conn, company)
        loan_ceiling = loan_ceiling_for_round(round_no, home, float(settings["global_max_loan"]))
        loan_limit = available_loan_limit(
            loan_base_net_assets,
            float(settings["loan_asset_threshold"]),
            float(home["min_loan"]),
            loan_ceiling,
        )
        minimum_new_loan = float(home["min_loan"])

    st.markdown(
        f'<div class="hint">工资建议：工人约 <b>{money(helper["worker_wage"])}</b>，工程师约 '
        f'<b>{money(helper["engineer_wage"])}</b>；专利投入失败会累计，真实成功概率仅管理员可见。</div>',
        unsafe_allow_html=True,
    )
    if decision.get("submitted_at"):
        render_submitted_decision(decision, city_values, loan_base_net_assets, loan_limit)
        return
    if round_row["status"] != "open":
        st.warning("当前回合未开放决策。")
        return
    if end and datetime.now(timezone.utc) > end:
        st.error("本轮提交时间已结束，请等待管理员结算。")
        return

    with st.form(f"decision_{round_no}"):
        loan_min = -float(company["debt"])
        loan_max = max(0.0, loan_limit)
        with st.expander("💰 银行贷款", expanded=False):
            st.markdown(
                f'<div class="section-note">计算净资产 {money(loan_base_net_assets)} ÷ 阈值 {money(settings["loan_asset_threshold"])} '
                f'× 本轮封顶基数 {money(loan_ceiling)}；本轮可新增至多 {money(loan_limit)}，新增贷款最少 {money(minimum_new_loan)}。负数为还款。</div>',
                unsafe_allow_html=True,
            )
            loan_change = st.number_input(
                "贷款变化",
                min_value=loan_min,
                max_value=loan_max,
                value=min(max(float(decision["loan_change"]), loan_min), loan_max),
                step=10_000.0,
            )

        with st.expander("👥 人力资源", expanded=True):
            worker_left, worker_right = st.columns(2)
            worker_delta = worker_left.number_input(
                "工人增减",
                min_value=-current_workers,
                value=int(decision["worker_delta"]),
                step=1,
            )
            worker_salary = worker_right.number_input(
                "工人月薪",
                min_value=worker_salary_low,
                max_value=worker_salary_high,
                value=min(max(float(decision["worker_salary"]), worker_salary_low), worker_salary_high),
                step=50.0,
            )
            engineer_left, engineer_right = st.columns(2)
            engineer_delta = engineer_left.number_input(
                "工程师增减",
                min_value=-current_engineers,
                value=int(decision["engineer_delta"]),
                step=1,
            )
            engineer_salary = engineer_right.number_input(
                "工程师月薪",
                min_value=engineer_salary_low,
                max_value=engineer_salary_high,
                value=min(max(float(decision["engineer_salary"]), engineer_salary_low), engineer_salary_high),
                step=50.0,
            )
            st.caption(
                f"当前：工人 {current_workers:,}、工程师 {current_engineers:,}。新员工按全局 KDS 收取培训费；第三轮起老员工享受经验倍率。"
            )

        with st.expander("🏭 生产与研发", expanded=True):
            production_left, production_right = st.columns(2)
            production_volume = production_left.number_input(
                "计划生产量",
                min_value=0,
                value=int(decision["production_volume"]),
                step=1,
            )
            management = production_right.number_input(
                "管理投入（MA）",
                min_value=0.0,
                value=float(decision["management_investment"]),
                step=10_000.0,
            )
            quality_left, research_right = st.columns(2)
            quality = quality_left.number_input(
                "品质投入（QI）",
                min_value=0.0,
                value=float(decision["quality_investment"]),
                step=10_000.0,
            )
            research = research_right.number_input(
                "研发 / 专利投入（R&D）",
                min_value=0.0,
                value=float(decision["research_investment"]),
                step=10_000.0,
            )

        st.markdown("### 🏙️ 城市销售")
        st.caption("每个城市单独设置 Agent、营销投入和售价；按需购买市场报告。")
        city_inputs: dict[str, dict[str, Any]] = {}
        for market in markets:
            city = str(market["city"])
            values = city_values[city]
            capacity = number(market_size(market, round_no, float(settings["market_growth"])))
            with st.expander(f"{city}　Agent {values['current_agents']}　容量约 {capacity}"):
                c1, c2, c3, c4 = st.columns(4)
                agent_delta = c1.number_input(
                    "Agent 增减",
                    min_value=-int(values["current_agents"]),
                    max_value=int(settings["max_agent_add_per_city_round"]),
                    value=int(values["agent_delta"]),
                    step=1,
                    key=f"agent_{round_no}_{city}",
                )
                marketing = c2.number_input(
                    "营销投入（MI）",
                    min_value=0.0,
                    value=float(values["marketing_investment"]),
                    step=10_000.0,
                    key=f"mi_{round_no}_{city}",
                )
                city_max = min(float(settings["price_max"]), float(market["max_price"]))
                saved_price = min(max(float(values["price"]), float(settings["price_min"])), city_max)
                price = c3.number_input(
                    "售价",
                    min_value=float(settings["price_min"]),
                    max_value=city_max,
                    value=saved_price,
                    step=50.0,
                    key=f"price_{round_no}_{city}",
                )
                order_report = c4.checkbox("购买市场报告", value=bool(values["order_report"]), key=f"report_{round_no}_{city}")
                city_inputs[city] = {
                    "agent_delta": int(agent_delta),
                    "marketing_investment": float(marketing),
                    "price": float(price),
                    "order_report": int(order_report),
                    "current_agents": int(values["current_agents"]),
                }
        submitted = st.form_submit_button("保存并提交本轮决策", type="primary", use_container_width=True)

    if submitted:
        errors: list[str] = []
        if 0 < float(loan_change) < minimum_new_loan:
            errors.append(f"新增贷款不得低于主场最低贷款 {money(minimum_new_loan)}；不贷款请填写 0。")
        for city, values in city_inputs.items():
            if values["agent_delta"] > int(settings["max_agent_add_per_city_round"]):
                errors.append(f"{city} 每轮最多新增 {int(settings['max_agent_add_per_city_round'])} 个 Agent。")
            if values["current_agents"] + values["agent_delta"] < 0:
                errors.append(f"{city} 的 Agent 数量不能为负数。")
        if errors:
            for error in errors:
                st.error(error)
            return
        with connect() as conn:
            latest_round = current_round(conn)
            latest_end = parse_time(latest_round["ends_at"]) if latest_round else None
            if not latest_round or latest_round["status"] != "open" or int(latest_round["round_no"]) != round_no or (latest_end and datetime.now(timezone.utc) > latest_end):
                st.error("回合状态已经变化，本次提交未保存。")
                return
            already_submitted = one(
                conn,
                "SELECT submitted_at FROM decisions WHERE company_id=? AND round_no=? AND submitted_at IS NOT NULL",
                (company["id"], round_no),
            )
            if already_submitted:
                st.warning("本轮决策已经提交并锁定，本次修改未保存。")
                return
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,engineer_salary,"
                "management_investment,production_volume,quality_investment,research_investment,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(company_id,round_no) DO UPDATE SET loan_change=excluded.loan_change,worker_delta=excluded.worker_delta,"
                "worker_salary=excluded.worker_salary,engineer_delta=excluded.engineer_delta,engineer_salary=excluded.engineer_salary,"
                "management_investment=excluded.management_investment,production_volume=excluded.production_volume,"
                "quality_investment=excluded.quality_investment,research_investment=excluded.research_investment,submitted_at=excluded.submitted_at",
                (
                    company["id"], round_no, loan_change, int(worker_delta), worker_salary, int(engineer_delta), engineer_salary,
                    management, int(production_volume), quality, research, now_iso(),
                ),
            )
            for city, values in city_inputs.items():
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,round_no,city) DO UPDATE SET agent_delta=excluded.agent_delta,"
                    "marketing_investment=excluded.marketing_investment,price=excluded.price,order_report=excluded.order_report",
                    (company["id"], round_no, city, values["agent_delta"], values["marketing_investment"], values["price"], values["order_report"]),
                )
        flash("success", "本轮决策已提交并锁定。")
        st.rerun()


def ranking_table(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "排名": row["rank"],
                "队伍": row["code"],
                "公司": row["name"],
                "主场": row["home_city"],
                "净资产": row["net_assets"],
                "现金": row["cash"],
                "本轮利润": row["net_profit"],
                "售出": row["sold"],
                "库存": row["inventory"],
            }
            for row in rows
        ]
    )


def render_ranking(admin: bool = False) -> None:
    if admin:
        hero("财富排行榜", "按当轮结束后的 Net Assets 排序；Net Assets = 总资产（含期末库存价值）− 负债，所有费用均已计入。")
    with connect() as conn:
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results")
        latest_round = int(latest["n"] or 0) if latest else 0
        if not latest_round:
            st.info("暂无已结算回合。")
            return
        round_numbers = [int(row["round_no"]) for row in all_rows(conn, "SELECT DISTINCT round_no FROM results ORDER BY round_no DESC")]
        selected = st.selectbox("选择轮次", round_numbers, index=0)
        rows = rank_rows(conn, selected)
    if not admin:
        current_company_id = int(st.session_state.get("auth", {}).get("company_id", 0))
        ranking_rows = []
        for row in rows:
            mine = " is-me" if int(row["id"]) == current_company_id else ""
            ranking_rows.append(
                f'<div class="ranking-row{mine}">'
                '<div class="ranking-avatar">●</div>'
                f'<div class="ranking-position">{int(row["rank"])}</div>'
                f'<div class="ranking-team">{html.escape(str(row["code"]))}</div>'
                f'<div class="ranking-name">{html.escape(str(row["name"]))}</div>'
                '</div>'
            )
        st.markdown(
            '<div class="ranking-card">'
            f'<div class="ranking-round">ROUND {int(selected)}</div>'
            '<div class="ranking-head"><div>头像</div><div>排名</div><div>队伍</div><div>公司名称</div></div>'
            f'{"".join(ranking_rows)}'
            '</div>',
            unsafe_allow_html=True,
        )
        st.caption("排行榜仅公开名次、队伍编号和公司名称；经营数据仅本人及管理员可见。")
        return
    frame = ranking_table(rows)
    st.dataframe(
        frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "净资产": st.column_config.NumberColumn(format="¥ %.0f"),
            "现金": st.column_config.NumberColumn(format="¥ %.0f"),
            "本轮利润": st.column_config.NumberColumn(format="¥ %.0f"),
        },
    )


def render_report_detail(conn: sqlite3.Connection, company_id: int, round_no: int, admin: bool) -> None:
    company = one(conn, "SELECT * FROM companies WHERE id=?", (company_id,))
    row = one(conn, "SELECT * FROM results WHERE company_id=? AND round_no=?", (company_id, round_no))
    if not company or not row:
        st.error("未找到报表。")
        return
    report = json.loads(row["report_json"])
    ranking = rank_rows(conn, round_no)
    my_rank = next((item["rank"] for item in ranking if item["id"] == company_id), "—")
    metrics = report["key_metrics"]
    st.markdown(f"### {company['code']} · {company['name']}　｜　第 {round_no} 轮报表")
    st.markdown('<div class="report-title">关键指标 Key Metrics</div>', unsafe_allow_html=True)
    cols = st.columns(4)
    cols[0].metric("总资产", money(metrics["total_assets"]))
    cols[1].metric("负债", money(metrics["debt"]))
    cols[2].metric("净资产 / Net Assets", money(metrics["net_assets"]))
    cols[3].metric("排名", f"#{my_rank}")
    cols = st.columns(3)
    cols[0].metric("销售收入", money(metrics["sales_revenue"]))
    cols[1].metric("总成本", money(metrics["cost"]))
    cols[2].metric("净利润", money(metrics["net_profit"]))
    st.markdown('<div class="report-note">净利润 = 销售收入 − 全部成本；排名依据 Net Assets（总资产 − 负债），专利、市场报告、运输费、税费和贷款利息均已计入。</div>', unsafe_allow_html=True)

    st.markdown('<div class="report-title">财务 Finance</div>', unsafe_allow_html=True)
    finance = report["finance"]
    start_debt = float(finance.get("starting_debt", float(metrics["debt"]) - float(finance.get("loan_change", 0.0)) - float(finance.get("interest", 0.0))))
    cash_running = float(finance["round_begins"])
    debt_running = start_debt
    finance_items = [
        ("期初 / Round begins", 0.0, 0.0),
        ("银行贷款 / Bank loan", float(finance.get("loan_change", 0.0)), float(finance.get("loan_change", 0.0))),
        ("工人工资 / Workers salary", -float(finance.get("worker_wages", finance.get("wages", 0.0))), 0.0),
        ("工程师工资 / Engineers salary", -float(finance.get("engineer_wages", 0.0)), 0.0),
        ("裁员费用 / Layoff", -float(finance.get("layoff_cash", finance.get("layoff", 0.0))), float(finance.get("layoff_debt", 0.0))),
        ("离职补偿 / Quit compensation", -float(finance.get("quit_penalty_cash", finance.get("quit_penalty", 0.0))), float(finance.get("quit_penalty_debt", 0.0))),
        ("培训费用 / Training", -float(finance.get("training", 0.0)), 0.0),
        ("零件材料 / Components material", -float(finance.get("component_material", finance.get("materials", 0.0))), 0.0),
        ("零件仓储 / Components storage", -float(finance.get("component_storage", finance.get("storage", 0.0))), 0.0),
        ("产品材料 / Products material", -float(finance.get("product_material", 0.0)), 0.0),
        ("产品仓储 / Products storage", -float(finance.get("product_storage", 0.0)), 0.0),
        ("Agent 变更", -float(finance.get("agents", 0.0)), 0.0),
        ("营销投入 / Marketing", -float(finance.get("marketing", 0.0)), 0.0),
        ("品质投入 / Quality", -float(finance.get("quality", 0.0)), 0.0),
        ("管理投入 / Management", -float(finance.get("management", 0.0)), 0.0),
        ("销售收入 / Sales revenue", float(metrics["sales_revenue"]), 0.0),
        ("研发投入 / Research", -float(finance.get("research", 0.0)), 0.0),
        ("跨城运输 / Transportation", -float(finance.get("transport", 0.0)), 0.0),
        ("市场报告 / Market report", -float(finance.get("market_reports", 0.0)), 0.0),
        ("贷款利息 / Debt interest", 0.0, float(finance.get("interest", 0.0))),
        ("税费 / Tax", -float(finance.get("tax", 0.0)), 0.0),
    ]
    finance_rows = []
    for label, cash_flow, debt_change in finance_items:
        cash_running += cash_flow
        debt_running += debt_change
        finance_rows.append({"项目": label, "现金流": cash_flow, "现金余额": cash_running, "负债变化": debt_change, "负债余额": debt_running})
    st.dataframe(pd.DataFrame(finance_rows), hide_index=True, use_container_width=True, column_config={key: st.column_config.NumberColumn(format="¥ %.0f") for key in ("现金流", "现金余额", "负债变化", "负债余额")})

    hr = report["human_resources"]
    st.markdown('<div class="report-title">人力资源 Human Resources</div>', unsafe_allow_html=True)
    hr_frame = pd.DataFrame([
        {"岗位": "工人 Workers", "期初": hr.get("previous_workers", max(0, int(hr["workers"]) - int(hr.get("worker_delta", 0)))), "增减": hr.get("worker_delta", 0), "当前": hr["workers"], "有效人数": hr["effective_workers"], "月薪": hr["worker_salary"], "平均工资": hr.get("average_worker_salary", 0), "工资倍率": hr["worker_wage_multiplier"]},
        {"岗位": "工程师 Engineers", "期初": hr.get("previous_engineers", max(0, int(hr["engineers"]) - int(hr.get("engineer_delta", 0)))), "增减": hr.get("engineer_delta", 0), "当前": hr["engineers"], "有效人数": hr["effective_engineers"], "月薪": hr["engineer_salary"], "平均工资": hr.get("average_engineer_salary", 0), "工资倍率": hr["engineer_wage_multiplier"]},
    ])
    st.dataframe(hr_frame, hide_index=True, use_container_width=True, column_config={"月薪": st.column_config.NumberColumn(format="¥ %.0f"), "平均工资": st.column_config.NumberColumn(format="¥ %.0f"), "工资倍率": st.column_config.NumberColumn(format="%.2f")})
    if hr.get("rows"):
        employee_labels = {
            "Inexperienced Workers": "非熟练工人",
            "Experienced Workers": "熟练工人",
            "Inexperienced Engineers": "非熟练工程师",
            "Experienced Engineers": "熟练工程师",
        }
        detail_rows = pd.DataFrame([
            {
                "员工类别": employee_labels.get(item.get("employee", ""), item.get("employee", "")),
                "期初": item.get("previous", 0),
                "主动裁员": item.get("laid", 0),
                "低工资离职": item.get("quitted", 0),
                "新增": item.get("added", 0),
                "晋升熟练": item.get("promoted", 0),
                "期末在岗": item.get("working", 0),
            }
            for item in hr["rows"]
        ])
        st.dataframe(detail_rows, hide_index=True, use_container_width=True)
    st.markdown('<div class="report-note">工资低于本主场平均值时员工会按比例离职，并按两个月本轮工资补偿；新员工收取培训费，主动裁员按一个月工资补偿。</div>', unsafe_allow_html=True)

    production = report["production"]
    st.markdown('<div class="report-title">管理与生产 Management / Production</div>', unsafe_allow_html=True)
    management_frame = pd.DataFrame([{"管理投入": finance.get("management", 0.0), "管理指数": production.get("ma_index", row["ma_index"]), "品质投入": finance.get("quality", 0.0), "品质指数": production.get("qi_index", row["qi_index"])}])
    st.dataframe(management_frame, hide_index=True, use_container_width=True, column_config={"管理投入": st.column_config.NumberColumn(format="¥ %.0f"), "品质投入": st.column_config.NumberColumn(format="¥ %.0f")})
    product_frame = pd.DataFrame([
        {"项目": "零件 Components", "计划": int(production.get("planned", 0)) * int(production.get("components_per_product", 7)), "期初": production.get("old_components", 0), "本轮生产": production.get("components", int(production.get("produced", 0)) * 7), "总量": production.get("old_components", 0) + production.get("components", int(production.get("produced", 0)) * 7), "使用/售出": production.get("component_used", production.get("components", int(production.get("produced", 0)) * 7)), "结余": production.get("component_surplus", 0)},
        {"项目": "产品 Products", "计划": production.get("planned", 0), "期初": production.get("old_products", 0), "本轮生产": production.get("produced", 0), "总量": production.get("old_products", 0) + production.get("produced", 0), "使用/售出": production.get("sold", 0), "结余": production.get("surplus", 0)},
    ])
    st.dataframe(product_frame, hide_index=True, use_container_width=True)
    if production.get("bottleneck"):
        st.markdown(
            f'<div class="report-note">生产结果：{html.escape(str(production["bottleneck"]))}。'
            f'工程师最多可合成 {int(production.get("engineer_capacity_units", 0))} 件，'
            f'现有零件最多可合成 {int(production.get("component_capacity_units", 0))} 件。</div>',
            unsafe_allow_html=True,
        )
    if "component_storage_before" in production:
        storage_frame = pd.DataFrame([
            {"仓储": "零件", "扩容前": production["component_storage_before"], "扩容后": production["component_storage_after"], "新增容量": production["component_storage_increase"]},
            {"仓储": "产品", "扩容前": production["product_storage_before"], "扩容后": production["product_storage_after"], "新增容量": production["product_storage_increase"]},
        ])
        st.dataframe(storage_frame, hide_index=True, use_container_width=True)

    research = report["research"]
    st.markdown('<div class="report-title">研发 Research Investment</div>', unsafe_allow_html=True)
    research_record = {"本轮投入": research["investment"], "累计研发投入": research.get("accumulated_for_probability", research["investment"]), "本轮结果": "获得专利" if research["success"] else "未获得专利", "累计专利": research["patents_after"]}
    if admin:
        research_record["真实成功概率"] = research["probability"] * 100
    research_frame = pd.DataFrame([research_record])
    research_columns = {"本轮投入": st.column_config.NumberColumn(format="¥ %.0f"), "累计研发投入": st.column_config.NumberColumn(format="¥ %.0f")}
    if admin:
        research_columns["真实成功概率"] = st.column_config.NumberColumn(format="%.1f%%")
    st.dataframe(research_frame, hide_index=True, use_container_width=True, column_config=research_columns)
    if research.get("success"):
        st.caption(f"本轮获得的专利从第 {research.get('effective_from_round', round_no + 1)} 轮开始降低材料成本，本轮生产成本不受影响。")

    st.markdown('<div class="report-title">销售 Sales</div>', unsafe_allow_html=True)
    sales_frame = pd.DataFrame([{"市场": item["city"], "Agent": item["agents"], "竞争力 CPI%": item["cpi"], "销售量": item["sold"], "市场份额%": item["market_share"] * 100, "售价": item["price"], "销售收入": item["sold"] * item["price"], "营销投入": item["marketing"]} for item in report["sales"]])
    st.dataframe(sales_frame, hide_index=True, use_container_width=True, column_config={"竞争力 CPI%": st.column_config.NumberColumn(format="%.2f%%"), "市场份额%": st.column_config.NumberColumn(format="%.2f%%"), "售价": st.column_config.NumberColumn(format="¥ %.0f"), "销售收入": st.column_config.NumberColumn(format="¥ %.0f"), "营销投入": st.column_config.NumberColumn(format="¥ %.0f")})

    if admin:
        visible_cities = {str(item["city"]) for item in all_rows(conn, "SELECT city FROM market_config")}
    else:
        visible_cities = {str(item["city"]) for item in report["sales"] if item.get("report_purchased")}
        if not any("report_purchased" in item for item in report["sales"]):
            visible_cities = {str(item["city"]) for item in all_rows(conn, "SELECT city FROM city_decisions WHERE company_id=? AND round_no=? AND order_report=1", (company_id, round_no))}
    pdf_market_sections: list[dict[str, Any]] = []
    for city in sorted(visible_cities):
        all_market_rows = all_rows(conn, "SELECT c.code,c.name,cr.*,r.ma_index,r.qi_index,a.count AS current_agents FROM city_results cr JOIN companies c ON c.id=cr.company_id JOIN results r ON r.company_id=cr.company_id AND r.round_no=cr.round_no LEFT JOIN agents a ON a.company_id=cr.company_id AND a.city=cr.city WHERE cr.round_no=? AND cr.city=? ORDER BY cr.market_share DESC", (round_no, city))
        market_rows = []
        for market_row in all_market_rows:
            stored_breakdown = json.loads(market_row["breakdown_json"]) if market_row["breakdown_json"] else {}
            if int(stored_breakdown.get("agents", 0)) > 0:
                market_rows.append(market_row)
        stats = one(conn, "SELECT * FROM market_round_stats WHERE round_no=? AND city=?", (round_no, city))
        market_config = one(conn, "SELECT * FROM market_config WHERE city=?", (city,))
        city_sale = next((item for item in report["sales"] if item["city"] == city), {})
        report_market_size = float(stats["market_size"]) if stats else float(city_sale.get("market_size", 0.0))
        total_volume = float(stats["player_total_volume"]) if stats else float(sum(int(item["sold"]) for item in market_rows))
        base_average = float(stats["base_average_price"]) if stats else float(market_config["initial_avg_price"] if market_config else 0.0)
        average_price = float(stats["average_price"]) if stats else weighted_market_average(base_average, report_market_size, [(float(item["price"]), float(item["sold"])) for item in market_rows])
        st.markdown(f'<div class="report-title">市场报告 Market Report · {html.escape(city)}</div>', unsafe_allow_html=True)
        stat_cols = st.columns(5)
        stat_cols[0].metric("人口", number(market_config["population"] if market_config else 0))
        stat_cols[1].metric("渗透率", percentage(market_config["penetration"] if market_config else 0))
        stat_cols[2].metric("市场大小", number(report_market_size))
        stat_cols[3].metric("总销售量", number(total_volume))
        stat_cols[4].metric("市场均价", money(average_price))
        market_records = []
        for item in market_rows:
            breakdown = json.loads(item["breakdown_json"]) if item["breakdown_json"] else {}
            market_records.append({"队伍": item["code"], "公司": item["name"], "管理指数": item["ma_index"], "Agent": breakdown.get("agents", item["current_agents"] or 0), "营销投入": item["marketing"], "品质指数": item["qi_index"], "CPI%": item["cpi"], "售价": item["price"], "销售量": item["sold"], "市场份额%": item["market_share"] * 100})
        market_frame = pd.DataFrame(market_records)
        st.dataframe(market_frame, hide_index=True, use_container_width=True, column_config={"营销投入": st.column_config.NumberColumn(format="¥ %.0f"), "CPI%": st.column_config.NumberColumn(format="%.2f%%"), "售价": st.column_config.NumberColumn(format="¥ %.0f"), "市场份额%": st.column_config.NumberColumn(format="%.2f%%")})
        pdf_market_sections.append({
            "city": city, "population": market_config["population"] if market_config else 0,
            "penetration": market_config["penetration"] if market_config else 0, "market_size": report_market_size,
            "total_volume": total_volume, "average_price": average_price,
            "rows": [{"code": item["队伍"], "ma_index": item["管理指数"], "agents": item["Agent"], "marketing": item["营销投入"], "qi_index": item["品质指数"], "cpi": item["CPI%"], "price": item["售价"], "sold": item["销售量"], "market_share": item["市场份额%"] / 100} for item in market_records],
        })
        st.caption("均价 = [Σ(玩家价格 × 对应售货量) + 基准均价 × (市场大小 − 玩家总售货量)] ÷ 市场大小")
    try:
        pdf_bytes = build_round_report_pdf(dict(company), round_no, report, my_rank, pdf_market_sections)
        st.download_button("下载官方格式 PDF 报表", pdf_bytes, file_name=f"Round_{round_no}_{company['code']}_Report.pdf", mime="application/pdf", use_container_width=True)
    except Exception:
        LOGGER.exception("PDF report generation failed")
        st.error("PDF 报表生成失败，请联系管理员查看后台日志。")


def render_reports(company: sqlite3.Row | None, admin: bool = False) -> None:
    hero("赛后报表", "查看现金流、生产、人力、城市 CPI 分解和专利结果。")
    with connect() as conn:
        if admin:
            companies = all_rows(conn, "SELECT * FROM companies ORDER BY code")
            if not companies:
                st.info("暂无队伍。")
                return
            labels = [f"{row['code']} · {row['name']}" for row in companies]
            selected_label = st.selectbox("队伍", labels)
            selected_company = companies[labels.index(selected_label)]
        else:
            selected_company = company
        rounds = all_rows(conn, "SELECT round_no FROM results WHERE company_id=? ORDER BY round_no DESC", (selected_company["id"],))
        if not rounds:
            st.info("暂无已结算报表。")
            return
        round_no = st.selectbox("轮次", [int(row["round_no"]) for row in rounds])
        render_report_detail(conn, int(selected_company["id"]), int(round_no), admin)


def render_wealth(company: sqlite3.Row | None, admin: bool = False) -> None:
    hero("财富曲线", "按照官方样式对比全部队伍每轮 Net Assets（总资产 − 负债）。")
    with connect() as conn:
        rows = all_rows(
            conn,
            "SELECT c.id,c.code,c.name,r.round_no,r.net_assets FROM results r JOIN companies c ON c.id=r.company_id "
            "WHERE r.round_no>=1 ORDER BY r.round_no,c.id",
        )
        companies = all_rows(conn, "SELECT id,code,name FROM companies ORDER BY id")
        initial_cash = float(get_setting(conn, "initial_cash", 6_500_000.0))
    if not rows:
        st.info("暂无已结算数据。")
        return
    records = [{"队伍": f"{row['code']} · {row['name']}", "轮次": int(row["round_no"]), "净资产": float(row["net_assets"])} for row in rows]
    present_ids = {int(row["id"]) for row in rows}
    records.extend({"队伍": f"{item['code']} · {item['name']}", "轮次": 0, "净资产": initial_cash} for item in companies if int(item["id"]) in present_ids)
    frame = pd.DataFrame(records)
    max_round = int(frame["轮次"].max())
    latest = frame.sort_values("轮次").groupby("队伍", as_index=False).tail(1).sort_values("净资产", ascending=False)
    ranking_order = latest["队伍"].tolist()
    chart = (
        alt.Chart(frame)
        .mark_line(point=alt.OverlayMarkDef(size=58), strokeWidth=2.2)
        .encode(
            x=alt.X("轮次:Q", title="Round", scale=alt.Scale(domain=[0, max_round], nice=False), axis=alt.Axis(values=list(range(0, max_round + 1)), tickMinStep=1)),
            y=alt.Y("净资产:Q", title="Net Assets (RMB)", scale=alt.Scale(zero=False), axis=alt.Axis(format=",")),
            color=alt.Color("队伍:N", title=None, sort=ranking_order, legend=alt.Legend(orient="right")),
            tooltip=[alt.Tooltip("队伍:N"), alt.Tooltip("轮次:Q", format=".0f"), alt.Tooltip("净资产:Q", format=",.0f")],
        )
        .properties(height=520, title="Chart for Simulation")
    )
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(latest[["队伍", "轮次", "净资产"]], hide_index=True, use_container_width=True, column_config={"净资产": st.column_config.NumberColumn(format="¥ %.0f")})


def render_player_kds(company: sqlite3.Row) -> None:
    hero("KDS · Key Data Sheet", "本场比赛公开参数；数值由管理员统一设置。")
    with connect() as conn:
        settings = settings_dict(conn)
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
    st.markdown(f'<div class="report-note" style="text-align:right">Initial Cash · 初始现金：<b>{money(settings["initial_cash"])}</b></div>', unsafe_allow_html=True)
    st.markdown('<div class="report-title">Markets Details · 城市参数</div>', unsafe_allow_html=True)
    market_frame = pd.DataFrame(
        [
            {
                "城市": row["city"],
                "第一轮最高贷款": row["max_loan"],
                "利率": float(row["interest_rate"]) * 100,
                "工人初始工资": row["worker_initial_salary"],
                "工程师初始工资": row["engineer_initial_salary"],
                "零件材料单价": row["component_material"],
                "产品材料单价": row["product_material"],
                "零件仓储单价": row["component_storage"],
                "产品仓储单价": row["product_storage"],
                "人口": row["population"],
                "初始渗透率": float(row["penetration"]) * 100,
                "初始均价": row["initial_avg_price"],
            }
            for row in markets
        ]
    )
    st.dataframe(
        market_frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "第一轮最高贷款": st.column_config.NumberColumn(format="¥ %.0f"),
            "利率": st.column_config.NumberColumn(format="%.2f%%"),
            "工人初始工资": st.column_config.NumberColumn(format="¥ %.0f"),
            "工程师初始工资": st.column_config.NumberColumn(format="¥ %.0f"),
            "零件材料单价": st.column_config.NumberColumn(format="¥ %.0f"),
            "产品材料单价": st.column_config.NumberColumn(format="¥ %.0f"),
            "零件仓储单价": st.column_config.NumberColumn(format="¥ %.0f"),
            "产品仓储单价": st.column_config.NumberColumn(format="¥ %.0f"),
            "人口": st.column_config.NumberColumn(format="%.0f"),
            "初始渗透率": st.column_config.NumberColumn(format="%.2f%%"),
            "初始均价": st.column_config.NumberColumn(format="¥ %.0f"),
        },
    )
    st.caption("Maximum loans, salaries, prices and penetrations may change from round to round.")
    st.caption("Market penetration: the number of customers interested in buying products in that market.")
    st.markdown('<div class="report-title">Equations & Ranges & Prices · 公式与范围</div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        - 1 Component · 1 个零件 = `{int(settings['component_workers'])} Inexperienced Workers + {int(settings['component_hours'])} Hours + 1 Component Material`
        - 1 Product · 1 个产品 = `{int(settings['product_engineers'])} Inexperienced Engineers + {int(settings['product_hours'])} Hours + {int(settings['components_per_product'])} Components + 1 Product Material`
        - Experienced workers and engineers produce `10%` more per unit time than inexperienced employees.
        - Training Cost：`{money(settings['worker_training_cost'])} / Worker`，`{money(settings['engineer_training_cost'])} / Engineer`
        - Product Quality Index = `Quality Investment ÷ (Old Products × 1.20 + New Products)`
        - Management Index = `Management Investment ÷ (Workers + Engineers)`
        - Salary Range：`{money(settings['salary_min'])} – {money(settings['salary_max'])}`
        - Product Price Range：`{money(settings['price_min'])} – {money(settings['price_max'])}`
        - Transportation Fee：`{money(settings['transport_cost'])} / Product`（仅主场之外实际售出的产品收取）
        - Add One Sales Agent：`{money(settings['agent_add_cost'])}`
        - Remove One Sales Agent：`{money(settings['agent_remove_cost'])}`
        - Order One Market Report：`{money(settings['report_cost'])}`
        - Research & Development：研发投入未成功时会累计到下一轮；成功后累计投入清零。真实成功概率仅管理员可见。
        - Patent：每项专利将材料成本乘以 `{float(settings['patent_factor']):.2f}`，中奖后的下一轮开始生效。
        """
    )


def render_admin_overview() -> None:
    hero("管理员总览", "统一管理队伍、KDS、回合结算与数据备份。")
    with connect() as conn:
        round_row = current_round(conn)
        companies = all_rows(conn, "SELECT * FROM companies ORDER BY id")
        setup = setup_status(conn)
        submission = submission_status(conn, int(round_row["round_no"])) if round_row and round_row["status"] in ("open", "paused") else None
        ranking = rank_rows(conn)
    round_banner(round_row)
    cols = st.columns(4)
    cols[0].metric("队伍数", len(companies))
    cols[1].metric("赛前就绪", f"{setup['ready']}/{setup['total']}")
    cols[2].metric("本轮已提交", f"{submission['submitted']}/{submission['total']}" if submission else "—")
    cols[3].metric("已结算轮次", max((int(row["round_no"]) for row in ranking), default=0))
    if ranking:
        st.subheader("最新排名")
        st.dataframe(ranking_table(ranking), hide_index=True, use_container_width=True, column_config={"净资产": st.column_config.NumberColumn(format="¥ %.0f"), "现金": st.column_config.NumberColumn(format="¥ %.0f"), "本轮利润": st.column_config.NumberColumn(format="¥ %.0f")})


def render_admin_companies() -> None:
    hero("队伍管理", "新增队伍、代选主场、修改公司名称、确认赛前就绪或重置密码。")
    with st.form("add_company", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        code = c1.text_input("队伍账号", placeholder="C05")
        name = c2.text_input("初始名称", placeholder="待命名-C05")
        password = c3.text_input("初始密码", value="1234", type="password")
        add = st.form_submit_button("新增队伍", type="primary")
    if add:
        clean_code = code.strip().upper()
        if not clean_code or not password:
            st.error("账号和密码不能为空。")
        else:
            try:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO companies(code,name,password_hash,cash,created_at) VALUES(?,?,?,?,?)",
                        (clean_code, name.strip() or f"待命名-{clean_code}", hash_password(password), get_setting(conn, "initial_cash", 15_000_000.0), now_iso()),
                    )
                flash("success", f"已新增队伍 {clean_code}。")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("账号或公司名称重复。")

    with connect() as conn:
        companies = all_rows(conn, "SELECT * FROM companies ORDER BY id")
        markets = all_rows(conn, "SELECT city FROM market_config WHERE home_enabled=1 ORDER BY city")
        round_row = current_round(conn)
    setup_editable = bool(round_row and round_row["status"] == "waiting")
    home_options = ["未选择"] + [str(row["city"]) for row in markets]
    with st.form("add_bots"):
        bot_cols = st.columns([2, 1, 2, 1])
        bot_count = bot_cols[0].number_input("新增 Bot 数量", min_value=1, max_value=30, value=1, step=1, disabled=not setup_editable)
        add_bots = bot_cols[1].form_submit_button("添加普通 Bot", type="primary", disabled=not setup_editable, use_container_width=True)
        super_bot_count = bot_cols[2].number_input("新增超级 Bot 数量", min_value=1, max_value=10, value=1, step=1, disabled=not setup_editable)
        add_super_bots = bot_cols[3].form_submit_button("添加超级 Bot", disabled=not setup_editable, use_container_width=True)
        st.caption("普通 Bot 开轮立即提交；超级 Bot 等其他队伍全部提交后读取本轮决策，再分析并提交。")
    if add_bots or add_super_bots:
        if not markets:
            st.error("请先创建至少一个可选主场。")
        else:
            with connect() as conn:
                is_super = bool(add_super_bots)
                existing = int(one(conn, "SELECT COUNT(*) AS n FROM companies WHERE is_bot=1 AND is_super_bot=?", (int(is_super),))["n"])
                initial_cash = get_setting(conn, "initial_cash", 15_000_000.0)
                requested_count = int(super_bot_count if is_super else bot_count)
                prefix = "SBOT" if is_super else "BOT"
                for offset in range(requested_count):
                    number_index = existing + offset + 1
                    code_value = f"{prefix}{number_index:02d}"
                    while one(conn, "SELECT 1 FROM companies WHERE code=?", (code_value,)):
                        number_index += 1
                        code_value = f"{prefix}{number_index:02d}"
                    home = str(markets[(number_index - 1) % len(markets)]["city"])
                    cursor = conn.execute(
                        "INSERT INTO companies(code,name,password_hash,home_city,cash,setup_submitted_at,is_bot,is_super_bot,bot_profile,created_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
                        (code_value, f"{'Super ' if is_super else ''}Auto Company {number_index}", hash_password(os.urandom(16).hex()), home, initial_cash, now_iso(), int(is_super), number_index, now_iso()),
                    )
                    conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,1)", (cursor.lastrowid, home))
            flash("success", f"已添加 {requested_count} 支{'超级' if is_super else '普通'} Bot 队伍。")
            st.rerun()
    if not setup_editable:
        st.info("比赛已开始：为避免影响结算，赛前资料已锁定。密码仍可重置。")

    st.markdown("#### 批量删除玩家与 Bot")
    company_labels = {
        int(company["id"]): (
            f"{company['code']} · {company['name']} · "
            f"{'超级 Bot' if company['is_super_bot'] else ('普通 Bot' if company['is_bot'] else '玩家')}"
        )
        for company in companies
    }
    with st.form("bulk_delete_companies"):
        selected_company_ids = st.multiselect(
            "选择要删除的队伍",
            options=list(company_labels),
            format_func=lambda company_id: company_labels[company_id],
            placeholder="可一次选择多个玩家、普通 Bot 或超级 Bot",
        )
        confirm_bulk_delete = st.checkbox("我确认永久删除所选队伍及其全部比赛数据")
        bulk_delete = st.form_submit_button(
            f"批量删除所选队伍",
            use_container_width=True,
        )
    if bulk_delete:
        if not selected_company_ids:
            st.error("请至少选择一个要删除的玩家或 Bot。")
        elif not confirm_bulk_delete:
            st.error("请先勾选删除确认。")
        else:
            try:
                with connect() as conn:
                    deleted_count = delete_companies(conn, [int(company_id) for company_id in selected_company_ids])
                flash("success", f"已批量删除 {deleted_count} 支队伍。")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

    for company in companies:
        bot_tag = (" · 超级 BOT" if bool(company["is_super_bot"]) else " · BOT") if bool(company["is_bot"]) else ""
        with st.expander(f"{company['code']} · {company['name']} · {company['home_city'] or '未选主场'}{bot_tag}"):
            st.markdown("##### 代管赛前资料")
            with st.form(f"admin_company_setup_{company['id']}"):
                setup_cols = st.columns([2, 2, 1])
                managed_name = setup_cols[0].text_input(
                    "公司名称",
                    value=str(company["name"]),
                    max_chars=40,
                    disabled=not setup_editable,
                )
                current_home = str(company["home_city"] or "未选择")
                home_index = home_options.index(current_home) if current_home in home_options else 0
                managed_home = setup_cols[1].selectbox(
                    "主场城市",
                    home_options,
                    index=home_index,
                    disabled=not setup_editable,
                )
                managed_ready = setup_cols[2].checkbox(
                    "赛前就绪",
                    value=bool(company["setup_submitted_at"]),
                    disabled=not setup_editable,
                    help="勾选后，管理员可代替选手完成赛前确认。",
                )
                save_setup = st.form_submit_button(
                    "保存赛前资料",
                    type="primary",
                    disabled=not setup_editable,
                    use_container_width=True,
                )
            if save_setup:
                clean_name = managed_name.strip()
                selected_home = None if managed_home == "未选择" else managed_home
                if len(clean_name) < 2:
                    st.error("公司名称至少需要 2 个字符。")
                elif managed_ready and (not selected_home or clean_name.startswith("待命名-")):
                    st.error("标记就绪前，请填写正式公司名称并选择主场。")
                else:
                    try:
                        with connect() as conn:
                            duplicate = one(
                                conn,
                                "SELECT id FROM companies WHERE lower(name)=lower(?) AND id<>?",
                                (clean_name, company["id"]),
                            )
                            if duplicate:
                                raise ValueError("公司名称已被其他队伍使用。")
                            setup_at = now_iso() if managed_ready and selected_home else None
                            conn.execute(
                                "UPDATE companies SET name=?,home_city=?,setup_submitted_at=? WHERE id=?",
                                (clean_name, selected_home, setup_at, company["id"]),
                            )
                            if selected_home != company["home_city"]:
                                conn.execute("DELETE FROM agents WHERE company_id=?", (company["id"],))
                            if selected_home:
                                conn.execute(
                                    "INSERT INTO agents(company_id,city,count) VALUES(?,?,1) "
                                    "ON CONFLICT(company_id,city) DO UPDATE SET count=MAX(count,1)",
                                    (company["id"], selected_home),
                                )
                        flash("success", f"{company['code']} 的赛前资料已由管理员更新。")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

            if bool(company["is_bot"]):
                with st.form(f"bot_type_{company['id']}"):
                    current_type = "超级 Bot" if bool(company["is_super_bot"]) else "普通 Bot"
                    bot_type = st.radio(
                        "Bot 类型", ["普通 Bot", "超级 Bot"],
                        index=1 if bool(company["is_super_bot"]) else 0,
                        horizontal=True, disabled=not setup_editable,
                    )
                    save_bot_type = st.form_submit_button("保存 Bot 类型", disabled=not setup_editable)
                if save_bot_type:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE companies SET is_super_bot=? WHERE id=?",
                            (int(bot_type == "超级 Bot"), company["id"]),
                        )
                    flash("success", f"{company['code']} 已切换为{bot_type}。")
                    st.rerun()

            st.markdown("##### 登录权限")
            with st.form(f"admin_company_password_{company['id']}"):
                password_cols = st.columns([3, 1])
                new_password = password_cols[0].text_input("新密码", type="password")
                reset_password = password_cols[1].form_submit_button("重置密码", use_container_width=True)
            if reset_password:
                if not new_password:
                    st.error("请先输入新密码。")
                else:
                    with connect() as conn:
                        conn.execute("UPDATE companies SET password_hash=? WHERE id=?", (hash_password(new_password), company["id"]))
                    flash("success", f"{company['code']} 密码已重置。")
                    st.rerun()
            st.caption(f"现金 {money(company['cash'])} · 负债 {money(company['debt'])} · 专利 {company['patents']} · 库存 {company['product_inventory']}")
            st.markdown("##### 删除玩家")
            delete_phrase = f"DELETE {company['code']}"
            delete_confirm = st.text_input(
                f"输入 {delete_phrase} 确认删除",
                key=f"delete_company_confirm_{company['id']}",
            )
            if st.button(
                "永久删除该玩家及全部比赛数据",
                key=f"delete_company_{company['id']}",
                disabled=delete_confirm != delete_phrase,
            ):
                try:
                    with connect() as conn:
                        delete_company(conn, int(company["id"]))
                    flash("success", f"玩家 {company['code']} 已删除。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))


def render_admin_decisions() -> None:
    hero("决策管理", "查看玩家本轮提交内容；管理员可在结算前直接修正并代为提交。")
    with connect() as conn:
        round_row = current_round(conn)
        companies = all_rows(conn, "SELECT * FROM companies ORDER BY code")
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
        settings = settings_dict(conn)
    if not round_row or not companies:
        st.info("暂无可管理的回合或队伍。")
        return
    round_no = int(round_row["round_no"])
    editable = round_row["status"] in ("waiting", "open", "paused")
    labels = [f"{row['code']} · {row['name']}" for row in companies]
    selected_label = st.selectbox("选择队伍", labels)
    company = companies[labels.index(selected_label)]
    if not company["home_city"]:
        st.warning("该队伍尚未选择主场，需先在“队伍管理”中完成设置。")
        return
    with connect() as conn:
        decision_row = one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        city_rows = {
            str(row["city"]): dict(row)
            for row in all_rows(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        }
        current_workers = employee_count(conn, company["id"], "worker")
        current_engineers = employee_count(conn, company["id"], "engineer")
        home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        previous_worker_salary = previous_salary(conn, company, round_no, "worker_salary", float(home["worker_initial_salary"]))
        previous_engineer_salary = previous_salary(conn, company, round_no, "engineer_salary", float(home["engineer_initial_salary"]))
    decision = dict(decision_row) if decision_row else {
        "loan_change": 0.0, "worker_delta": 0, "worker_salary": previous_worker_salary,
        "engineer_delta": 0, "engineer_salary": previous_engineer_salary,
        "management_investment": 0.0, "production_volume": 0, "quality_investment": 0.0,
        "research_investment": 0.0, "submitted_at": None,
    }
    if not decision.get("submitted_at"):
        decision.update({"loan_change": 0.0, "worker_delta": 0, "engineer_delta": 0, "management_investment": 0.0, "production_volume": 0, "quality_investment": 0.0, "research_investment": 0.0})
        city_rows = {}
    status_text = "已提交" if decision.get("submitted_at") else "未提交"
    st.info(f"第 {round_no} 轮 · {STATUS_LABELS.get(round_row['status'], round_row['status'])} · 玩家状态：{status_text}")
    if not editable:
        st.warning("本轮已经结算，决策仅可查看，不能再修改。")

    mark_submitted = st.checkbox("本轮已提交", value=bool(decision.get("submitted_at")), disabled=not editable, help="未勾选时保存会把本轮所有新增、生产和投资决策归零。")
    worker_low, worker_high = salary_bounds(settings, previous_worker_salary)
    engineer_low, engineer_high = salary_bounds(settings, previous_engineer_salary)
    with connect() as conn:
        loan_base_net_assets = current_company_net_assets(conn, company)
    loan_ceiling = loan_ceiling_for_round(round_no, home, float(settings["global_max_loan"]))
    loan_limit = available_loan_limit(
        loan_base_net_assets, float(settings["loan_asset_threshold"]), float(home["min_loan"]), loan_ceiling
    )
    with st.form(f"admin_decision_{round_no}_{company['id']}"):
        with st.expander("💰 银行贷款", expanded=False):
            st.caption(
                f"计算净资产 {money(loan_base_net_assets)}；本轮最高新增 {money(loan_limit)}；"
                f"新增贷款最少 {money(home['min_loan'])}；本轮公式封顶基数 {money(loan_ceiling)}。"
            )
            loan_change = st.number_input("贷款变化", min_value=-float(company["debt"]), max_value=max(0.0, loan_limit), value=min(max(float(decision["loan_change"]), -float(company["debt"])), max(0.0, loan_limit)), step=10_000.0, disabled=not editable)
        with st.expander("👥 人力资源", expanded=True):
            cols = st.columns(2)
            worker_delta = cols[0].number_input("工人增减", min_value=-current_workers, value=int(decision["worker_delta"]), step=1, disabled=not editable)
            worker_salary = cols[1].number_input("工人月薪", min_value=worker_low, max_value=worker_high, value=min(max(float(decision["worker_salary"]), worker_low), worker_high), step=50.0, disabled=not editable)
            cols = st.columns(2)
            engineer_delta = cols[0].number_input("工程师增减", min_value=-current_engineers, value=int(decision["engineer_delta"]), step=1, disabled=not editable)
            engineer_salary = cols[1].number_input("工程师月薪", min_value=engineer_low, max_value=engineer_high, value=min(max(float(decision["engineer_salary"]), engineer_low), engineer_high), step=50.0, disabled=not editable)
        with st.expander("🏭 生产与研发", expanded=True):
            cols = st.columns(2)
            production_volume = cols[0].number_input("计划生产量", min_value=0, value=int(decision["production_volume"]), step=1, disabled=not editable)
            management = cols[1].number_input("管理投入（MA）", min_value=0.0, value=float(decision["management_investment"]), step=10_000.0, disabled=not editable)
            cols = st.columns(2)
            quality = cols[0].number_input("品质投入（QI）", min_value=0.0, value=float(decision["quality_investment"]), step=10_000.0, disabled=not editable)
            research = cols[1].number_input("研发 / 专利投入", min_value=0.0, value=float(decision["research_investment"]), step=50_000.0, disabled=not editable)
        st.markdown("### 🏙️ 城市销售")
        city_inputs: dict[str, dict[str, Any]] = {}
        for market in markets:
            city = str(market["city"])
            saved = city_rows.get(city, {})
            with st.expander(city, expanded=city == company["home_city"]):
                with connect() as conn:
                    agent_row = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company["id"], city))
                current_agents = int(agent_row["count"]) if agent_row else 0
                cols = st.columns(4)
                city_inputs[city] = {
                    "agent_delta": cols[0].number_input("Agent 增减", min_value=-current_agents, max_value=int(settings["max_agent_add_per_city_round"]), value=min(int(saved.get("agent_delta", 0)), int(settings["max_agent_add_per_city_round"])), step=1, key=f"admin_agent_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "marketing_investment": cols[1].number_input("营销投入（MI）", min_value=0.0, value=float(saved.get("marketing_investment", 0.0)), step=10_000.0, key=f"admin_mi_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "price": cols[2].number_input("售价", min_value=float(settings["price_min"]), max_value=min(float(settings["price_max"]), float(market["max_price"])), value=float(saved.get("price", market["initial_avg_price"])), step=100.0, key=f"admin_price_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "order_report": cols[3].checkbox("购买市场报告", value=bool(saved.get("order_report", 0)), key=f"admin_report_{company['id']}_{round_no}_{city}", disabled=not editable),
                }
        save = st.form_submit_button("保存玩家决策", type="primary", disabled=not editable, use_container_width=True)
    if save:
        if mark_submitted and 0 < float(loan_change) < float(home["min_loan"]):
            st.error(f"新增贷款不得低于主场最低贷款 {money(home['min_loan'])}；不贷款请填写 0。")
            return
        submitted_at = now_iso() if mark_submitted else None
        if not mark_submitted:
            loan_change = worker_delta = engineer_delta = management = production_volume = quality = research = 0
            for values in city_inputs.values():
                values["agent_delta"] = 0
                values["marketing_investment"] = 0.0
                values["order_report"] = False
        with connect() as conn:
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id,round_no) DO UPDATE SET loan_change=excluded.loan_change,worker_delta=excluded.worker_delta,worker_salary=excluded.worker_salary,engineer_delta=excluded.engineer_delta,engineer_salary=excluded.engineer_salary,management_investment=excluded.management_investment,production_volume=excluded.production_volume,quality_investment=excluded.quality_investment,research_investment=excluded.research_investment,submitted_at=excluded.submitted_at",
                (company["id"], round_no, loan_change, worker_delta, worker_salary, engineer_delta, engineer_salary, management, production_volume, quality, research, submitted_at),
            )
            for city, values in city_inputs.items():
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(company_id,round_no,city) DO UPDATE SET agent_delta=excluded.agent_delta,marketing_investment=excluded.marketing_investment,price=excluded.price,order_report=excluded.order_report",
                    (company["id"], round_no, city, values["agent_delta"], values["marketing_investment"], values["price"], int(values["order_report"])),
                )
        flash("success", f"{company['code']} 第 {round_no} 轮决策已由管理员保存。")
        st.rerun()


def render_admin_kds() -> None:
    hero("KDS 设置", "修改将影响后续结算；已结算结果不会追溯变化。")
    with connect() as conn:
        settings = settings_dict(conn)
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
        started_row = one(conn, "SELECT COUNT(*) AS n FROM rounds WHERE status<>'waiting' OR starts_at IS NOT NULL")
    competition_started = bool(started_row and int(started_row["n"]) > 0)
    unlocked = not competition_started or bool(st.session_state.get("admin_kds_unlocked"))
    if competition_started and not unlocked:
        st.warning("比赛已经开始，KDS 已锁定。输入 UNLOCK KDS 后才可编辑，修改只影响尚未结算的回合。")
        unlock_text = st.text_input("解锁确认", placeholder="UNLOCK KDS", type="password")
        if st.button("解锁 KDS 编辑", disabled=unlock_text != "UNLOCK KDS"):
            st.session_state["admin_kds_unlocked"] = True
            st.rerun()
    elif competition_started:
        lock_col, note_col = st.columns([1, 4])
        if lock_col.button("重新锁定", use_container_width=True):
            st.session_state.pop("admin_kds_unlocked", None)
            st.rerun()
        note_col.info("KDS 当前已临时解锁；退出登录或点击“重新锁定”后恢复锁定。")
    with st.form("global_kds"):
        values: dict[str, Any] = {}
        items = [key for key in GLOBAL_SETTING_LABELS if key in settings]
        for start in range(0, len(items), 3):
            cols = st.columns(3)
            for offset, key in enumerate(items[start:start + 3]):
                default = settings[key]
                if isinstance(default, int):
                    values[key] = cols[offset].number_input(GLOBAL_SETTING_LABELS[key], value=int(default), step=1, key=f"setting_{key}", disabled=not unlocked)
                else:
                    values[key] = cols[offset].number_input(GLOBAL_SETTING_LABELS[key], value=float(default), step=0.01 if abs(float(default)) < 2 else 100.0, format="%.4f" if abs(float(default)) < 2 else "%.2f", key=f"setting_{key}", disabled=not unlocked)
        save = st.form_submit_button("保存全局 KDS", type="primary", disabled=not unlocked)
    if save:
        if values["salary_min"] > values["salary_max"] or values["price_min"] > values["price_max"]:
            st.error("最低值不能高于最高值。")
        elif int(values["total_rounds"]) < 1:
            st.error("比赛总轮数必须至少为 1。")
        elif float(values["loan_asset_threshold"]) <= 0:
            st.error("贷款净资产阈值必须大于 0，贷款公式才能生效。")
        elif float(values["global_max_loan"]) < 0:
            st.error("第二轮起全局最高贷款不能为负数。")
        elif any(float(values[key]) < 0 for key in ("transport_cost", "worker_training_cost", "engineer_training_cost")):
            st.error("运输费和培训费不能为负数。")
        else:
            with connect() as conn:
                for key, value in values.items():
                    set_setting(conn, key, value)
            flash("success", "全局 KDS 已保存。")
            st.rerun()

    st.subheader("城市参数")
    frame = pd.DataFrame([dict(row) for row in markets])
    ordered_columns = [column for column in MARKET_COLUMNS if column in frame.columns]
    frame = frame[ordered_columns]
    edited = st.data_editor(
        frame,
        hide_index=True,
        use_container_width=True,
        disabled=list(frame.columns) if not unlocked else ["city"],
        column_config={
            key: (st.column_config.CheckboxColumn(label) if key == "home_enabled" else st.column_config.Column(label))
            for key, label in MARKET_COLUMNS.items()
        },
        key="market_editor",
    )
    if st.button("保存全部城市参数", type="primary", disabled=not unlocked):
        numeric_columns = [column for column in frame.columns if column not in ("city", "home_enabled")]
        try:
            records = edited.to_dict("records")
            for record in records:
                minimum_loan = float(record["min_loan"])
                maximum_loan = float(record["max_loan"])
                if minimum_loan < 0 or maximum_loan < 0 or minimum_loan > maximum_loan:
                    raise ValueError(f"{record['city']} 的最低贷款必须大于等于 0，且不能高于最高贷款。")
            with connect() as conn:
                for record in records:
                    values_sql = [int(bool(record["home_enabled"]))] + [float(record[column]) for column in numeric_columns]
                    assignments = ["home_enabled=?"] + [f"{column}=?" for column in numeric_columns]
                    conn.execute(f"UPDATE market_config SET {','.join(assignments)} WHERE city=?", (*values_sql, record["city"]))
            flash("success", "城市 KDS 已保存。")
            st.rerun()
        except (TypeError, ValueError) as exc:
            st.error(str(exc) or "城市参数必须是有效数字。")

    with st.form("add_city", clear_on_submit=True):
        city = st.text_input("新增城市名称", disabled=not unlocked)
        add_city = st.form_submit_button("新增城市", disabled=not unlocked)
    if add_city and city.strip():
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO market_config(city,home_enabled,max_loan,min_loan,interest_rate,worker_initial_salary,engineer_initial_salary,"
                    "component_material,product_material,component_storage,product_storage,population,penetration,initial_avg_price,max_price,"
                    "transport_cost,worker_training_cost,engineer_training_cost) VALUES(?,1,0,0,0,0,0,0,0,0,0,1,0.01,0,?,0,0,0)",
                    (city.strip(), get_setting(conn, "price_max", 25_000.0)),
                )
            flash("success", f"已新增城市 {city.strip()}，请补充参数。")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("城市名称重复。")

    st.markdown("#### 删除城市")
    st.caption("删除城市会同步移除该城市的 Agent、决策和市场数据；以该城市为主场的玩家需要重新选择主场。")
    city_names = [str(row["city"]) for row in markets]
    selected_city_for_delete = st.selectbox("选择要删除的城市", city_names, disabled=not unlocked or len(city_names) <= 1)
    city_delete_phrase = f"DELETE {selected_city_for_delete}" if selected_city_for_delete else ""
    city_delete_confirm = st.text_input(
        f"输入 {city_delete_phrase or 'DELETE 城市名'} 确认",
        key="delete_city_confirm",
        disabled=not unlocked or len(city_names) <= 1,
    )
    if st.button(
        "永久删除该城市",
        disabled=not unlocked or len(city_names) <= 1 or city_delete_confirm != city_delete_phrase,
    ):
        try:
            with connect() as conn:
                delete_city(conn, selected_city_for_delete)
            flash("success", f"城市 {selected_city_for_delete} 已删除。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def render_admin_rounds() -> None:
    hero("回合控制", "设置比赛总轮数、控制计时与结算；必要时可随时中断并从第一轮重开。")
    with connect() as conn:
        round_row = current_round(conn)
        total_rounds = max(1, get_setting(conn, "total_rounds", 5, int))
        default_minutes = max(1, get_setting(conn, "round_duration_minutes", 30, int))
        default_test_round = bool(get_setting(conn, "test_round_enabled", 0, int))
        companies_for_bonus = all_rows(conn, "SELECT id,code,name,is_bot,is_super_bot FROM companies ORDER BY id")
        setup = setup_status(conn)
        submission = submission_status(conn, int(round_row["round_no"])) if round_row and round_row["status"] in ("open", "paused") else None
        decisions = all_rows(
            conn,
            "SELECT c.code,c.name,c.home_city,c.setup_submitted_at,c.is_bot,c.is_super_bot,d.submitted_at,d.production_volume,d.management_investment,d.quality_investment,d.research_investment "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=? ORDER BY c.id",
            (int(round_row["round_no"]),),
        ) if round_row else []
        regular_status = one(
            conn,
            "SELECT COUNT(*) AS total,SUM(CASE WHEN d.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=? WHERE c.is_super_bot=0",
            (int(round_row["round_no"]),),
        ) if round_row else None
        super_status = one(
            conn,
            "SELECT COUNT(*) AS total,SUM(CASE WHEN d.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=? WHERE c.is_super_bot=1",
            (int(round_row["round_no"]),),
        ) if round_row else None
        history = all_rows(conn, "SELECT * FROM rounds ORDER BY round_no DESC")
    round_banner(round_row)
    current_round_no = int(round_row["round_no"]) if round_row else 1
    settings_cols = st.columns([2, 1, 2])
    selected_total_rounds = settings_cols[0].number_input(
        "比赛总轮数",
        min_value=max(1, current_round_no),
        value=max(total_rounds, current_round_no),
        step=1,
    )
    progress_text = "测试轮" if current_round_no < 0 else f"{current_round_no}/{total_rounds}"
    settings_cols[1].metric("比赛进度", progress_text)
    if settings_cols[2].button("保存总轮数", use_container_width=True):
        with connect() as conn:
            set_setting(conn, "total_rounds", int(selected_total_rounds))
        flash("success", f"比赛总轮数已设置为 {int(selected_total_rounds)} 轮。")
        st.rerun()
    st.write(f"赛前就绪：{setup['ready']}/{setup['total']}")
    if submission:
        st.write(f"本轮提交：{submission['submitted']}/{submission['total']}")

    if round_row and round_row["status"] == "waiting":
        use_test_round = st.checkbox(
            "开始正式比赛前先进行 -1 测试轮",
            value=default_test_round,
            help="测试轮会正常结算并生成报表；结束后系统恢复赛前现金、员工、库存、专利和 Agent，再开启正式第一轮。",
        )
        duration_label = "测试轮时长（分钟）" if use_test_round else "第一轮时长（分钟）"
        minutes = st.number_input(duration_label, min_value=1, value=default_minutes, step=1)
        start_label = "开始 -1 测试轮" if use_test_round else "开始第一轮"
        if st.button(start_label, type="primary", disabled=not bool(setup["all_ready"])):
            with connect() as conn:
                started_round = start_competition(conn, int(minutes), use_test_round)
                submit_bot_decisions(conn, started_round)
            flash("success", "测试轮已开始。" if started_round < 0 else "第一轮已开始。")
            st.rerun()
    elif round_row and round_row["status"] in ("open", "paused"):
        regular_total = int(regular_status["total"] or 0) if regular_status else 0
        regular_submitted = int(regular_status["submitted"] or 0) if regular_status else 0
        super_total = int(super_status["total"] or 0) if super_status else 0
        super_submitted = int(super_status["submitted"] or 0) if super_status else 0
        if super_total:
            st.markdown("#### 超级 Bot 决策")
            st.caption(
                f"真人玩家与普通 Bot：{regular_submitted}/{regular_total} · 超级 Bot：{super_submitted}/{super_total}。"
                "超级 Bot 只会在其他队伍全部提交后读取本轮决策。"
            )
            super_action_label = (
                "重新分析并逐个覆盖超级 Bot 决策"
                if super_submitted >= super_total
                else "继续分析未完成的超级 Bot"
                if super_submitted > 0
                else "超级 Bot 分析并提交"
            )
            if st.button(
                super_action_label,
                type="primary",
                disabled=regular_submitted < regular_total,
                use_container_width=True,
            ):
                try:
                    super_progress = st.progress(0.0, text="超级 Bot 正在模拟 CPI 候选方案…")
                    def update_super_progress(done: int, total: int, code: str) -> None:
                        stage = "联合复算完成" if code == "联合复算" else f"{code} 已分析并保存"
                        super_progress.progress(
                            min(1.0, done / max(1, total)),
                            text=f"{stage} · {done}/{total}",
                        )
                    remote_round = int(round_row["round_no"])
                    if _remote_super_bot_submit(
                        remote_round,
                        replace_existing=super_submitted >= super_total,
                    ):
                        update_super_progress(super_total, super_total, "远程计算完成")
                    else:
                        with connect() as conn:
                            submit_super_bot_decisions(
                                conn,
                                remote_round,
                                update_super_progress,
                                replace_existing=super_submitted >= super_total,
                            )
                    super_progress.empty()
                    flash("success", f"{super_total} 支超级 Bot 已读取全部对手决策并完成提交。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except sqlite3.OperationalError:
                    LOGGER.exception("Super Bot submission database operation failed")
                    st.error("数据库当前正忙，系统没有结算本轮。请稍等几秒后再次点击超级 Bot 分析。")
        cols = st.columns(4)
        if round_row["status"] == "open":
            if cols[0].button("暂停", use_container_width=True):
                with connect() as conn:
                    conn.execute("UPDATE rounds SET status='paused' WHERE round_no=?", (round_row["round_no"],))
                st.rerun()
        else:
            if cols[0].button("继续", use_container_width=True):
                with connect() as conn:
                    conn.execute("UPDATE rounds SET status='open' WHERE round_no=?", (round_row["round_no"],))
                st.rerun()
        extend_minutes = cols[1].number_input("延长分钟", min_value=1, value=5, step=1, label_visibility="collapsed")
        if cols[2].button("延长", use_container_width=True):
            old_end = parse_time(round_row["ends_at"]) or datetime.now(timezone.utc)
            with connect() as conn:
                conn.execute("UPDATE rounds SET ends_at=? WHERE round_no=?", ((old_end + timedelta(minutes=int(extend_minutes))).isoformat(), round_row["round_no"]))
            flash("success", f"已延长 {extend_minutes} 分钟。")
            st.rerun()
        non_super_ready = regular_submitted >= regular_total
        settlement_label = "超级 Bot 分析并结算" if super_total and super_submitted < super_total else "结算本轮"
        if cols[3].button(settlement_label, type="primary", use_container_width=True, disabled=not non_super_ready):
            try:
                remote_completed = False
                remote_round = int(round_row["round_no"])
                if super_total and super_submitted < super_total:
                    settlement_progress = st.progress(0.0, text="超级 Bot 正在模拟 CPI 候选方案…")
                    remote_completed = _remote_super_bot_submit(remote_round)
                    if remote_completed:
                        settlement_progress.progress(1.0, text="远程计算完成")
                with connect() as conn:
                    if super_total and super_submitted < super_total and not remote_completed:
                        def update_settlement_progress(done: int, total: int, code: str) -> None:
                            stage = "联合复算完成" if code == "联合复算" else f"{code} 已分析并保存"
                            settlement_progress.progress(
                                min(1.0, done / max(1, total)),
                                text=f"{stage} · {done}/{total}",
                            )
                        submit_super_bot_decisions(conn, remote_round, update_settlement_progress)
                    settle_round(conn, int(round_row["round_no"]))
                if super_total and super_submitted < super_total:
                    settlement_progress.empty()
                completed_label = "测试轮" if int(round_row["round_no"]) < 0 else f"第 {round_row['round_no']} 轮"
                flash("success", f"{completed_label}结算完成。")
                st.rerun()
            except Exception:
                LOGGER.exception("Round settlement failed")
                st.error("结算失败，请在部署后台日志中查看详细原因。")
    elif round_row and round_row["status"] == "settled":
        if int(round_row["round_no"]) < 0:
            st.success("-1 测试轮已经结束。测试报表会保留，但测试轮造成的现金、贷款、库存、专利、员工和 Agent 变化不会带入正式比赛。")
            minutes = st.number_input("正式第一轮时长（分钟）", min_value=1, value=default_minutes, step=1)
            if st.button("恢复赛前状态并开始第一轮", type="primary"):
                try:
                    with connect() as conn:
                        prepare_first_round_after_test(conn, int(minutes))
                        submit_bot_decisions(conn, 1)
                    flash("success", "赛前状态已恢复，正式第一轮已开始。")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        elif int(round_row["round_no"]) >= total_rounds:
            st.success(f"全部 {total_rounds} 轮已经结束。")
        else:
            next_round = int(round_row["round_no"]) + 1
            st.markdown("#### 下一轮 Bonus")
            st.caption("填写后点击开启下一轮，Bonus 会先加入对应队伍现金；留空或填 0 即不发放。")
            with st.form(f"next_round_{next_round}"):
                minutes = st.number_input("下一轮时长（分钟）", min_value=1, value=default_minutes, step=1)
                bonus_values: dict[int, float] = {}
                for start_index in range(0, len(companies_for_bonus), 3):
                    bonus_cols = st.columns(3)
                    for offset, team in enumerate(companies_for_bonus[start_index:start_index + 3]):
                        bonus_values[int(team["id"])] = bonus_cols[offset].number_input(
                            f"{team['code']} · {team['name']}", min_value=0.0, value=0.0, step=10_000.0,
                            key=f"bonus_{next_round}_{team['id']}",
                        )
                start_next = st.form_submit_button(f"发放 Bonus 并开启第 {next_round} 轮", type="primary", use_container_width=True)
            if start_next:
                start = datetime.now(timezone.utc)
                with connect() as conn:
                    for company_id, amount in bonus_values.items():
                        clean_amount = max(0.0, float(amount))
                        if clean_amount <= 0:
                            continue
                        conn.execute("UPDATE companies SET cash=cash+? WHERE id=?", (clean_amount, company_id))
                        conn.execute(
                            "INSERT INTO round_bonuses(company_id,round_no,amount,created_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(company_id,round_no) DO UPDATE SET amount=excluded.amount,created_at=excluded.created_at",
                            (company_id, next_round, clean_amount, now_iso()),
                        )
                    conn.execute("INSERT INTO rounds(round_no,status,starts_at,ends_at) VALUES(?,'open',?,?)", (next_round, start.isoformat(), (start + timedelta(minutes=int(minutes))).isoformat()))
                    submit_bot_decisions(conn, next_round)
                flash("success", f"第 {next_round} 轮已开始。")
                st.rerun()

    if decisions:
        st.subheader("队伍状态")
        status_frame = pd.DataFrame(
            [{"队伍": row["code"], "公司": row["name"], "类型": "超级 Bot" if row["is_super_bot"] else ("普通 Bot" if row["is_bot"] else "玩家"), "主场": row["home_city"] or "—", "赛前就绪": bool(row["setup_submitted_at"]), "本轮提交": bool(row["submitted_at"]), "计划产量": (row["production_volume"] or 0) if row["submitted_at"] else 0, "MA": (row["management_investment"] or 0) if row["submitted_at"] else 0, "QI": (row["quality_investment"] or 0) if row["submitted_at"] else 0, "专利": (row["research_investment"] or 0) if row["submitted_at"] else 0} for row in decisions]
        )
        st.dataframe(status_frame, hide_index=True, use_container_width=True)
    st.subheader("回合历史")
    st.dataframe(pd.DataFrame([dict(row) for row in history]), hide_index=True, use_container_width=True)
    st.divider()
    settled_round_numbers = [int(row["round_no"]) for row in history if row["status"] == "settled"]
    st.subheader("回退上一轮")
    if settled_round_numbers:
        rollback_target = max(settled_round_numbers)
        st.markdown(
            f'<div class="danger">将撤销第 {rollback_target} 轮结算，恢复该轮结算前的现金、负债、库存、专利、员工和 Agent；'
            '更晚回合会被删除；真人玩家的原决策保留为草稿，普通 Bot 会立即重新决策并提交。</div>',
            unsafe_allow_html=True,
        )
        rollback_cols = st.columns([1, 2])
        rollback_minutes = rollback_cols[0].number_input(
            "重新开放时长（分钟）", min_value=1, value=default_minutes, step=1, key="rollback_duration"
        )
        rollback_confirm = rollback_cols[1].text_input(
            "输入 ROLLBACK 确认回退", key="round_rollback_confirm"
        )
        if st.button(
            f"撤销第 {rollback_target} 轮并重新开放",
            disabled=rollback_confirm != "ROLLBACK",
            key="round_rollback_button",
            use_container_width=True,
        ):
            try:
                with connect() as conn:
                    reopened_round = rollback_latest_settled_round(conn, int(rollback_minutes))
                    normal_bot_count = submit_bot_decisions(conn, reopened_round)
                    missing_non_super = one(
                        conn,
                        "SELECT COUNT(*) AS n FROM companies c WHERE c.is_super_bot=0 AND NOT EXISTS "
                        "(SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=? "
                        "AND d.submitted_at IS NOT NULL)",
                        (reopened_round,),
                    )
                    super_bot_count = 0
                    if not missing_non_super or int(missing_non_super["n"] or 0) == 0:
                        super_bot_count = submit_super_bot_decisions(conn, reopened_round)
                bot_message = f"普通 Bot 已重新提交 {normal_bot_count} 支"
                if super_bot_count:
                    bot_message += f"，超级 Bot 已重新提交 {super_bot_count} 支"
                flash(
                    "success",
                    f"已撤销第 {reopened_round} 轮结算并重新开放；{bot_message}。真人玩家可修改草稿后重新提交。",
                )
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    else:
        st.caption("暂无已结算回合可回退。")
    st.divider()
    st.subheader("中断并重开比赛")
    st.markdown('<div class="danger">重开会清空所有回合、提交、结算报表、员工和 Agent，并让所有玩家重新选择主场与公司名称；玩家账号和 KDS 保留。</div>', unsafe_allow_html=True)
    restart_confirm = st.text_input("输入 RESTART 确认从第一轮重开", key="round_restart_confirm")
    if st.button("中断当前比赛并重开", disabled=restart_confirm != "RESTART", key="round_restart_button"):
        with connect() as conn:
            reset_competition(conn)
        st.session_state.pop("admin_kds_unlocked", None)
        flash("success", "比赛已中断并重开，现在回到第一轮赛前设置。")
        st.rerun()


def render_backup_reset() -> None:
    hero("备份与重置", "SQLite 数据在部分云平台重启后可能丢失；建议每轮结算后下载备份。")
    backup = database_bytes()
    st.download_button("下载完整数据库备份", backup, file_name=f"business_sim_{datetime.now().strftime('%Y%m%d_%H%M')}.db", mime="application/x-sqlite3", disabled=not bool(backup))
    st.subheader("恢复备份")
    uploaded = st.file_uploader("上传本系统导出的 .db 文件", type=["db", "sqlite", "sqlite3"])
    restore_confirm = st.text_input("输入 RESTORE 确认覆盖当前数据")
    if st.button("恢复数据库", disabled=uploaded is None or restore_confirm != "RESTORE"):
        try:
            restore_database_bytes(uploaded.getvalue())
            st.session_state.clear()
            st.success("恢复完成，请重新登录。")
            st.rerun()
        except Exception:
            LOGGER.exception("Database restore failed")
            st.error("恢复失败，请确认备份文件来自本系统；详细原因仅记录在后台日志。")
    st.divider()
    st.subheader("重置整场比赛")
    st.markdown('<div class="danger">这会清除全部回合、决策、报表、员工和 Agent 数据，但保留队伍账号与 KDS。</div>', unsafe_allow_html=True)
    confirm = st.text_input("输入 RESET 确认")
    if st.button("重置比赛", type="primary", disabled=confirm != "RESET"):
        with connect() as conn:
            reset_competition(conn)
        flash("success", "比赛已重置。")
        st.rerun()


def main() -> None:
    show_flash()
    auth = st.session_state.get("auth")
    if not auth:
        render_login()
        return
    if auth.get("role") == "admin":
        page = sidebar("admin")
        {
            "总览": render_admin_overview,
            "队伍管理": render_admin_companies,
            "决策管理": render_admin_decisions,
            "KDS 设置": render_admin_kds,
            "回合控制": render_admin_rounds,
            "赛后报表": lambda: render_reports(None, admin=True),
            "财富曲线": lambda: render_wealth(None, admin=True),
            "备份与重置": render_backup_reset,
        }[page]()
        return

    company_id = int(auth.get("company_id", 0))
    with connect() as conn:
        company = one(conn, "SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        st.session_state.clear()
        st.rerun()
    if not company["home_city"] or not company["setup_submitted_at"]:
        player_setup_header(company)
        render_setup(company)
        return
    page = player_navigation(company)
    {
        "概览": lambda: render_player_overview(company),
        "决策": lambda: render_player_decision(company),
        "排名": render_ranking,
        "报表": lambda: render_reports(company),
        "规则": lambda: render_player_kds(company),
    }[page]()


if __name__ == "__main__":
    main()
