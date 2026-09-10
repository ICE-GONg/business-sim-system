from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from .db import all_rows, effective_employee_count, employee_count, get_setting, now_iso, one


BOT_API_VERSION = 3

BOT_PLANS = (
    {"production": 350, "ma": 1340, "markets": 1, "agents": 2, "research": 1_500_000},
    {"production": 850, "ma": 1480, "markets": 1, "agents": 2, "research": 1_800_000},
    {"production": 1700, "ma": 1650, "markets": 2, "agents": 3, "research": 2_000_000},
    {"production": 3000, "ma": 1900, "markets": 2, "agents": 3, "research": 2_200_000},
    {"production": 4800, "ma": 2250, "markets": 3, "agents": 4, "research": 2_000_000},
    {"production": 7200, "ma": 2700, "markets": 3, "agents": 4, "research": 1_600_000},
    {"production": 10000, "ma": 3300, "markets": 4, "agents": 5, "research": 0},
)

# Seven deliberately different opponents. Multipliers apply to CPI indices,
# not raw cash, so they remain meaningful when the KDS changes.
BOT_STYLES = (
    {"ma": 1.00, "qi": 1.05, "mi": 1.00, "high": 0.940, "cut": 80},
    {"ma": 1.35, "qi": 1.10, "mi": 0.95, "high": 0.945, "cut": 110},
    {"ma": 1.05, "qi": 1.70, "mi": 1.00, "high": 0.950, "cut": 150},
    {"ma": 1.20, "qi": 1.25, "mi": 1.55, "high": 0.955, "cut": 190},
    {"ma": 2.20, "qi": 1.05, "mi": 0.90, "high": 0.960, "cut": 230},
    {"ma": 1.10, "qi": 2.00, "mi": 1.35, "high": 0.965, "cut": 280},
    {"ma": 3.00, "qi": 1.80, "mi": 1.70, "high": 0.970, "cut": 340},
)


def _previous_report(conn: sqlite3.Connection, company_id: int, round_no: int) -> dict[str, Any]:
    row = one(
        conn,
        "SELECT report_json FROM results WHERE company_id=? AND round_no>=1 AND round_no<? "
        "ORDER BY round_no DESC LIMIT 1",
        (company_id, max(1, round_no)),
    )
    if not row:
        return {}
    try:
        return json.loads(row["report_json"])
    except (TypeError, ValueError):
        return {}


def _bot_salary(
    report: dict[str, Any], role: str, fallback: float, previous: float,
    profile: int, minimum: float, maximum: float, change_limit: float,
) -> tuple[float, float]:
    """Target 1.05--1.10 of last round's home average, capped at useful output."""
    key = "average_worker_salary" if role == "worker" else "average_engineer_salary"
    average = float(report.get("human_resources", {}).get(key, fallback) or fallback)
    target = average * (1.05 + profile * (0.05 / 6.0))
    low = max(minimum, previous - change_limit)
    high = min(maximum, previous + change_limit)
    salary = min(high, max(low, target))
    return salary, min(1.10, max(0.01, salary / max(average, 1.0)))


def _staff_delta(
    conn: sqlite3.Connection, company_id: int, role: str, round_no: int, required: float,
) -> int:
    """Hire shortages and lay off only staff above the calculated requirement."""
    current = employee_count(conn, company_id, role)
    effective = effective_employee_count(conn, company_id, role, round_no)
    required = max(0.0, required)
    if effective < required - 1e-9:
        return math.ceil(required - effective)
    removable = 0
    for cohort in all_rows(
        conn,
        "SELECT count,hire_round FROM employee_cohorts WHERE company_id=? AND role=? "
        "ORDER BY hire_round DESC,id DESC",
        (company_id, role),
    ):
        factor = 1.10 if round_no - int(cohort["hire_round"]) >= 2 else 1.0
        count = min(int(cohort["count"]), max(0, math.floor((effective - required) / factor)))
        removable += count
        effective -= count * factor
    return -min(current, removable)


def _selected_markets(bot: sqlite3.Row | dict[str, Any], markets: list[dict[str, Any]], count: int) -> list[int]:
    profile = int(bot["bot_profile"] if bot["bot_profile"] is not None else bot["id"]) % 7
    home = str(bot["home_city"] or markets[profile % len(markets)]["city"])
    home_index = next((i for i, market in enumerate(markets) if market["city"] == home), 0)
    selected = [home_index]
    cursor = home_index + profile + 1
    while len(selected) < min(len(markets), count):
        candidate = cursor % len(markets)
        if candidate not in selected:
            selected.append(candidate)
        cursor += 1
    return selected


def _submit_bots(conn: sqlite3.Connection, round_no: int, super_mode: bool) -> int:
    bots = all_rows(
        conn,
        "SELECT * FROM companies WHERE is_bot=1 AND is_super_bot=? ORDER BY id",
        (int(super_mode),),
    )
    markets = [dict(row) for row in all_rows(conn, "SELECT * FROM market_config ORDER BY city")]
    if not bots or not markets:
        return 0

    official_round = 1 if round_no < 0 else round_no
    plan_index = min(6, max(0, official_round - 1))
    plan = BOT_PLANS[plan_index]
    setting = lambda key, default: float(get_setting(conn, key, default))
    salary_min, salary_max = setting("salary_min", 1000), setting("salary_max", 10000)
    salary_change = setting("salary_change_limit", 1000)
    price_min, price_max = setting("price_min", 3500), setting("price_max", 25000)
    growth = setting("market_growth", 1.10)
    worker_need, worker_hours = setting("component_workers", 3), setting("component_hours", 7)
    engineer_need, engineer_hours = setting("product_engineers", 4), setting("product_hours", 14)
    component_need = max(1, int(round(setting("components_per_product", 7))))
    worker_training = setting("worker_training_cost", 0)
    engineer_training = setting("engineer_training_cost", 0)
    add_agent_cost = setting("agent_add_cost", 300000)
    transport_cost = setting("transport_cost", 0)
    patent_factor = setting("patent_factor", 0.70)
    ma_threshold = setting("cpi_ma_large_threshold", 1300)
    research_goal = setting("research_75", 6000000) * setting("research_hidden_threshold_multiplier", 4 / 3)
    research_goal += setting("research_buffer", 150000)

    market_count = int(plan["markets"]) + (2 if super_mode else 0)
    selected_by_bot = {
        int(bot["id"]): _selected_markets(bot, markets, market_count) for bot in bots
    }
    city_competitors: dict[int, int] = {}
    for index, market in enumerate(markets):
        human_sellers = one(
            conn,
            "SELECT COUNT(*) AS n FROM agents a JOIN companies c ON c.id=a.company_id "
            "WHERE c.is_bot=0 AND a.city=? AND a.count>0",
            (market["city"],),
        )
        planned_bots = sum(index in indices for indices in selected_by_bot.values())
        city_competitors[index] = max(1, int(human_sellers["n"] if human_sellers else 0) + planned_bots)

    submitted = 0
    for bot_row in bots:
        bot = dict(bot_row)
        company_id = int(bot["id"])
        if one(conn, "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?", (company_id, round_no)):
            continue
        profile = int(bot["bot_profile"] if bot["bot_profile"] is not None else company_id) % 7
        style = BOT_STYLES[profile]
        variation = 0.94 + profile * 0.02
        home = str(bot["home_city"] or markets[profile % len(markets)]["city"])
        home_market = next((market for market in markets if market["city"] == home), markets[0])
        report = _previous_report(conn, company_id, round_no)
        previous = one(
            conn,
            "SELECT d.worker_salary,d.engineer_salary FROM decisions d JOIN results r "
            "ON r.company_id=d.company_id AND r.round_no=d.round_no WHERE d.company_id=? "
            "AND d.round_no>=1 AND d.round_no<? ORDER BY d.round_no DESC LIMIT 1",
            (company_id, max(1, round_no)),
        )
        previous_worker = float(previous["worker_salary"]) if previous else float(home_market["worker_initial_salary"])
        previous_engineer = float(previous["engineer_salary"]) if previous else float(home_market["engineer_initial_salary"])
        worker_salary, worker_multiplier = _bot_salary(
            report, "worker", float(home_market["worker_initial_salary"]), previous_worker,
            profile, salary_min, salary_max, salary_change,
        )
        engineer_salary, engineer_multiplier = _bot_salary(
            report, "engineer", float(home_market["engineer_initial_salary"]), previous_engineer,
            profile, salary_min, salary_max, salary_change,
        )

        selected = selected_by_bot[company_id]

        agent_plan: dict[int, tuple[int, int]] = {}
        agent_cost = 0.0
        for index, market in enumerate(markets):
            row = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company_id, market["city"]))
            current = int(row["count"] if row else 0)
            desired = int(plan["agents"]) if index in selected else current
            delta = max(-current, min(3, desired - current))
            agent_plan[index] = (delta, current + delta)
            if delta > 0:
                agent_cost += delta * add_agent_cost

        saturated: dict[int, bool] = {}
        previous_prices: dict[int, float] = {}
        for index in selected:
            market = markets[index]
            stats = one(
                conn,
                "SELECT market_size,player_total_volume,average_price FROM market_round_stats "
                "WHERE city=? AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
                (market["city"], max(1, round_no)),
            )
            saturation_history = one(
                conn,
                "SELECT MAX(player_total_volume / MAX(market_size,1)) AS peak FROM market_round_stats "
                "WHERE city=? AND round_no>=1 AND round_no<?",
                (market["city"], max(1, round_no)),
            )
            saturated[index] = bool(saturation_history and float(saturation_history["peak"] or 0) >= 0.60)
            previous_prices[index] = float(stats["average_price"]) if stats else float(market["initial_avg_price"])

        old_products = int(bot["product_inventory"] or 0)
        old_components = int(bot["component_inventory"] or 0)
        desired_available = int(float(plan["production"]) * variation)
        prior_production = report.get("production", {})
        if prior_production:
            prior_sold = int(prior_production.get("sold", 0) or 0)
            prior_total = int(prior_production.get("old_products", 0) or 0) + int(prior_production.get("produced", 0) or 0)
            if prior_total and prior_sold >= prior_total * 0.95 and not any(saturated.values()):
                desired_available = max(desired_available, int(prior_sold * 1.35))
            elif int(prior_production.get("surplus", 0) or 0) > max(20, prior_sold * 0.30):
                desired_available = min(desired_available, int(prior_sold * 1.12 + old_products))
        production_goal = max(0, desired_available - old_products)
        material_factor = patent_factor ** int(bot["patents"] or 0)
        use_qi, use_mi = super_mode or plan_index >= 1, super_mode or plan_index >= 2
        research_balance = max(0.0, float(bot["research_balance"] or 0))
        research_needed = max(0.0, research_goal - research_balance)
        # A bot never submits a token patent amount: it either funds the whole
        # remaining threshold or submits zero. Super bots postpone R&D until
        # later rounds and only when they hold a large cash cushion; profitable
        # production and reliable sell-through take priority over early R&D.
        super_research_ready = not super_mode or (
            official_round >= 4 and float(bot["cash"]) >= research_goal * 4.0
        )
        can_fully_fund_research = super_research_ready and float(bot["cash"]) + 1e-9 >= research_needed
        research = research_needed if plan_index != 6 and can_fully_fund_research else 0.0

        rival_metrics: dict[int, list[dict[str, float]]] = {index: [] for index in range(len(markets))}
        if super_mode:
            rival_rows = all_rows(
                conn,
                "SELECT d.company_id,d.worker_delta,d.engineer_delta,d.management_investment,d.production_volume,"
                "d.quality_investment,cd.city,cd.agent_delta,cd.marketing_investment,cd.price,c.product_inventory "
                "FROM decisions d JOIN companies c ON c.id=d.company_id "
                "JOIN city_decisions cd ON cd.company_id=d.company_id AND cd.round_no=d.round_no "
                "WHERE d.round_no=? AND d.submitted_at IS NOT NULL AND d.company_id<>?",
                (round_no, company_id),
            )
            market_index = {str(market["city"]): index for index, market in enumerate(markets)}
            for rival in rival_rows:
                index = market_index[str(rival["city"])]
                agent_row = one(
                    conn, "SELECT count FROM agents WHERE company_id=? AND city=?",
                    (rival["company_id"], rival["city"]),
                )
                agents = max(0, int(agent_row["count"] if agent_row else 0) + int(rival["agent_delta"] or 0))
                if agents <= 0:
                    continue
                workers = max(0, employee_count(conn, int(rival["company_id"]), "worker") + int(rival["worker_delta"] or 0))
                engineers = max(0, employee_count(conn, int(rival["company_id"]), "engineer") + int(rival["engineer_delta"] or 0))
                ma_index = float(rival["management_investment"] or 0) / max(1, workers + engineers)
                qi_denominator = float(rival["product_inventory"] or 0) * 1.2 + float(rival["production_volume"] or 0)
                qi_index = float(rival["quality_investment"] or 0) / max(1.0, qi_denominator)
                rival_metrics[index].append({
                    "ma": ma_index,
                    "qi": qi_index,
                    "mi_effective": float(rival["marketing_investment"] or 0) * (1.0 + agents * 0.10),
                    "price": float(rival["price"] or 0),
                })

        def budget_for(production: int) -> dict[str, Any]:
            components_to_make = max(0, production * component_need - old_components)
            worker_effective = components_to_make * worker_need * worker_hours / 504.0
            engineer_effective = production * engineer_need * engineer_hours / 504.0
            # Do not budget production around an optimistic wage multiplier:
            # the current-round market average is endogenous and can rise when
            # other teams also raise salaries. A multiplier above 1 is upside,
            # not capacity the bot must rely upon to complete its plan.
            worker_planning_multiplier = min(1.0, worker_multiplier)
            engineer_planning_multiplier = min(1.0, engineer_multiplier)
            worker_delta = _staff_delta(conn, company_id, "worker", round_no, worker_effective / worker_planning_multiplier)
            engineer_delta = _staff_delta(conn, company_id, "engineer", round_no, engineer_effective / engineer_planning_multiplier)
            workers = max(0, employee_count(conn, company_id, "worker") + worker_delta)
            engineers = max(0, employee_count(conn, company_id, "engineer") + engineer_delta)
            staff_cost = workers * worker_salary * 3 + engineers * engineer_salary * 3
            staff_cost += max(0, worker_delta) * worker_training + max(0, engineer_delta) * engineer_training
            staff_cost += max(0, -worker_delta) * worker_salary + max(0, -engineer_delta) * engineer_salary
            new_components = components_to_make
            material_cost = new_components * float(home_market["component_material"]) * material_factor
            material_cost += production * float(home_market["product_material"]) * material_factor
            storage_cost = max(0, old_components + new_components - int(bot["component_storage_capacity"] or 0)) * float(home_market["component_storage"])
            storage_cost += max(0, old_products + production - int(bot["product_storage_capacity"] or 0)) * float(home_market["product_storage"])
            ma_index = max(ma_threshold * 1.02, float(plan["ma"]) * variation) * float(style["ma"])
            if super_mode:
                rival_ma = max((item["ma"] for index in selected for item in rival_metrics[index]), default=0.0)
                ma_index = min(5000.0, max(ma_index, ma_threshold * 1.25, rival_ma * 1.25))
            management = ma_index * max(1, workers + engineers)
            denominator = old_products * 1.2 + production
            qi_line = max(float(markets[i]["max_price"]) / 50.0 for i in selected)
            qi_index = qi_line * (1.03 + profile * 0.01) * float(style["qi"])
            if super_mode:
                rival_qi = max((item["qi"] for index in selected for item in rival_metrics[index]), default=0.0)
                qi_index = min(qi_line * 5.0, max(qi_index, qi_line * 1.25, rival_qi * 1.25))
            quality = qi_index * max(1.0, denominator) if use_qi else 0.0
            marketing: dict[int, float] = {}
            for index in selected:
                market = markets[index]
                active_agents = agent_plan[index][1]
                size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                threshold = (float(market["max_price"]) / 50.0) * size * 0.20
                threshold /= max(1.0, 1.0 + active_agents * 0.10) * 1.5 * 2.0
                target_mi = threshold * (1.03 + profile * 0.01) * float(style["mi"])
                if super_mode:
                    rival_mi = max((item["mi_effective"] for item in rival_metrics[index]), default=0.0)
                    target_mi = min(threshold * 4.0, max(target_mi, threshold * 1.25, rival_mi * 1.25 / (1.0 + active_agents * 0.10)))
                marketing[index] = target_mi if use_mi and active_agents else 0.0
            total = staff_cost + material_cost + storage_cost + agent_cost + management + quality + sum(marketing.values())
            return {
                "worker_delta": worker_delta, "engineer_delta": engineer_delta,
                "workers": workers, "engineers": engineers, "management": management,
                "quality": quality, "marketing": marketing, "total": total,
            }

        # Patent money is charged after revenue, but reserving it here prevents
        # a zero-sales edge case from turning a threshold patent decision into
        # a partial, ineffective payment.
        cash_budget = max(0.0, float(bot["cash"]) - research) * 0.985
        # Empty markets reward converting available cash into saleable output.
        # Four times the round rhythm is only a search ceiling; affordability
        # and existing inventory decide the actual submitted production.
        safe_market_units = 0.0
        for index in selected:
            market = markets[index]
            city_size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
            safe_market_units += city_size * 0.60 / city_competitors[index]
        expansion_ceiling = max(0, int(safe_market_units * variation) - old_products)
        production_goal = min(production_goal, expansion_ceiling)
        low, high = 0, max(production_goal, expansion_ceiling)
        while low < high:
            middle = (low + high + 1) // 2
            if budget_for(middle)["total"] <= cash_budget + 1e-9:
                low = middle
            else:
                high = middle - 1
        production = low
        budget = budget_for(production)
        if plan_index == 6:
            spare = max(0.0, cash_budget - float(budget["total"]))
            ma_cap = 5000 * max(1, budget["workers"] + budget["engineers"])
            budget["management"] = min(ma_cap, float(budget["management"]) + spare)

        conn.execute(
            "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,"
            "engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, 0, budget["worker_delta"], worker_salary, budget["engineer_delta"],
             engineer_salary, budget["management"], production, budget["quality"], research, now_iso()),
        )
        for index, market in enumerate(markets):
            delta, _ = agent_plan[index]
            marketing = budget["marketing"].get(index, 0.0)
            cap = min(price_max, float(market["max_price"]))
            if index in selected and saturated.get(index, False):
                reference = previous_prices[index] - float(style["cut"])
            else:
                reference = cap * float(style["high"])
            if super_mode and index in selected:
                rival_prices = [item["price"] for item in rival_metrics[index] if item["price"] > 0]
                rival_floor = min(rival_prices, default=previous_prices[index])
                reference = min(previous_prices[index] - 100 - profile * 20, rival_floor * (0.985 - profile * 0.002))
                component_labor = component_need * worker_need * worker_hours / 504.0 * worker_salary * 3
                product_labor = engineer_need * engineer_hours / 504.0 * engineer_salary * 3
                direct_unit_cost = (
                    component_need * float(home_market["component_material"]) * material_factor
                    + float(home_market["product_material"]) * material_factor
                    + component_labor + product_labor
                    + (transport_cost if str(market["city"]) != home else 0.0)
                )
                available_units = max(1, old_products + production)
                allocated_operating_cost = (float(budget["total"]) + research) / available_units
                # Never undercut below a profitable floor. The operating-cost
                # floor includes staffing, agents and all three CPI investments;
                # direct cost remains a separate safeguard for inventory rounds.
                profitable_floor = max(direct_unit_cost * 1.25, allocated_operating_cost * 1.12)
                reference = max(reference, profitable_floor)
            price = min(cap, max(price_min, reference))
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                "VALUES(?,?,?,?,?,?,0)",
                (company_id, round_no, market["city"], delta, marketing, price),
            )
        submitted += 1
    return submitted


def submit_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Ordinary bots submit immediately when a round opens."""
    return _submit_bots(conn, round_no, False)


def submit_super_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Super bots wait until every non-super team has submitted."""
    missing = one(
        conn,
        "SELECT COUNT(*) AS n FROM companies c WHERE c.is_super_bot=0 AND NOT EXISTS "
        "(SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=? AND d.submitted_at IS NOT NULL)",
        (round_no,),
    )
    if missing and int(missing["n"]) > 0:
        raise ValueError("仍有真人玩家或普通 Bot 未提交，超级 Bot 暂不能读取本轮数据。")
    conn.execute(
        "DELETE FROM city_decisions WHERE round_no=? AND company_id IN "
        "(SELECT id FROM companies WHERE is_super_bot=1)",
        (round_no,),
    )
    conn.execute(
        "DELETE FROM decisions WHERE round_no=? AND company_id IN "
        "(SELECT id FROM companies WHERE is_super_bot=1)",
        (round_no,),
    )
    return _submit_bots(conn, round_no, True)
