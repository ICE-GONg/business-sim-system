from __future__ import annotations

import csv
import html
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import altair as alt
import streamlit as st

from sim import APP_NAME
from sim.db import (
    all_rows,
    connect,
    current_round,
    database_bytes,
    employee_count,
    get_setting,
    hash_password,
    now_iso,
    one,
    reset_competition,
    restore_database_bytes,
    set_setting,
    settings_dict,
    setup_status,
    submission_status,
    verify_password,
)
from sim.defaults import GLOBAL_SETTING_LABELS, MARKET_COLUMNS
from sim.engine import market_size, settle_round, weighted_market_average


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
    @media (max-width: 720px) {
      .block-container { padding-left: .75rem; padding-right: .75rem; }
      .block-container h2 { font-size:1.65rem; }
      .hero { padding:14px; }
      .hero h1 { font-size:1.3rem; }
      .round-strip .value { font-size:1.1rem; }
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


def secret_value(name: str, fallback: str) -> str:
    if os.environ.get(name):
        return str(os.environ[name])
    try:
        return str(st.secrets.get(name, fallback))
    except Exception:
        return fallback


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
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results")
        round_no = int(latest["n"] or 0) if latest else 0
    if not round_no:
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
                expected_user = secret_value("SIM_ADMIN_USER", "admin")
                expected_password = secret_value("SIM_ADMIN_PASSWORD", "admin123")
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
        labels = [f"{m['city']}｜最高贷款 {money(m['max_loan'])}｜材料 {money(m['component_material'])}/{money(m['product_material'])}" for m in markets]
        with st.form("home_setup"):
            selected = st.selectbox("主场城市（确认后锁定）", labels)
            confirmed = st.form_submit_button("确认主场", type="primary")
        if confirmed:
            city = markets[labels.index(selected)]["city"]
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
    cols[0].metric("当前轮次", f"第 {round_row['round_no']} 轮")
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
        round_value = f"第 {round_row['round_no']} 轮"
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
    hero(f"你好，{company['name']}", "以 Net Cash（现金减负债）为核心，平衡生产、价格、投资和库存风险。")
    with connect() as conn:
        round_row = current_round(conn)
        latest = one(conn, "SELECT * FROM results WHERE company_id=? ORDER BY round_no DESC LIMIT 1", (company["id"],))
        ranking = rank_rows(conn, int(latest["round_no"]) if latest else 0)
        my_rank = next((row["rank"] for row in ranking if row["id"] == company["id"]), None)
        workers = employee_count(conn, company["id"], "worker")
        engineers = employee_count(conn, company["id"], "engineer")
        ready = setup_status(conn)
        wealth_rows = all_rows(
            conn,
            "SELECT round_no,net_assets FROM results WHERE company_id=? ORDER BY round_no",
            (company["id"],),
        )
    cols = st.columns(3)
    cols[0].metric("现金", money(company["cash"]))
    cols[1].metric("负债", money(company["debt"]))
    cols[2].metric("库存", number(company["product_inventory"]))
    secondary_cols = st.columns(2)
    secondary_cols[0].metric("员工", f"{workers + engineers:,}")
    secondary_cols[1].metric("最新排名", f"#{my_rank}" if my_rank else "—")
    if round_row and round_row["status"] == "waiting":
        st.info(f"已有 {ready['ready']}/{ready['total']} 支队伍完成赛前设置。全部就绪后管理员才能开始第一轮。")
    if latest:
        st.subheader("上一轮摘要")
        summary = pd.DataFrame(
            [{
                "轮次": int(latest["round_no"]),
                "生产": int(latest["produced"]),
                "售出": int(latest["sold"]),
                "库存": int(latest["inventory"]),
                "净利润": money(latest["net_profit"]),
                "净现金": money(latest["net_assets"]),
            }]
        )
        st.dataframe(summary, hide_index=True, use_container_width=True)
    if wealth_rows:
        with st.expander("查看财富趋势", expanded=False):
            wealth_frame = pd.DataFrame([dict(row) for row in wealth_rows]).rename(
                columns={"round_no": "轮次", "net_assets": "净现金"}
            )
            st.line_chart(wealth_frame.set_index("轮次"), y="净现金", y_label="净现金")


def decision_helper(conn: sqlite3.Connection, company: sqlite3.Row, round_no: int) -> dict[str, Any]:
    previous = all_rows(
        conn,
        "SELECT worker_salary,engineer_salary FROM decisions WHERE round_no=? AND submitted_at IS NOT NULL",
        (round_no - 1,),
    ) if round_no > 1 else []
    home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
    if previous:
        avg_worker = sum(float(row["worker_salary"]) for row in previous) / len(previous)
        avg_engineer = sum(float(row["engineer_salary"]) for row in previous) / len(previous)
    else:
        avg_worker = float(home["worker_initial_salary"])
        avg_engineer = float(home["engineer_initial_salary"])
    salary_max = get_setting(conn, "salary_max", 10_000.0)
    research = get_setting(conn, "research_75", 6_000_000.0) / 0.75 * 1.10 + get_setting(conn, "research_buffer", 150_000.0)
    return {
        "worker_wage": min(salary_max, (avg_worker + 100) * 1.10),
        "engineer_wage": min(salary_max, (avg_engineer + 100) * 1.10),
        "research": research,
    }


def render_player_decision(company: sqlite3.Row) -> None:
    hero("本轮决策", "决策可以在截止前重复提交；系统只保留最后一次提交。")
    with connect() as conn:
        round_row = current_round(conn)
        if not round_row:
            st.info("暂无回合。")
            return
        if round_row["status"] != "open":
            st.warning("当前回合未开放决策。")
            return
        end = parse_time(round_row["ends_at"])
        if end and datetime.now(timezone.utc) > end:
            st.error("本轮提交时间已结束，请等待管理员结算。")
            return
        round_no = int(round_row["round_no"])
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
        home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        current_workers = employee_count(conn, company["id"], "worker")
        current_engineers = employee_count(conn, company["id"], "engineer")
        decision_row = one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        decision = dict(decision_row) if decision_row else {
            "loan_change": 0.0,
            "worker_delta": 0,
            "worker_salary": float(home["worker_initial_salary"]),
            "engineer_delta": 0,
            "engineer_salary": float(home["engineer_initial_salary"]),
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

    st.markdown(
        f'<div class="hint">工资建议：工人约 <b>{money(helper["worker_wage"])}</b>，工程师约 '
        f'<b>{money(helper["engineer_wage"])}</b>；75% 专利参考投入约 <b>{money(helper["research"])}</b>。</div>',
        unsafe_allow_html=True,
    )
    if decision.get("submitted_at"):
        st.success(f"已提交；最后保存时间：{str(decision['submitted_at'])[:19].replace('T', ' ')} UTC")

    with st.form(f"decision_{round_no}"):
        loan_min = -float(company["debt"])
        loan_max = max(0.0, float(home["max_loan"]) - float(company["debt"]))
        with st.expander("💰 银行贷款", expanded=False):
            st.markdown('<div class="section-note">正数为新增贷款，负数为本轮还款。</div>', unsafe_allow_html=True)
            loan_change = st.number_input(
                "贷款变化",
                min_value=loan_min,
                max_value=loan_max,
                value=float(decision["loan_change"]),
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
                min_value=float(settings["salary_min"]),
                max_value=float(settings["salary_max"]),
                value=float(decision["worker_salary"]),
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
                min_value=float(settings["salary_min"]),
                max_value=float(settings["salary_max"]),
                value=float(decision["engineer_salary"]),
                step=50.0,
            )
            st.caption(
                f"当前：工人 {current_workers:,}、工程师 {current_engineers:,}。新员工按主场 KDS 收取培训费；第三轮起老员工享受经验倍率。"
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
                    max_value=int(settings["max_agent_add_per_round"]),
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
        total_agent_add = sum(max(0, row["agent_delta"]) for row in city_inputs.values())
        errors: list[str] = []
        if total_agent_add > int(settings["max_agent_add_per_round"]):
            errors.append(f"本轮最多新增 {int(settings['max_agent_add_per_round'])} 个 Agent，当前填写 {total_agent_add} 个。")
        for city, values in city_inputs.items():
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
        flash("success", "本轮决策已保存。")
        st.rerun()


def ranking_table(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "排名": row["rank"],
                "队伍": row["code"],
                "公司": row["name"],
                "主场": row["home_city"],
                "净现金": row["net_assets"],
                "现金": row["cash"],
                "本轮利润": row["net_profit"],
                "售出": row["sold"],
                "库存": row["inventory"],
            }
            for row in rows
        ]
    )


def render_ranking(admin: bool = False) -> None:
    hero("财富排行榜", "按 Net Cash 排序；Net Cash = 期末现金 − 负债，未售库存不计入排名。")
    with connect() as conn:
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results")
        latest_round = int(latest["n"] or 0) if latest else 0
        if not latest_round:
            st.info("暂无已结算回合。")
            return
        round_numbers = [int(row["round_no"]) for row in all_rows(conn, "SELECT DISTINCT round_no FROM results ORDER BY round_no DESC")]
        selected = st.selectbox("选择轮次", round_numbers, index=0)
        rows = rank_rows(conn, selected)
    frame = ranking_table(rows)
    st.dataframe(
        frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "净现金": st.column_config.NumberColumn(format="¥ %.0f"),
            "现金": st.column_config.NumberColumn(format="¥ %.0f"),
            "本轮利润": st.column_config.NumberColumn(format="¥ %.0f"),
        },
    )


def report_csv(report: dict[str, Any]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Section", "Item", "Value"])
    for section, value in report.items():
        if isinstance(value, dict):
            for key, item in value.items():
                writer.writerow([section, key, json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else item])
    return output.getvalue().encode("utf-8-sig")


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
    st.markdown('<div class="report-note">净利润 = 销售收入 − 全部成本；排名依据 Net Assets（现金 − 负债）。</div>', unsafe_allow_html=True)

    st.markdown('<div class="report-title">财务 Finance</div>', unsafe_allow_html=True)
    finance = report["finance"]
    start_debt = float(metrics["debt"]) - float(finance.get("loan_change", 0.0))
    cash_running = float(finance["round_begins"])
    debt_running = start_debt
    finance_items = [
        ("期初 / Round begins", 0.0, 0.0),
        ("银行贷款 / Bank loan", float(finance.get("loan_change", 0.0)), float(finance.get("loan_change", 0.0))),
        ("员工工资 / Salary cost", -float(finance.get("wages", 0.0)), 0.0),
        ("裁员费用 / Layoff", -float(finance.get("layoff", 0.0)), 0.0),
        ("培训费用 / Training", -float(finance.get("training", 0.0)), 0.0),
        ("材料成本 / Materials", -float(finance.get("materials", 0.0)), 0.0),
        ("仓储扩容 / Storage", -float(finance.get("storage", 0.0)), 0.0),
        ("Agent 变更", -float(finance.get("agents", 0.0)), 0.0),
        ("营销投入 / Marketing", -float(finance.get("marketing", 0.0)), 0.0),
        ("品质投入 / Quality", -float(finance.get("quality", 0.0)), 0.0),
        ("管理投入 / Management", -float(finance.get("management", 0.0)), 0.0),
        ("市场报告 / Market report", -float(finance.get("market_reports", 0.0)), 0.0),
        ("研发投入 / Research", -float(finance.get("research", 0.0)), 0.0),
        ("销售收入 / Sales revenue", float(metrics["sales_revenue"]), 0.0),
        ("贷款利息 / Debt interest", -float(finance.get("interest", 0.0)), 0.0),
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
        {"岗位": "工人 Workers", "期初": hr.get("previous_workers", max(0, int(hr["workers"]) - int(hr.get("worker_delta", 0)))), "增减": hr.get("worker_delta", 0), "当前": hr["workers"], "有效人数": hr["effective_workers"], "月薪": hr["worker_salary"], "工资倍率": hr["worker_wage_multiplier"]},
        {"岗位": "工程师 Engineers", "期初": hr.get("previous_engineers", max(0, int(hr["engineers"]) - int(hr.get("engineer_delta", 0)))), "增减": hr.get("engineer_delta", 0), "当前": hr["engineers"], "有效人数": hr["effective_engineers"], "月薪": hr["engineer_salary"], "工资倍率": hr["engineer_wage_multiplier"]},
    ])
    st.dataframe(hr_frame, hide_index=True, use_container_width=True, column_config={"月薪": st.column_config.NumberColumn(format="¥ %.0f"), "工资倍率": st.column_config.NumberColumn(format="%.2f")})
    st.markdown('<div class="report-note">低工资会降低有效人数；新员工收取培训费，裁员按一个月工资支付补偿。</div>', unsafe_allow_html=True)

    production = report["production"]
    st.markdown('<div class="report-title">管理与生产 Management / Production</div>', unsafe_allow_html=True)
    management_frame = pd.DataFrame([{"管理投入": finance.get("management", 0.0), "管理指数": production.get("ma_index", row["ma_index"]), "品质投入": finance.get("quality", 0.0), "品质指数": production.get("qi_index", row["qi_index"])}])
    st.dataframe(management_frame, hide_index=True, use_container_width=True, column_config={"管理投入": st.column_config.NumberColumn(format="¥ %.0f"), "品质投入": st.column_config.NumberColumn(format="¥ %.0f")})
    product_frame = pd.DataFrame([
        {"项目": "零件 Components", "计划": int(production.get("planned", 0)) * 7, "期初": 0, "本轮生产": production.get("components", int(production.get("produced", 0)) * 7), "总量": production.get("components", int(production.get("produced", 0)) * 7), "使用/售出": production.get("components", int(production.get("produced", 0)) * 7), "结余": 0},
        {"项目": "产品 Products", "计划": production.get("planned", 0), "期初": production.get("old_products", 0), "本轮生产": production.get("produced", 0), "总量": production.get("old_products", 0) + production.get("produced", 0), "使用/售出": production.get("sold", 0), "结余": production.get("surplus", 0)},
    ])
    st.dataframe(product_frame, hide_index=True, use_container_width=True)
    if "component_storage_before" in production:
        storage_frame = pd.DataFrame([
            {"仓储": "零件", "扩容前": production["component_storage_before"], "扩容后": production["component_storage_after"], "新增容量": production["component_storage_increase"]},
            {"仓储": "产品", "扩容前": production["product_storage_before"], "扩容后": production["product_storage_after"], "新增容量": production["product_storage_increase"]},
        ])
        st.dataframe(storage_frame, hide_index=True, use_container_width=True)

    research = report["research"]
    st.markdown('<div class="report-title">研发 Research Investment</div>', unsafe_allow_html=True)
    research_frame = pd.DataFrame([{"本轮投入": research["investment"], "成功概率": research["probability"] * 100, "本轮结果": "获得专利" if research["success"] else "未获得专利", "累计专利": research["patents_after"]}])
    st.dataframe(research_frame, hide_index=True, use_container_width=True, column_config={"本轮投入": st.column_config.NumberColumn(format="¥ %.0f"), "成功概率": st.column_config.NumberColumn(format="%.1f%%")})

    st.markdown('<div class="report-title">销售 Sales</div>', unsafe_allow_html=True)
    sales_frame = pd.DataFrame([{"市场": item["city"], "Agent": item["agents"], "竞争力 CPI%": item["cpi"], "销售量": item["sold"], "市场份额%": item["market_share"] * 100, "售价": item["price"], "销售收入": item["sold"] * item["price"], "营销投入": item["marketing"], "市场均价": item.get("market_average_price")} for item in report["sales"]])
    st.dataframe(sales_frame, hide_index=True, use_container_width=True, column_config={"市场份额%": st.column_config.NumberColumn(format="%.2f%%"), "售价": st.column_config.NumberColumn(format="¥ %.0f"), "销售收入": st.column_config.NumberColumn(format="¥ %.0f"), "营销投入": st.column_config.NumberColumn(format="¥ %.0f"), "市场均价": st.column_config.NumberColumn(format="¥ %.0f")})

    visible_cities = {str(item["city"]) for item in all_rows(conn, "SELECT city FROM market_config")} if admin else {str(item["city"]) for item in all_rows(conn, "SELECT city FROM city_decisions WHERE company_id=? AND round_no=? AND order_report=1", (company_id, round_no))}
    for city in sorted(visible_cities):
        market_rows = all_rows(conn, "SELECT c.code,c.name,cr.*,r.ma_index,r.qi_index,a.count AS agents FROM city_results cr JOIN companies c ON c.id=cr.company_id JOIN results r ON r.company_id=cr.company_id AND r.round_no=cr.round_no LEFT JOIN agents a ON a.company_id=cr.company_id AND a.city=cr.city WHERE cr.round_no=? AND cr.city=? ORDER BY cr.market_share DESC", (round_no, city))
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
        market_frame = pd.DataFrame([{"队伍": item["code"], "公司": item["name"], "管理指数": item["ma_index"], "Agent": item["agents"] or 0, "营销投入": item["marketing"], "品质指数": item["qi_index"], "售价": item["price"], "销售量": item["sold"], "市场份额%": item["market_share"] * 100} for item in market_rows])
        st.dataframe(market_frame, hide_index=True, use_container_width=True, column_config={"营销投入": st.column_config.NumberColumn(format="¥ %.0f"), "售价": st.column_config.NumberColumn(format="¥ %.0f"), "市场份额%": st.column_config.NumberColumn(format="%.2f%%")})
        st.caption("均价 = [Σ(玩家价格 × 对应售货量) + 基准均价 × (市场大小 − 玩家总售货量)] ÷ 市场大小")
    st.download_button("下载本轮 CSV", report_csv(report), file_name=f"round_{round_no}_{company['code']}.csv", mime="text/csv")


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
    hero("财富曲线", "按照官方样式对比全部队伍每轮 Net Assets（期末现金 − 负债）。")
    with connect() as conn:
        rows = all_rows(
            conn,
            "SELECT c.id,c.code,c.name,r.round_no,r.net_assets FROM results r JOIN companies c ON c.id=r.company_id ORDER BY r.round_no,c.id",
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
    chart = (
        alt.Chart(frame)
        .mark_line(point=alt.OverlayMarkDef(size=58), strokeWidth=2.2)
        .encode(
            x=alt.X("轮次:Q", title="Round", axis=alt.Axis(tickMinStep=1)),
            y=alt.Y("净资产:Q", title="Net Assets (RMB)", scale=alt.Scale(zero=False), axis=alt.Axis(format=",")),
            color=alt.Color("队伍:N", title=None, legend=alt.Legend(orient="right")),
            tooltip=[alt.Tooltip("队伍:N"), alt.Tooltip("轮次:Q", format=".0f"), alt.Tooltip("净资产:Q", format=",.0f")],
        )
        .properties(height=520, title="Chart for Simulation")
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)
    latest = frame.sort_values("轮次").groupby("队伍", as_index=False).tail(1).sort_values("净资产", ascending=False)
    st.dataframe(latest[["队伍", "轮次", "净资产"]], hide_index=True, use_container_width=True, column_config={"净资产": st.column_config.NumberColumn(format="¥ %.0f")})


def render_player_kds(company: sqlite3.Row) -> None:
    hero("规则速查", "比赛参数与核心公式只读；管理员可在后台统一修改。")
    with connect() as conn:
        settings = settings_dict(conn)
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
    st.markdown(
        f"""
        - 每轮工时：`504`
        - 工人 : 工程师 = `A × B × E : C × D`
        - 工资产能倍率：`min(本队工资 ÷ 本轮平均工资, 1.1)`
        - MA 指数：`MA 投资 ÷ (工人 + 工程师)`
        - QI 指数：`QI 投资 ÷ (旧产品 × 1.2 + 新产品)`
        - QI 大量 CPI 门槛：`城市最高价 ÷ 50`
        - CPI：按城市独立执行赠品、第一层、第二层、福利 1/2；价格差使用 `{int(settings['cpi_price_power'])}` 次方。
        """
    )
    setting_frame = pd.DataFrame([{"参数": GLOBAL_SETTING_LABELS.get(key, key), "值": value} for key, value in settings.items() if key in GLOBAL_SETTING_LABELS])
    st.dataframe(setting_frame, hide_index=True, use_container_width=True)
    market_frame = pd.DataFrame([dict(row) for row in markets]).rename(columns=MARKET_COLUMNS)
    st.dataframe(market_frame, hide_index=True, use_container_width=True)


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
        st.dataframe(ranking_table(ranking), hide_index=True, use_container_width=True, column_config={"净现金": st.column_config.NumberColumn(format="¥ %.0f"), "现金": st.column_config.NumberColumn(format="¥ %.0f"), "本轮利润": st.column_config.NumberColumn(format="¥ %.0f")})


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
    if not setup_editable:
        st.info("比赛已开始：为避免影响结算，赛前资料已锁定。密码仍可重置。")

    for company in companies:
        with st.expander(f"{company['code']} · {company['name']} · {company['home_city'] or '未选主场'}"):
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
    decision = dict(decision_row) if decision_row else {
        "loan_change": 0.0, "worker_delta": 0, "worker_salary": float(home["worker_initial_salary"]),
        "engineer_delta": 0, "engineer_salary": float(home["engineer_initial_salary"]),
        "management_investment": 0.0, "production_volume": 0, "quality_investment": 0.0,
        "research_investment": 0.0, "submitted_at": None,
    }
    status_text = "已提交" if decision.get("submitted_at") else "未提交"
    st.info(f"第 {round_no} 轮 · {STATUS_LABELS.get(round_row['status'], round_row['status'])} · 玩家状态：{status_text}")
    if not editable:
        st.warning("本轮已经结算，决策仅可查看，不能再修改。")

    with st.form(f"admin_decision_{round_no}_{company['id']}"):
        with st.expander("💰 银行贷款", expanded=False):
            loan_change = st.number_input("贷款变化", value=float(decision["loan_change"]), step=10_000.0, disabled=not editable)
        with st.expander("👥 人力资源", expanded=True):
            cols = st.columns(2)
            worker_delta = cols[0].number_input("工人增减", min_value=-current_workers, value=int(decision["worker_delta"]), step=1, disabled=not editable)
            worker_salary = cols[1].number_input("工人月薪", min_value=float(settings["salary_min"]), max_value=float(settings["salary_max"]), value=float(decision["worker_salary"]), step=50.0, disabled=not editable)
            cols = st.columns(2)
            engineer_delta = cols[0].number_input("工程师增减", min_value=-current_engineers, value=int(decision["engineer_delta"]), step=1, disabled=not editable)
            engineer_salary = cols[1].number_input("工程师月薪", min_value=float(settings["salary_min"]), max_value=float(settings["salary_max"]), value=float(decision["engineer_salary"]), step=50.0, disabled=not editable)
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
                    "agent_delta": cols[0].number_input("Agent 增减", min_value=-current_agents, max_value=int(settings["max_agent_add_per_round"]), value=int(saved.get("agent_delta", 0)), step=1, key=f"admin_agent_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "marketing_investment": cols[1].number_input("营销投入（MI）", min_value=0.0, value=float(saved.get("marketing_investment", 0.0)), step=10_000.0, key=f"admin_mi_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "price": cols[2].number_input("售价", min_value=float(settings["price_min"]), max_value=min(float(settings["price_max"]), float(market["max_price"])), value=float(saved.get("price", market["initial_avg_price"])), step=100.0, key=f"admin_price_{company['id']}_{round_no}_{city}", disabled=not editable),
                    "order_report": cols[3].checkbox("购买市场报告", value=bool(saved.get("order_report", 0)), key=f"admin_report_{company['id']}_{round_no}_{city}", disabled=not editable),
                }
        mark_submitted = st.checkbox("保存后标记为已提交", value=bool(decision.get("submitted_at")), disabled=not editable)
        save = st.form_submit_button("保存玩家决策", type="primary", disabled=not editable, use_container_width=True)
    if save:
        submitted_at = now_iso() if mark_submitted else None
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
        st.warning("比赛已进入第一轮，KDS 已锁定。输入 UNLOCK KDS 后才可编辑，修改只影响尚未结算的回合。")
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
        else:
            with connect() as conn:
                for key, value in values.items():
                    set_setting(conn, key, value)
            flash("success", "全局 KDS 已保存。")
            st.rerun()

    st.subheader("城市参数")
    frame = pd.DataFrame([dict(row) for row in markets])
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
            with connect() as conn:
                for record in edited.to_dict("records"):
                    values_sql = [int(bool(record["home_enabled"]))] + [float(record[column]) for column in numeric_columns]
                    assignments = ["home_enabled=?"] + [f"{column}=?" for column in numeric_columns]
                    conn.execute(f"UPDATE market_config SET {','.join(assignments)} WHERE city=?", (*values_sql, record["city"]))
            flash("success", "城市 KDS 已保存。")
            st.rerun()
        except (TypeError, ValueError):
            st.error("城市参数必须是有效数字。")

    with st.form("add_city", clear_on_submit=True):
        city = st.text_input("新增城市名称", disabled=not unlocked)
        add_city = st.form_submit_button("新增城市", disabled=not unlocked)
    if add_city and city.strip():
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO market_config(city,home_enabled,max_loan,interest_rate,worker_initial_salary,engineer_initial_salary,"
                    "component_material,product_material,component_storage,product_storage,population,penetration,initial_avg_price,max_price,"
                    "transport_cost,worker_training_cost,engineer_training_cost) VALUES(?,1,0,0,0,0,0,0,0,0,1,0.01,0,?,0,0,0)",
                    (city.strip(), get_setting(conn, "price_max", 25_000.0)),
                )
            flash("success", f"已新增城市 {city.strip()}，请补充参数。")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("城市名称重复。")


def render_admin_rounds() -> None:
    hero("回合控制", "第一轮需全部队伍完成赛前设置；每轮需全部队伍提交后才能结算。")
    with connect() as conn:
        round_row = current_round(conn)
        setup = setup_status(conn)
        submission = submission_status(conn, int(round_row["round_no"])) if round_row and round_row["status"] in ("open", "paused") else None
        decisions = all_rows(
            conn,
            "SELECT c.code,c.name,c.home_city,c.setup_submitted_at,d.submitted_at,d.production_volume,d.management_investment,d.quality_investment,d.research_investment "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=? ORDER BY c.id",
            (int(round_row["round_no"]),),
        ) if round_row else []
        history = all_rows(conn, "SELECT * FROM rounds ORDER BY round_no DESC")
    round_banner(round_row)
    st.write(f"赛前就绪：{setup['ready']}/{setup['total']}")
    if submission:
        st.write(f"本轮提交：{submission['submitted']}/{submission['total']}")

    if round_row and round_row["status"] == "waiting":
        minutes = st.number_input("第一轮时长（分钟）", min_value=1, value=30, step=1)
        if st.button("开始第一轮", type="primary", disabled=not bool(setup["all_ready"])):
            start = datetime.now(timezone.utc)
            with connect() as conn:
                conn.execute("UPDATE rounds SET status='open',starts_at=?,ends_at=? WHERE round_no=?", (start.isoformat(), (start + timedelta(minutes=int(minutes))).isoformat(), round_row["round_no"]))
            flash("success", "第一轮已开始。")
            st.rerun()
    elif round_row and round_row["status"] in ("open", "paused"):
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
        if cols[3].button("结算本轮", type="primary", use_container_width=True, disabled=not bool(submission and submission["all_submitted"])):
            try:
                with connect() as conn:
                    settle_round(conn, int(round_row["round_no"]))
                flash("success", f"第 {round_row['round_no']} 轮结算完成。")
                st.rerun()
            except Exception as exc:
                st.error(f"结算失败：{exc}")
    elif round_row and round_row["status"] == "settled":
        minutes = st.number_input("下一轮时长（分钟）", min_value=1, value=30, step=1)
        if st.button("开启下一轮", type="primary"):
            start = datetime.now(timezone.utc)
            next_round = int(round_row["round_no"]) + 1
            with connect() as conn:
                conn.execute("INSERT INTO rounds(round_no,status,starts_at,ends_at) VALUES(?,'open',?,?)", (next_round, start.isoformat(), (start + timedelta(minutes=int(minutes))).isoformat()))
            flash("success", f"第 {next_round} 轮已开始。")
            st.rerun()

    if decisions:
        st.subheader("队伍状态")
        status_frame = pd.DataFrame(
            [{"队伍": row["code"], "公司": row["name"], "主场": row["home_city"] or "—", "赛前就绪": bool(row["setup_submitted_at"]), "本轮提交": bool(row["submitted_at"]), "计划产量": row["production_volume"] or 0, "MA": row["management_investment"] or 0, "QI": row["quality_investment"] or 0, "专利": row["research_investment"] or 0} for row in decisions]
        )
        st.dataframe(status_frame, hide_index=True, use_container_width=True)
    st.subheader("回合历史")
    st.dataframe(pd.DataFrame([dict(row) for row in history]), hide_index=True, use_container_width=True)


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
        except Exception as exc:
            st.error(f"恢复失败：{exc}")
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
