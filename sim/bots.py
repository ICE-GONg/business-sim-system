from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from .cpi import allocate_city_cpi
from .db import all_rows, effective_employee_count, employee_count, get_setting, now_iso, one


BOT_API_VERSION = 4

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
    {"ma": 0.72, "qi": 0.85, "mi": 1.05, "high": 0.940, "cut": 80},
    {"ma": 1.00, "qi": 1.15, "mi": 1.25, "high": 0.945, "cut": 130},
    {"ma": 1.38, "qi": 1.75, "mi": 1.55, "high": 0.950, "cut": 190},
    {"ma": 1.82, "qi": 1.05, "mi": 1.90, "high": 0.955, "cut": 260},
    {"ma": 2.30, "qi": 2.25, "mi": 2.25, "high": 0.960, "cut": 340},
    {"ma": 2.85, "qi": 1.45, "mi": 2.70, "high": 0.965, "cut": 440},
    {"ma": 3.55, "qi": 2.90, "mi": 3.20, "high": 0.970, "cut": 560},
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


def _upper_typical(values: list[float]) -> float:
    """Strong rival reference that ignores one irrational all-in outlier."""
    clean = sorted(max(0.0, float(value)) for value in values)
    if not clean:
        return 0.0
    return clean[min(len(clean) - 1, math.floor((len(clean) - 1) * 0.80))]


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
    initial_cash = max(1.0, setting("initial_cash", 15_000_000))
    total_rounds = max(1, int(setting("total_rounds", 5)))
    research_goal = setting("research_75", 6000000) * setting("research_hidden_threshold_multiplier", 4 / 3)
    research_goal += setting("research_buffer", 150000)

    selected_by_bot: dict[int, list[int]] = {}
    for bot in bots:
        wealth_multiple = max(1.0, float(bot["cash"]) / initial_cash)
        wealth_expansion = min(3, max(0, int(math.log2(wealth_multiple)) // 2))
        market_count = int(plan["markets"]) + wealth_expansion + (2 if super_mode else 0)
        if official_round >= total_rounds:
            market_count += 2
        selected_by_bot[int(bot["id"])] = _selected_markets(bot, markets, min(len(markets), market_count))
    city_competitors: dict[int, int] = {}
    for index, market in enumerate(markets):
        seller_ids = {
            int(row["company_id"])
            for row in all_rows(conn, "SELECT company_id FROM agents WHERE city=? AND count>0", (market["city"],))
        }
        for row in all_rows(
            conn,
            "SELECT cd.company_id,COALESCE(a.count,0)+cd.agent_delta AS agents_after "
            "FROM city_decisions cd LEFT JOIN agents a ON a.company_id=cd.company_id AND a.city=cd.city "
            "WHERE cd.round_no=? AND cd.city=?",
            (round_no, market["city"]),
        ):
            if int(row["agents_after"] or 0) > 0:
                seller_ids.add(int(row["company_id"]))
            else:
                seller_ids.discard(int(row["company_id"]))
        seller_ids.update(company_id for company_id, indices in selected_by_bot.items() if index in indices)
        city_competitors[index] = max(1, len(seller_ids))

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
        utilization: dict[int, float] = {}
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
            utilization[index] = (
                float(stats["player_total_volume"] or 0) / max(1.0, float(stats["market_size"] or 0))
                if stats else 0.0
            )
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
        research_balance = max(0.0, float(bot["research_balance"] or 0))
        research_needed = max(0.0, research_goal - research_balance)
        research = 0.0

        rival_metrics: dict[int, list[dict[str, float]]] = {index: [] for index in range(len(markets))}
        if super_mode:
            rival_rows = all_rows(
                conn,
                "SELECT d.company_id,d.worker_delta,d.engineer_delta,d.management_investment,d.production_volume,"
                "d.quality_investment,cd.city,cd.agent_delta,cd.marketing_investment,cd.price,c.product_inventory "
                "FROM decisions d JOIN companies c ON c.id=d.company_id "
                "JOIN city_decisions cd ON cd.company_id=d.company_id AND cd.round_no=d.round_no "
                "WHERE d.round_no=? AND d.submitted_at IS NOT NULL AND d.company_id<>? AND c.is_super_bot=0",
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
                active_count = one(
                    conn,
                    "SELECT COUNT(*) AS n FROM city_decisions cd LEFT JOIN agents a "
                    "ON a.company_id=cd.company_id AND a.city=cd.city WHERE cd.company_id=? AND cd.round_no=? "
                    "AND COALESCE(a.count,0)+cd.agent_delta>0",
                    (rival["company_id"], round_no),
                )
                ma_index = float(rival["management_investment"] or 0) / max(1, workers + engineers)
                qi_denominator = float(rival["product_inventory"] or 0) * 1.2 + float(rival["production_volume"] or 0)
                qi_index = float(rival["quality_investment"] or 0) / max(1.0, qi_denominator)
                rival_metrics[index].append({
                    "ma": ma_index,
                    "qi": qi_index,
                    "mi_effective": float(rival["marketing_investment"] or 0) * (1.0 + agents * 0.10),
                    "price": float(rival["price"] or 0),
                    "agents": float(agents),
                    "available": (
                        float(rival["product_inventory"] or 0) + float(rival["production_volume"] or 0)
                    ) / max(1, int(active_count["n"] or 0)),
                })

            # Analyse all super bots against the same submitted-player snapshot.
            # Synthetic peers prevent an all-super match from assuming each bot
            # owns the whole market and avoid the sequential investment arms race
            # that previously bankrupted every later super bot.
            for other_row in bots:
                other = dict(other_row)
                other_id = int(other["id"])
                if other_id == company_id:
                    continue
                other_profile = int(other["bot_profile"] if other["bot_profile"] is not None else other_id) % 7
                other_style = BOT_STYLES[other_profile]
                other_variation = 0.94 + other_profile * 0.02
                other_selected = selected_by_bot[other_id]
                for index in other_selected:
                    market = markets[index]
                    agent_row = one(
                        conn, "SELECT count FROM agents WHERE company_id=? AND city=?",
                        (other_id, market["city"]),
                    )
                    other_agents = max(1, int(agent_row["count"] if agent_row else 0))
                    size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                    qi_large = float(market["max_price"]) / 50.0
                    mi_large = qi_large * size * 0.20 / 1.5 / 2.0
                    rival_metrics[index].append({
                        "ma": max(ma_threshold * 1.02, float(plan["ma"]) * other_variation) * float(other_style["ma"]),
                        "qi": qi_large * float(other_style["qi"]) if plan_index >= 1 else 0.0,
                        "mi_effective": mi_large * float(other_style["mi"]) if plan_index >= 2 else 0.0,
                        "price": min(price_max, float(market["max_price"])) * float(other_style["high"]),
                        "agents": float(other_agents),
                        "available": float(plan["production"]) * other_variation / max(1, len(other_selected)),
                    })

        current_pressure: dict[int, float] = {}
        for index in selected:
            market = markets[index]
            size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
            current_pressure[index] = sum(item.get("available", 0.0) for item in rival_metrics[index]) / max(1.0, size)
        market_pressure = max(
            (max(utilization.get(index, 0.0), current_pressure.get(index, 0.0)) for index in selected),
            default=0.0,
        )
        # QI is opened only when competition/volume justifies another 20% pool.
        # MI starts from round three, or earlier only in an already crowded city;
        # whenever it is opened its amount is at least the full large threshold.
        use_qi = market_pressure >= 0.55 or (plan_index >= 1 and profile in (2, 4, 6) and market_pressure >= 0.30)
        use_mi = plan_index >= 2
        mi_city_limit = min(len(selected), 1 + max(0, plan_index - 2) // 2)
        mi_priority = sorted(
            selected,
            key=lambda index: (
                str(markets[index]["city"]) == home,
                agent_plan[index][1],
                float(markets[index]["population"]) * float(markets[index]["penetration"]),
            ),
            reverse=True,
        )
        mi_selected = set(mi_priority[:mi_city_limit]) if use_mi else set()

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
                rival_ma = _upper_typical([item["ma"] for index in selected for item in rival_metrics[index]])
                ma_index = min(5000.0, max(ma_index, ma_threshold * 1.12, rival_ma * 1.18))
            management = ma_index * max(1, workers + engineers)
            denominator = old_products * 1.2 + production
            qi_line = max(float(markets[i]["max_price"]) / 50.0 for i in selected)
            qi_index = qi_line * max(1.03, float(style["qi"]))
            if super_mode:
                rival_qi = _upper_typical([item["qi"] for index in selected for item in rival_metrics[index]])
                qi_index = min(qi_line * 5.0, max(qi_index, qi_line * 1.12, rival_qi * 1.18))
            quality = qi_index * max(1.0, denominator) if use_qi else 0.0
            marketing: dict[int, float] = {}
            for index in selected:
                market = markets[index]
                active_agents = agent_plan[index][1]
                size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                threshold = (float(market["max_price"]) / 50.0) * size * 0.20
                threshold /= max(1.0, 1.0 + active_agents * 0.10) * 1.5 * 2.0
                target_mi = threshold * float(style["mi"])
                if super_mode:
                    rival_mi = _upper_typical([item["mi_effective"] for item in rival_metrics[index]])
                    target_mi = min(
                        threshold * 3.2,
                        max(target_mi, threshold * 1.20, rival_mi * 1.18 / (1.0 + active_agents * 0.10)),
                    )
                marketing[index] = target_mi if index in mi_selected and active_agents else 0.0
            total = staff_cost + material_cost + storage_cost + agent_cost + management + quality + sum(marketing.values())
            return {
                "worker_delta": worker_delta, "engineer_delta": engineer_delta,
                "workers": workers, "engineers": engineers, "management": management,
                "quality": quality, "marketing": marketing, "staff_cost": staff_cost,
                "agent_cost": agent_cost, "total": total,
            }

        last_round = official_round >= total_rounds
        if last_round:
            cash_use_ratio = 0.99
        elif market_pressure < 0.45:
            cash_use_ratio = 0.965 if super_mode else 0.985
        elif market_pressure < 0.60:
            cash_use_ratio = 0.84 if super_mode else 0.90
        else:
            # Once the market is crowded, cash is deliberately retained instead
            # of being converted into risky inventory. This is the Bot's basic
            # hold-production analysis, not an arbitrary fixed cash reserve.
            cash_use_ratio = 0.70 if super_mode else 0.74
        cash_budget = max(0.0, float(bot["cash"])) * cash_use_ratio

        # Never submit an MI plan that consumes the whole company before a
        # single product can be made. Postpone that line until the large
        # threshold and a minimal operating plan are both affordable.
        if use_mi and float(budget_for(0)["total"]) > cash_budget * 0.72:
            use_mi = False
            mi_selected = set()

        opened_investment_pools = 1 + int(use_qi) + int(use_mi)
        if market_pressure < 0.45:
            target_fraction = 0.60
        elif market_pressure < 0.60:
            target_fraction = min(0.66, 0.16 * opened_investment_pools + 0.10)
        else:
            target_fraction = min(0.68, 0.18 * opened_investment_pools + (0.18 if super_mode else 0.10))
        safe_market_units = 0.0
        for index in selected:
            market = markets[index]
            city_size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
            safe_market_units += city_size * target_fraction / city_competitors[index]
        target_available = int(safe_market_units * variation)
        if market_pressure < 0.45:
            target_available = max(target_available, desired_available)
        if prior_production and prior_total > 0:
            prior_sell_ratio = prior_sold / prior_total
            if prior_sell_ratio < 0.80:
                target_available = min(
                    target_available,
                    max(old_products, int(prior_sold * (1.04 + profile * 0.01) + old_products)),
                )
            elif prior_sell_ratio >= 0.95 and market_pressure < 0.45:
                # The market grew only one step; forecast that other competent
                # teams also expand after a sell-out instead of treating last
                # round's empty capacity as ours alone.
                expansion_factor = 1.65 + profile * 0.07
                target_available = min(
                    target_available,
                    max(desired_available, int(prior_sold * expansion_factor + old_products)),
                )
        production_limit = max(0, target_available - old_products)
        low, high = 0, production_limit
        while low < high:
            middle = (low + high + 1) // 2
            if budget_for(middle)["total"] <= cash_budget + 1e-9:
                low = middle
            else:
                high = middle - 1
        production = low
        budget = budget_for(production)

        def make_city_rows(aggressive: bool = False) -> list[dict[str, float | int | str]]:
            rows: list[dict[str, float | int | str]] = []
            active_sizes = [
                float(markets[index]["population"]) * float(markets[index]["penetration"])
                for index in selected if agent_plan[index][1] > 0
            ]
            non_home_share = 0.0
            if active_sizes:
                non_home_share = sum(
                    size for index, size in zip([i for i in selected if agent_plan[i][1] > 0], active_sizes)
                    if str(markets[index]["city"]) != home
                ) / sum(active_sizes)
            available_units = max(1, old_products + production)
            allocated_operating_cost = (
                float(budget["total"]) + available_units * transport_cost * non_home_share
            ) / available_units
            for index, market in enumerate(markets):
                delta, agents_after = agent_plan[index]
                marketing = budget["marketing"].get(index, 0.0)
                cap = min(price_max, float(market["max_price"]))
                city_pressure = max(utilization.get(index, 0.0), current_pressure.get(index, 0.0))
                reference = cap * float(style["high"])
                if index in selected and (city_pressure >= 0.60 or aggressive):
                    reference = previous_prices[index] - float(style["cut"])
                if super_mode and index in selected and (city_pressure >= 0.40 or aggressive):
                    rival_prices = [item["price"] for item in rival_metrics[index] if item["price"] > 0]
                    rival_floor = min(rival_prices, default=previous_prices[index])
                    factor = 0.955 - profile * 0.002 if aggressive else 0.988 - profile * 0.001
                    reference = min(previous_prices[index] - float(style["cut"]), rival_floor * factor)
                component_labor = component_need * worker_need * worker_hours / 504.0 * worker_salary * 3
                product_labor = engineer_need * engineer_hours / 504.0 * engineer_salary * 3
                direct_unit_cost = (
                    component_need * float(home_market["component_material"]) * material_factor
                    + float(home_market["product_material"]) * material_factor
                    + component_labor + product_labor
                    + (transport_cost if str(market["city"]) != home else 0.0)
                )
                margin = 1.12 if super_mode else 1.06
                reference = max(reference, direct_unit_cost * 1.12, allocated_operating_cost * margin)
                rows.append({
                    "index": index, "city": str(market["city"]), "agent_delta": delta,
                    "agents_after": agents_after, "marketing": marketing,
                    "price": min(cap, max(price_min, reference)),
                })
            return rows

        def forecast_super_capacity(rows: list[dict[str, float | int | str]]) -> float:
            if not super_mode or old_products + production <= 0:
                return 0.0
            capacity = 0.0
            ma_index = float(budget["management"]) / max(1, int(budget["workers"]) + int(budget["engineers"]))
            qi_index = float(budget["quality"]) / max(1.0, old_products * 1.2 + production)
            for row in rows:
                index = int(row["index"])
                if index not in selected or int(row["agents_after"]) <= 0:
                    continue
                entries: list[dict[str, float | int]] = []
                weighted_prices: list[tuple[float, float]] = []
                for rival_number, rival in enumerate(rival_metrics[index]):
                    rival_agents = max(1.0, rival.get("agents", 1.0))
                    entries.append({
                        "company_id": -(rival_number + 1), "ma_index": rival["ma"],
                        "qi_index": rival["qi"],
                        "mi_investment": rival["mi_effective"] / (1.0 + rival_agents * 0.10),
                        "price": rival["price"], "agents": rival_agents,
                    })
                    weighted_prices.append((rival["price"], max(1.0, rival.get("available", 1.0))))
                entries.append({
                    "company_id": company_id, "ma_index": ma_index, "qi_index": qi_index,
                    "mi_investment": float(row["marketing"]), "price": float(row["price"]),
                    "agents": int(row["agents_after"]),
                })
                weighted_prices.append((float(row["price"]), max(1.0, old_products + production)))
                weighted_total = sum(weight for _, weight in weighted_prices)
                average_price = sum(price * weight for price, weight in weighted_prices) / max(1.0, weighted_total)
                market = markets[index]
                size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                allocations = allocate_city_cpi(
                    entries, market_size=size, max_price=float(market["max_price"]),
                    ma_large_threshold=ma_threshold, average_price=average_price,
                    market_average_price=previous_prices[index],
                )
                mine = next(item for item in allocations if int(item["company_id"]) == company_id)
                capacity += size * float(mine["total_cpi"]) / 100.0
            return capacity

        city_rows = make_city_rows(False)
        if super_mode and old_products + production > 0:
            normal_capacity = forecast_super_capacity(city_rows)
            aggressive_rows = make_city_rows(True)
            aggressive_capacity = forecast_super_capacity(aggressive_rows)
            available_now = old_products + production
            def potential_revenue(rows: list[dict[str, float | int | str]], capacity: float) -> float:
                active = [row for row in rows if int(row["index"]) in selected and int(row["agents_after"]) > 0]
                total_weight = sum(
                    float(markets[int(row["index"])]["population"]) * float(markets[int(row["index"])]["penetration"])
                    for row in active
                )
                average = sum(
                    float(row["price"]) * float(markets[int(row["index"])]["population"]) * float(markets[int(row["index"])]["penetration"])
                    for row in active
                ) / max(1.0, total_weight)
                return min(float(available_now), capacity * 0.80) * average

            normal_revenue = potential_revenue(city_rows, normal_capacity)
            aggressive_revenue = potential_revenue(aggressive_rows, aggressive_capacity)
            chosen_aggressive = aggressive_revenue > normal_revenue * 1.03
            if chosen_aggressive:
                city_rows, normal_capacity = aggressive_rows, aggressive_capacity
            # Maintain a 25% capacity buffer. When the calculated CPI cannot
            # safely absorb the stock, reduce new production instead of gambling
            # the company on inventory that may not sell.
            safe_available = int(normal_capacity * 0.80)
            if safe_available < old_products + production:
                production = max(0, safe_available - old_products)
                budget = budget_for(production)
                city_rows = make_city_rows(chosen_aggressive)

        active_rows = [row for row in city_rows if int(row["agents_after"]) > 0 and int(row["index"]) in selected]
        weight_total = sum(
            float(markets[int(row["index"])]["population"]) * float(markets[int(row["index"])]["penetration"])
            for row in active_rows
        )
        average_sale_price = (
            sum(
                float(row["price"]) * float(markets[int(row["index"])]["population"]) * float(markets[int(row["index"])]["penetration"])
                for row in active_rows
            ) / max(1.0, weight_total)
        )
        prior_sell_ratio = prior_sold / prior_total if prior_production and prior_total > 0 else 0.95
        expected_sell_ratio = 0.95 if market_pressure < 0.45 else max(0.68, min(0.90, prior_sell_ratio))
        if super_mode:
            expected_sell_ratio = min(1.0, forecast_super_capacity(city_rows) / max(1, old_products + production))
            expected_sell_ratio = max(0.0, min(0.90, expected_sell_ratio))
        non_home_weight = sum(
            float(markets[int(row["index"])]["population"]) * float(markets[int(row["index"])]["penetration"])
            for row in active_rows if str(row["city"]) != home
        )
        projected_transport = (old_products + production) * expected_sell_ratio * transport_cost * non_home_weight / max(1.0, weight_total)
        projected_cash = (
            float(bot["cash"]) - float(budget["total"])
            + (old_products + production) * expected_sell_ratio * average_sale_price
            - projected_transport
        )
        operating_reserve = max(float(bot["cash"]) * 0.06, float(budget["staff_cost"]) * 0.50)
        research_ready = (
            not last_round and research_needed > 0
            and projected_cash - research_needed >= operating_reserve
            and (
                not super_mode
                or (
                    official_round >= 4
                    and float(bot["cash"]) >= research_goal * 4.0
                    and projected_cash - research_needed >= float(bot["cash"]) * 1.05
                )
            )
        )
        research = research_needed if research_ready else 0.0

        conn.execute(
            "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,"
            "engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, 0, budget["worker_delta"], worker_salary, budget["engineer_delta"],
             engineer_salary, budget["management"], production, budget["quality"], research, now_iso()),
        )
        for row in city_rows:
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                "VALUES(?,?,?,?,?,?,0)",
                (company_id, round_no, row["city"], row["agent_delta"], row["marketing"], row["price"]),
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
