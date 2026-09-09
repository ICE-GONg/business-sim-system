from __future__ import annotations

import sqlite3

from .db import all_rows, get_setting, now_iso, one


BOT_API_VERSION = 1

# Seven independent round plans. A small profile multiplier keeps bots from
# submitting identical decisions while preserving the same overall strategy.
BOT_PLANS = (
    {"workers": 180, "engineers": 36, "production": 2200, "ma": 300_000, "qi": 180_000, "research": 500_000, "marketing": 250_000, "markets": 1, "agents": 2, "price": 1.02},
    {"workers": 70, "engineers": 14, "production": 4800, "ma": 450_000, "qi": 320_000, "research": 750_000, "marketing": 420_000, "markets": 2, "agents": 2, "price": 0.98},
    {"workers": 50, "engineers": 10, "production": 7200, "ma": 650_000, "qi": 520_000, "research": 900_000, "marketing": 650_000, "markets": 2, "agents": 3, "price": 0.95},
    {"workers": 35, "engineers": 7, "production": 9800, "ma": 850_000, "qi": 720_000, "research": 1_100_000, "marketing": 850_000, "markets": 3, "agents": 3, "price": 0.92},
    {"workers": 20, "engineers": 4, "production": 12_500, "ma": 1_050_000, "qi": 950_000, "research": 1_250_000, "marketing": 1_050_000, "markets": 3, "agents": 4, "price": 0.90},
    {"workers": 10, "engineers": 2, "production": 15_000, "ma": 1_250_000, "qi": 1_150_000, "research": 1_400_000, "marketing": 1_250_000, "markets": 4, "agents": 4, "price": 0.88},
    {"workers": 0, "engineers": 0, "production": 17_500, "ma": 1_450_000, "qi": 1_350_000, "research": 1_500_000, "marketing": 1_450_000, "markets": 4, "agents": 5, "price": 0.86},
)


def submit_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Create and immediately submit this round's decisions for every bot."""
    bots = all_rows(conn, "SELECT * FROM companies WHERE is_bot=1 ORDER BY id")
    if not bots:
        return 0
    markets = [dict(row) for row in all_rows(conn, "SELECT * FROM market_config ORDER BY city")]
    if not markets:
        return 0
    plan_index = min(6, max(0, (1 if int(round_no) < 0 else int(round_no)) - 1))
    base = BOT_PLANS[plan_index]
    salary_min = float(get_setting(conn, "salary_min", 1_000.0))
    salary_max = float(get_setting(conn, "salary_max", 10_000.0))
    price_min = float(get_setting(conn, "price_min", 3_500.0))
    price_max = float(get_setting(conn, "price_max", 25_000.0))
    submitted = 0
    for bot in bots:
        company_id = int(bot["id"])
        if one(conn, "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?", (company_id, round_no)):
            continue
        profile = int(bot["bot_profile"] or company_id) % 7
        multiplier = 0.91 + profile * 0.03
        home = str(bot["home_city"] or markets[profile % len(markets)]["city"])
        home_market = next((market for market in markets if market["city"] == home), markets[0])
        previous = one(conn, "SELECT worker_salary,engineer_salary FROM decisions WHERE company_id=? ORDER BY round_no DESC LIMIT 1", (company_id,))
        worker_salary = float(previous["worker_salary"]) if previous else float(home_market["worker_initial_salary"])
        engineer_salary = float(previous["engineer_salary"]) if previous else float(home_market["engineer_initial_salary"])
        worker_salary = min(salary_max, max(salary_min, worker_salary + (100 if plan_index % 2 else 0)))
        engineer_salary = min(salary_max, max(salary_min, engineer_salary + (100 if plan_index % 2 else 0)))
        worker_delta = int(round(float(base["workers"]) * multiplier))
        engineer_delta = int(round(float(base["engineers"]) * multiplier))
        conn.execute(
            "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, 0, worker_delta, worker_salary, engineer_delta, engineer_salary,
             float(base["ma"]) * multiplier, int(float(base["production"]) * multiplier),
             float(base["qi"]) * multiplier, float(base["research"]) * multiplier, now_iso()),
        )
        home_index = next((index for index, market in enumerate(markets) if market["city"] == home), 0)
        selected = {home_index}
        for offset in range(1, int(base["markets"])):
            selected.add((home_index + profile + offset) % len(markets))
        for index, market in enumerate(markets):
            city = str(market["city"])
            current = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company_id, city))
            current_agents = int(current["count"] if current else 0)
            desired_agents = int(base["agents"]) if index in selected else current_agents
            agent_delta = max(-current_agents, min(3, desired_agents - current_agents))
            active_after = current_agents + agent_delta
            marketing = float(base["marketing"]) * multiplier if index in selected and active_after > 0 else 0.0
            reference_price = float(market["initial_avg_price"]) * float(base["price"]) * (0.97 + profile * 0.01)
            price = min(price_max, float(market["max_price"]), max(price_min, reference_price))
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) VALUES(?,?,?,?,?,?,0)",
                (company_id, round_no, city, agent_delta, marketing, price),
            )
        submitted += 1
    return submitted
