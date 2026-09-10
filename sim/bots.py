from __future__ import annotations

import json
import hashlib
import math
import random
import sqlite3
import threading
import time
from typing import Any, Callable

from .cpi import allocate_city_cpi, allocate_city_cpi_for_company
from .db import all_rows, effective_employee_count, employee_count, get_setting, now_iso, one
from .engine import available_loan_limit, current_company_net_assets, loan_ceiling_for_round


BOT_API_VERSION = 13
_SUPER_BOT_SUBMISSION_LOCK = threading.Lock()

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
    candidates = [index for index in range(len(markets)) if index != home_index]
    seed_text = f"markets:{bot['id']}:{bot['code']}:{bot['bot_profile']}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    random.Random(seed).shuffle(candidates)
    return [home_index, *candidates[:max(0, min(len(markets), count) - 1)]]


def _bot_rng(bot: sqlite3.Row | dict[str, Any], round_no: int, super_mode: bool) -> random.Random:
    """Stable per-team/per-round randomness, including after a Streamlit rerun."""
    seed_text = f"decision:{bot['id']}:{bot['code']}:{round_no}:{int(super_mode)}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed)


def _upper_typical(values: list[float]) -> float:
    """Strong rival reference that ignores one irrational all-in outlier."""
    clean = sorted(max(0.0, float(value)) for value in values)
    if not clean:
        return 0.0
    return clean[min(len(clean) - 1, math.floor((len(clean) - 1) * 0.80))]


def _balanced_production_group(
    component_workers: float,
    component_hours: float,
    product_engineers: float,
    product_hours: float,
    components_per_product: float,
) -> dict[str, float]:
    """Build the F/G/H/I production group defined in the strategy guide."""
    a = max(float(component_workers), 1e-9)
    b = max(float(component_hours), 1e-9)
    c = max(float(product_engineers), 1e-9)
    d = max(float(product_hours), 1e-9)
    e = max(float(components_per_product), 1e-9)
    worker_side = a * b * e
    engineer_side = c * d
    if all(abs(value - round(value)) < 1e-9 for value in (worker_side, engineer_side)):
        divisor = math.gcd(max(1, round(worker_side)), max(1, round(engineer_side)))
        workers = worker_side / divisor
        engineers = engineer_side / divisor
        components = 504.0 / b * workers / a
        products = components / e
    else:
        # A one-product normalized group keeps the same F:G ratio for decimal KDS values.
        products = 1.0
        components = e
        workers = a * b * components / 504.0
        engineers = c * d * products / 504.0
    return {
        "workers": workers,
        "engineers": engineers,
        "components": components,
        "products": products,
    }


def _affordable_group_count(
    available_cash: float,
    fixed_agent_and_mi: float,
    complete_group_cost: float,
    demand_groups: int,
) -> int:
    """Shared normal/super Bot group formula: floor((cash-fixed)/group cost)."""
    cash_after_fixed = max(0.0, float(available_cash) - max(0.0, float(fixed_agent_and_mi)))
    affordable = math.floor(cash_after_fixed / max(float(complete_group_cost), 1.0))
    return max(0, min(int(affordable), max(0, int(demand_groups))))


def _weighted_average(pairs: list[tuple[float, float]], fallback: float) -> float:
    total = sum(max(0.0, weight) for _, weight in pairs)
    if total <= 0:
        return float(fallback)
    return sum(value * max(0.0, weight) for value, weight in pairs) / total


def _all_markets_near_capacity(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
    threshold: float = 0.95,
) -> bool:
    """Cash may be held only after every market is effectively sold out."""
    if round_no <= 1 or not markets:
        return False
    for market in markets:
        stats = one(
            conn,
            "SELECT player_total_volume,market_size FROM market_round_stats WHERE city=? "
            "AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (market["city"], round_no),
        )
        if not stats:
            return False
        utilization = float(stats["player_total_volume"] or 0) / max(1.0, float(stats["market_size"] or 0))
        if utilization + 1e-9 < threshold:
            return False
    return True


def _forecast_submitted_cpi_capacity(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> dict[int, float]:
    """Re-run CPI against the final submitted field and return unit capacity.

    This intentionally happens after every Super Bot has selected a candidate.
    Candidate-by-candidate forecasts cannot know the final choices of the other
    Super Bots and may otherwise buy several times more CPI than their stock.
    """
    official_round = 1 if round_no < 0 else round_no
    growth = float(get_setting(conn, "market_growth", 1.10))
    ma_threshold = float(get_setting(conn, "cpi_ma_large_threshold", 1300))
    price_power = max(1, int(get_setting(conn, "cpi_price_power", 8)))
    states: dict[int, dict[str, Any]] = {}
    for row in all_rows(
        conn,
        "SELECT c.id,c.product_inventory,d.worker_delta,d.engineer_delta,d.management_investment,"
        "d.production_volume,d.quality_investment FROM companies c JOIN decisions d "
        "ON d.company_id=c.id WHERE d.round_no=? AND d.submitted_at IS NOT NULL",
        (round_no,),
    ):
        company_id = int(row["id"])
        workers = max(0, employee_count(conn, company_id, "worker") + int(row["worker_delta"] or 0))
        engineers = max(0, employee_count(conn, company_id, "engineer") + int(row["engineer_delta"] or 0))
        production = max(0, int(row["production_volume"] or 0))
        old_products = max(0, int(row["product_inventory"] or 0))
        states[company_id] = {
            "available": old_products + production,
            "ma": float(row["management_investment"] or 0) / max(1, workers + engineers),
            "qi": float(row["quality_investment"] or 0) / max(1.0, old_products * 1.2 + production),
            "cities": {},
        }
    if not states:
        return {}

    base_averages: dict[str, float] = {}
    market_sizes: dict[str, float] = {}
    for market in markets:
        city = str(market["city"])
        previous = one(
            conn,
            "SELECT average_price FROM market_round_stats WHERE city=? AND round_no>=1 "
            "AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (city, max(1, round_no)),
        )
        base_averages[city] = (
            float(previous["average_price"])
            if previous else float(market["initial_avg_price"])
        )
        market_sizes[city] = (
            float(market["population"])
            * float(market["penetration"])
            * growth ** max(0, official_round - 1)
        )
        for row in all_rows(
            conn,
            "SELECT cd.company_id,cd.agent_delta,cd.marketing_investment,cd.price,COALESCE(a.count,0) AS current_agents "
            "FROM city_decisions cd LEFT JOIN agents a ON a.company_id=cd.company_id AND a.city=cd.city "
            "WHERE cd.round_no=? AND cd.city=?",
            (round_no, city),
        ):
            company_id = int(row["company_id"])
            if company_id not in states:
                continue
            agents = max(0, int(row["current_agents"] or 0) + int(row["agent_delta"] or 0))
            if agents > 0:
                states[company_id]["cities"][city] = {
                    "agents": agents,
                    "marketing": float(row["marketing_investment"] or 0),
                    "price": float(row["price"] or 0),
                }

    player_averages = {
        city: _weighted_average(
            [
                (float(state["cities"][city]["price"]), max(1.0, float(state["available"])))
                for state in states.values() if city in state["cities"]
            ],
            base_averages[city],
        )
        for city in base_averages
    }
    capacities: dict[int, dict[str, float]] = {company_id: {} for company_id in states}
    for _ in range(12):
        capacities = {company_id: {} for company_id in states}
        for market in markets:
            city = str(market["city"])
            entries = [
                {
                    "company_id": company_id,
                    "ma_index": state["ma"],
                    "qi_index": state["qi"],
                    "mi_investment": state["cities"][city]["marketing"],
                    "price": state["cities"][city]["price"],
                    "agents": state["cities"][city]["agents"],
                }
                for company_id, state in states.items() if city in state["cities"]
            ]
            for allocation in allocate_city_cpi(
                entries,
                market_size=market_sizes[city],
                max_price=float(market["max_price"]),
                ma_large_threshold=ma_threshold,
                price_power=price_power,
                average_price=player_averages[city],
                market_average_price=base_averages[city],
            ):
                capacities[int(allocation["company_id"])][city] = (
                    market_sizes[city] * float(allocation["total_cpi"]) / 100.0
                )
        next_averages: dict[str, float] = {}
        for city in base_averages:
            sold_pairs: list[tuple[float, float]] = []
            for company_id, state in states.items():
                if city not in state["cities"]:
                    continue
                total_capacity = sum(capacities[company_id].values())
                factor = min(1.0, float(state["available"]) / total_capacity) if total_capacity > 0 else 0.0
                sold_pairs.append((
                    float(state["cities"][city]["price"]),
                    capacities[company_id].get(city, 0.0) * factor,
                ))
            next_averages[city] = _weighted_average(sold_pairs, player_averages[city])
        if all(math.isclose(next_averages[city], player_averages[city], abs_tol=0.01) for city in player_averages):
            break
        player_averages = next_averages
    return {
        company_id: sum(city_values.values())
        for company_id, city_values in capacities.items()
    }


def _rebalance_super_bot_production(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> None:
    """Use full-group cash, then taper only genuinely surplus CPI spending."""
    worker_need = float(get_setting(conn, "component_workers", 3))
    worker_hours = float(get_setting(conn, "component_hours", 7))
    engineer_need = float(get_setting(conn, "product_engineers", 4))
    engineer_hours = float(get_setting(conn, "product_hours", 14))
    component_need = max(1, int(round(get_setting(conn, "components_per_product", 7))))
    group = _balanced_production_group(
        worker_need, worker_hours, engineer_need, engineer_hours, component_need,
    )
    worker_training = float(get_setting(conn, "worker_training_cost", 0))
    engineer_training = float(get_setting(conn, "engineer_training_cost", 0))
    add_agent_cost = float(get_setting(conn, "agent_add_cost", 300000))
    remove_agent_cost = float(get_setting(conn, "agent_remove_cost", 100000))
    patent_factor = float(get_setting(conn, "patent_factor", 0.70))
    growth = float(get_setting(conn, "market_growth", 1.10))
    official_round = 1 if round_no < 0 else round_no

    market_by_city = {str(market["city"]): market for market in markets}
    all_markets_full = _all_markets_near_capacity(conn, round_no, markets)

    # Deploy idle capital before the joint CPI/production convergence. Only a
    # fraction of the gap is assigned to indices; the rest stays available to
    # build the complete production groups unlocked by that extra CPI.
    for row in ([] if all_markets_full else all_rows(
        conn,
        "SELECT c.*,d.loan_change,d.worker_salary,d.engineer_salary,d.worker_delta,d.engineer_delta,"
        "d.management_investment,d.production_volume,d.quality_investment FROM companies c "
        "JOIN decisions d ON d.company_id=c.id WHERE c.is_super_bot=1 AND d.round_no=?",
        (round_no,),
    )):
        company_id = int(row["id"])
        home = market_by_city.get(str(row["home_city"]), markets[0])
        production = max(0, int(row["production_volume"] or 0))
        old_products = max(0, int(row["product_inventory"] or 0))
        old_components = max(0, int(row["component_inventory"] or 0))
        workers = max(0, employee_count(conn, company_id, "worker") + int(row["worker_delta"] or 0))
        engineers = max(0, employee_count(conn, company_id, "engineer") + int(row["engineer_delta"] or 0))
        components_to_make = max(0, production * component_need - old_components)
        staff_cost = workers * float(row["worker_salary"]) * 3 + engineers * float(row["engineer_salary"]) * 3
        staff_cost += max(0, int(row["worker_delta"] or 0)) * worker_training
        staff_cost += max(0, int(row["engineer_delta"] or 0)) * engineer_training
        staff_cost += max(0, -int(row["worker_delta"] or 0)) * float(row["worker_salary"])
        staff_cost += max(0, -int(row["engineer_delta"] or 0)) * float(row["engineer_salary"])
        materials = components_to_make * float(home["component_material"]) * (patent_factor ** int(row["patents"] or 0))
        materials += production * float(home["product_material"]) * (patent_factor ** int(row["patents"] or 0))
        storage = max(
            0, old_components + components_to_make - int(row["component_storage_capacity"] or 0),
        ) * float(home["component_storage"])
        storage += max(
            0, old_products + production - int(row["product_storage_capacity"] or 0),
        ) * float(home["product_storage"])
        city_rows = [dict(city) for city in all_rows(
            conn,
            "SELECT cd.*,COALESCE(a.count,0) AS current_agents,m.population,m.penetration,m.max_price "
            "FROM city_decisions cd JOIN market_config m ON m.city=cd.city LEFT JOIN agents a "
            "ON a.company_id=cd.company_id AND a.city=cd.city "
            "WHERE cd.company_id=? AND cd.round_no=?",
            (company_id, round_no),
        )]
        agent_cost = sum(
            int(city["agent_delta"]) * add_agent_cost
            if int(city["agent_delta"]) > 0
            else -int(city["agent_delta"]) * remove_agent_cost
            for city in city_rows
        )
        marketing = sum(float(city["marketing_investment"] or 0) for city in city_rows)
        current_spend = (
            staff_cost + materials + storage + agent_cost + marketing
            + float(row["management_investment"] or 0)
            + float(row["quality_investment"] or 0)
        )
        active_cities = [
            city for city in city_rows
            if int(city["current_agents"] or 0) + int(city["agent_delta"] or 0) > 0
        ]
        utilizations: list[float] = []
        for city in active_cities:
            stats = one(
                conn,
                "SELECT player_total_volume,market_size FROM market_round_stats WHERE city=? "
                "AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
                (city["city"], max(1, round_no)),
            )
            utilizations.append(
                float(stats["player_total_volume"] or 0) / max(1.0, float(stats["market_size"] or 0))
                if stats else 0.0
            )
        market_is_saturated = bool(utilizations) and sum(utilizations) / len(utilizations) >= 0.60
        total_funds = float(row["cash"]) + max(0.0, float(row["loan_change"] or 0))
        target_spend_ratio = 0.72 if market_is_saturated else 0.94
        spend_gap = max(0.0, total_funds * target_spend_ratio - current_spend)
        investment_budget = spend_gap * (0.15 if market_is_saturated else 0.45)
        if investment_budget <= 1:
            continue

        headcount = max(1, workers + engineers)
        qi_denominator = max(1.0, old_products * 1.2 + production)
        ma_headroom = max(0.0, 8000.0 * headcount - float(row["management_investment"] or 0))
        qi_headroom = max(0.0, 3000.0 * qi_denominator - float(row["quality_investment"] or 0))
        mi_headrooms: dict[str, float] = {}
        for city in active_cities:
            agents = int(city["current_agents"] or 0) + int(city["agent_delta"] or 0)
            size = float(city["population"]) * float(city["penetration"]) * growth ** max(0, official_round - 1)
            threshold = (float(city["max_price"]) / 50.0) * size * 0.20
            threshold /= max(1.0, 1.0 + agents * 0.10) * 1.5 * 2.0
            mi_headrooms[str(city["city"])] = max(
                0.0,
                threshold * 6.0 - float(city["marketing_investment"] or 0),
            )
        total_headroom = ma_headroom + qi_headroom + sum(mi_headrooms.values())
        deployed = min(investment_budget, total_headroom)
        if deployed <= 1 or total_headroom <= 0:
            continue
        conn.execute(
            "UPDATE decisions SET management_investment=management_investment+?,"
            "quality_investment=quality_investment+? WHERE company_id=? AND round_no=?",
            (
                deployed * ma_headroom / total_headroom,
                deployed * qi_headroom / total_headroom,
                company_id,
                round_no,
            ),
        )
        for city, headroom in mi_headrooms.items():
            conn.execute(
                "UPDATE city_decisions SET marketing_investment=marketing_investment+? "
                "WHERE company_id=? AND round_no=? AND city=?",
                (deployed * headroom / total_headroom, company_id, round_no, city),
            )
        conn.commit()

    for _ in range(20):
        capacities = (
            _forecast_submitted_cpi_capacity(conn, round_no, markets)
            if all_markets_full else {}
        )
        changed = False
        for row in all_rows(
            conn,
            "SELECT c.*,d.loan_change,d.worker_salary,d.engineer_salary,d.worker_delta,d.engineer_delta,"
            "d.management_investment,d.production_volume,d.quality_investment FROM companies c "
            "JOIN decisions d ON d.company_id=c.id WHERE c.is_super_bot=1 AND d.round_no=?",
            (round_no,),
        ):
            company_id = int(row["id"])
            home = market_by_city.get(str(row["home_city"]), markets[0])
            old_products = max(0, int(row["product_inventory"] or 0))
            old_components = max(0, int(row["component_inventory"] or 0))
            current_production = max(0, int(row["production_volume"] or 0))
            current_workers = max(0, employee_count(conn, company_id, "worker") + int(row["worker_delta"] or 0))
            current_engineers = max(0, employee_count(conn, company_id, "engineer") + int(row["engineer_delta"] or 0))
            ma_index = float(row["management_investment"] or 0) / max(1, current_workers + current_engineers)
            qi_index = float(row["quality_investment"] or 0) / max(1.0, old_products * 1.2 + current_production)
            material_factor = patent_factor ** int(row["patents"] or 0)
            city_cost = one(
                conn,
                "SELECT COALESCE(SUM(CASE WHEN cd.agent_delta>0 THEN cd.agent_delta*? "
                "WHEN cd.agent_delta<0 THEN -cd.agent_delta*? ELSE 0 END),0) AS agent_cost,"
                "COALESCE(SUM(cd.marketing_investment),0) AS marketing FROM city_decisions cd "
                "WHERE cd.company_id=? AND cd.round_no=?",
                (add_agent_cost, remove_agent_cost, company_id, round_no),
            )
            fixed_cost = float(city_cost["agent_cost"] or 0) + float(city_cost["marketing"] or 0)

            def plan_for(groups: int) -> dict[str, float | int]:
                production = max(0, int(math.floor(groups * group["products"])))
                components_to_make = max(0, production * component_need - old_components)
                worker_required = components_to_make * worker_need * worker_hours / 504.0
                engineer_required = production * engineer_need * engineer_hours / 504.0
                worker_delta = _staff_delta(conn, company_id, "worker", round_no, worker_required)
                engineer_delta = _staff_delta(conn, company_id, "engineer", round_no, engineer_required)
                workers = max(0, employee_count(conn, company_id, "worker") + worker_delta)
                engineers = max(0, employee_count(conn, company_id, "engineer") + engineer_delta)
                staff = workers * float(row["worker_salary"]) * 3 + engineers * float(row["engineer_salary"]) * 3
                staff += max(0, worker_delta) * worker_training + max(0, engineer_delta) * engineer_training
                staff += max(0, -worker_delta) * float(row["worker_salary"])
                staff += max(0, -engineer_delta) * float(row["engineer_salary"])
                materials = components_to_make * float(home["component_material"]) * material_factor
                materials += production * float(home["product_material"]) * material_factor
                storage = max(
                    0,
                    old_components + components_to_make - int(row["component_storage_capacity"] or 0),
                ) * float(home["component_storage"])
                storage += max(
                    0,
                    old_products + production - int(row["product_storage_capacity"] or 0),
                ) * float(home["product_storage"])
                management = ma_index * max(1, workers + engineers)
                quality = qi_index * max(1.0, old_products * 1.2 + production)
                return {
                    "production": production,
                    "worker_delta": worker_delta,
                    "engineer_delta": engineer_delta,
                    "management": management,
                    "quality": quality,
                    "total": fixed_cost + staff + materials + storage + management + quality,
                }

            total_funds = float(row["cash"]) + max(0.0, float(row["loan_change"] or 0))
            if all_markets_full:
                target_new = max(0.0, capacities.get(company_id, 0.0) - old_products)
                target_groups = max(0, int(round(target_new / max(group["products"], 1.0))))
                low, high = 0, target_groups
            else:
                current_groups = max(0, int(round(current_production / max(group["products"], 1.0))))
                low, high = 0, max(1, current_groups)
                while float(plan_for(high)["total"]) <= total_funds + 1e-9:
                    low = high
                    high *= 2
            while low < high:
                middle = (low + high + 1) // 2
                if float(plan_for(middle)["total"]) <= total_funds + 1e-9:
                    low = middle
                else:
                    high = middle - 1
            selected_plan = plan_for(low)
            if int(selected_plan["production"]) == current_production:
                continue
            conn.execute(
                "UPDATE decisions SET worker_delta=?,engineer_delta=?,management_investment=?,"
                "production_volume=?,quality_investment=? WHERE company_id=? AND round_no=?",
                (
                    selected_plan["worker_delta"], selected_plan["engineer_delta"],
                    selected_plan["management"], selected_plan["production"],
                    selected_plan["quality"], company_id, round_no,
                ),
            )
            conn.commit()
            changed = True
        # Recalculate against the production changes above. If a Super Bot is
        # still buying materially more CPI units than it can stock, taper all
        # three investment indices together. Damping avoids a threshold cliff;
        # later passes let rivals absorb the released CPI before adjusting
        # again. MA/QI keep their documented minimum index of 1.
        capacities = _forecast_submitted_cpi_capacity(conn, round_no, markets)
        for row in all_rows(
            conn,
            "SELECT c.id,c.product_inventory,d.worker_delta,d.engineer_delta,d.management_investment,"
            "d.production_volume,d.quality_investment FROM companies c JOIN decisions d "
            "ON d.company_id=c.id WHERE c.is_super_bot=1 AND d.round_no=?",
            (round_no,),
        ):
            company_id = int(row["id"])
            available = max(0.0, float(row["product_inventory"] or 0) + float(row["production_volume"] or 0))
            capacity = capacities.get(company_id, 0.0)
            if available <= 0 or capacity <= available * 1.06:
                continue
            coverage = capacity / available
            scale = max(0.18, min(0.96, math.sqrt(1.0 / coverage)))
            workers = max(0, employee_count(conn, company_id, "worker") + int(row["worker_delta"] or 0))
            engineers = max(0, employee_count(conn, company_id, "engineer") + int(row["engineer_delta"] or 0))
            headcount = max(1, workers + engineers)
            ma_index = float(row["management_investment"] or 0) / headcount
            qi_denominator = max(
                1.0,
                float(row["product_inventory"] or 0) * 1.2 + float(row["production_volume"] or 0),
            )
            qi_index = float(row["quality_investment"] or 0) / qi_denominator
            conn.execute(
                "UPDATE decisions SET management_investment=?,quality_investment=? "
                "WHERE company_id=? AND round_no=?",
                (
                    max(1.0, ma_index * scale) * headcount,
                    max(1.0, qi_index * scale) * qi_denominator,
                    company_id,
                    round_no,
                ),
            )
            conn.execute(
                "UPDATE city_decisions SET marketing_investment=marketing_investment*? "
                "WHERE company_id=? AND round_no=? AND marketing_investment>0",
                (scale, company_id, round_no),
            )
            conn.commit()
            changed = True
        if not changed:
            break


def _submit_bots(
    conn: sqlite3.Connection,
    round_no: int,
    super_mode: bool,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    bots = all_rows(
        conn,
        "SELECT * FROM companies WHERE is_bot=1 AND is_super_bot=? ORDER BY id",
        (int(super_mode),),
    )
    markets = [dict(row) for row in all_rows(conn, "SELECT * FROM market_config ORDER BY city")]
    if not bots or not markets:
        return 0

    official_round = 1 if round_no < 0 else round_no
    all_markets_full = _all_markets_near_capacity(conn, round_no, markets)
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
    loan_threshold = setting("loan_asset_threshold", 15_000_000)
    global_max_loan = setting("global_max_loan", 10_000_000)
    research_goal = setting("research_75", 6000000) * setting("research_hidden_threshold_multiplier", 4 / 3)
    research_goal += setting("research_buffer", 150000)

    human_present = bool(one(conn, "SELECT 1 FROM companies WHERE is_bot=0 LIMIT 1"))
    previous_leader: dict[str, Any] | None = None
    if super_mode and human_present and official_round > 1:
        leader_row = one(
            conn,
            "SELECT r.company_id,r.ma_index,r.qi_index FROM results r "
            "WHERE r.round_no=? ORDER BY r.net_assets DESC,r.company_id LIMIT 1",
            (official_round - 1,),
        )
        if leader_row:
            previous_leader = {
                "ma": float(leader_row["ma_index"] or 0),
                "qi": float(leader_row["qi_index"] or 0),
                "cities": {
                    str(row["city"]): {
                        "marketing": float(row["marketing_investment"] or 0),
                        "price": float(row["price"] or 0),
                    }
                    for row in all_rows(
                        conn,
                        "SELECT city,marketing_investment,price FROM city_decisions "
                        "WHERE company_id=? AND round_no=?",
                        (leader_row["company_id"], official_round - 1),
                    )
                },
            }

    selected_by_bot: dict[int, list[int]] = {}
    for bot in bots:
        wealth_multiple = max(1.0, float(bot["cash"]) / initial_cash)
        # Profitable Bots must deploy their growing capital by opening more
        # real sales capacity. A slow, fixed three-city wealth cap was the main
        # reason late-round Bots kept hundreds of millions idle.
        wealth_expansion = min(
            len(markets),
            max(0, int(math.ceil(math.log2(wealth_multiple)))),
        )
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
    pending_total = sum(
        1 for bot in bots
        if not one(
            conn,
            "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?",
            (int(bot["id"]), round_no),
        )
    )
    progress_total = pending_total + (1 if super_mode and pending_total else 0)
    submitted_super_ids = {
        int(row["company_id"])
        for row in all_rows(
            conn,
            "SELECT d.company_id FROM decisions d JOIN companies c ON c.id=d.company_id "
            "WHERE d.round_no=? AND d.submitted_at IS NOT NULL AND c.is_super_bot=1",
            (round_no,),
        )
    } if super_mode else set()
    for bot_row in bots:
        bot = dict(bot_row)
        company_id = int(bot["id"])
        if one(conn, "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?", (company_id, round_no)):
            continue
        profile = int(bot["bot_profile"] if bot["bot_profile"] is not None else company_id) % 7
        style = BOT_STYLES[profile]
        rng = _bot_rng(bot, round_no, super_mode)
        variation = rng.uniform(0.88, 1.14)
        ma_strength = min(4.25, max(0.62, float(style["ma"]) * rng.uniform(0.84, 1.20)))
        qi_strength = min(3.40, max(0.78, float(style["qi"]) * rng.uniform(0.86, 1.18)))
        mi_strength = min(3.60, max(1.03, float(style["mi"]) * rng.uniform(0.88, 1.16)))
        high_price_ratio = min(0.982, max(0.925, float(style["high"]) + rng.uniform(-0.014, 0.014)))
        price_cut = max(50.0, float(style["cut"]) * rng.uniform(0.72, 1.32))
        ma_round_buffer = rng.uniform(0.96, 1.10)
        super_aggression = rng.uniform(1.12, 1.34)
        super_mi_cap = rng.uniform(2.80, 3.60)
        normal_price_factor = rng.uniform(0.978, 0.994)
        aggressive_price_factor = rng.uniform(0.935, 0.968)
        home = str(bot["home_city"] or markets[profile % len(markets)]["city"])
        home_market = next((market for market in markets if market["city"] == home), markets[0])
        loan_change = 0.0
        if super_mode:
            loan_ceiling = loan_ceiling_for_round(round_no, home_market, global_max_loan)
            loan_change = available_loan_limit(
                current_company_net_assets(conn, bot),
                loan_threshold,
                float(home_market.get("min_loan", 0.0)),
                loan_ceiling,
            )
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
            rng.uniform(0.0, 6.0), salary_min, salary_max, salary_change,
        )
        engineer_salary, engineer_multiplier = _bot_salary(
            report, "engineer", float(home_market["engineer_initial_salary"]), previous_engineer,
            rng.uniform(0.0, 6.0), salary_min, salary_max, salary_change,
        )

        selected = selected_by_bot[company_id]

        agent_plan: dict[int, tuple[int, int]] = {}
        agent_cost = 0.0
        for index, market in enumerate(markets):
            row = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company_id, market["city"]))
            current = int(row["count"] if row else 0)
            agent_variation = rng.choice((-1, 0, 0, 0, 1))
            desired = max(1, int(plan["agents"]) + agent_variation) if index in selected else current
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
        wealth_multiple = max(1.0, float(bot["cash"]) / initial_cash)
        unsaturated_share = (
            sum(1 for index in selected if not saturated.get(index, False)) / max(1, len(selected))
        )
        # Fixed seven-round volumes are only the starting curve. When prior
        # markets still have room, profitable ordinary Bots compound production
        # with their capital instead of repeating the same small batch forever.
        capital_scale = 1.0 + (
            min(8.0, math.sqrt(wealth_multiple)) - 1.0
        ) * unsaturated_share
        desired_available = int(float(plan["production"]) * variation * capital_scale)
        prior_production = report.get("production", {})
        prior_sold = 0
        prior_total = 0
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

            # Super Bots are analysed and committed one at a time. Decisions
            # already saved in this pass are real rivals above; only the smaller
            # unresolved remainder still needs a synthetic estimate.
            for other_row in bots:
                other = dict(other_row)
                other_id = int(other["id"])
                if other_id == company_id or other_id in submitted_super_ids:
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
        if super_mode:
            # Super Bots evaluate the full QI range; QI=1 is the effective
            # "closed" candidate and therefore costs almost nothing per unit.
            use_qi = True
        use_mi = plan_index >= 2
        mi_city_limit = min(
            len(selected),
            1 + max(0, plan_index - 2) // 2 + (1 if plan_index >= 4 and rng.random() > 0.55 else 0),
        )
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

        # Choose the CPI indices first. The affordable production count is then
        # derived from the strategy guide's complete group cost; investment is
        # never used as a blind cash sink after production has been calculated.
        ma_index_target = max(ma_threshold * ma_round_buffer, float(plan["ma"]) * variation) * ma_strength
        if super_mode:
            rival_ma = _upper_typical([item["ma"] for index in selected for item in rival_metrics[index]])
            ma_index_target = min(
                5000.0,
                max(ma_index_target, ma_threshold * super_aggression, rival_ma * super_aggression),
            )
        qi_line = max(float(markets[index]["max_price"]) / 50.0 for index in selected)
        qi_index_target = qi_line * max(1.03, qi_strength)
        if super_mode:
            rival_qi = _upper_typical([item["qi"] for index in selected for item in rival_metrics[index]])
            qi_index_target = min(
                qi_line * 5.0,
                max(qi_index_target, qi_line * super_aggression, rival_qi * super_aggression),
            )
        marketing_targets: dict[int, float] = {}
        mi_thresholds: dict[int, float] = {}
        for index in selected:
            market = markets[index]
            active_agents = agent_plan[index][1]
            size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
            threshold = (float(market["max_price"]) / 50.0) * size * 0.20
            threshold /= max(1.0, 1.0 + active_agents * 0.10) * 1.5 * 2.0
            mi_thresholds[index] = threshold
            target_mi = threshold * mi_strength
            if super_mode:
                rival_mi = _upper_typical([item["mi_effective"] for item in rival_metrics[index]])
                target_mi = min(
                    threshold * super_mi_cap,
                    max(
                        target_mi,
                        threshold * super_aggression,
                        rival_mi * super_aggression / (1.0 + active_agents * 0.10),
                    ),
                )
            marketing_targets[index] = target_mi if index in mi_selected and active_agents else 0.0

        group = _balanced_production_group(worker_need, worker_hours, engineer_need, engineer_hours, component_need)
        group_cost = (
            group["workers"] * worker_salary * 3
            + group["engineers"] * engineer_salary * 3
            + group["workers"] * worker_training
            + group["engineers"] * engineer_training
            + group["components"]
            * (float(home_market["component_material"]) * material_factor + float(home_market["component_storage"]))
            + group["products"]
            * (float(home_market["product_material"]) * material_factor + float(home_market["product_storage"]))
            + ma_index_target * (group["workers"] + group["engineers"])
            + (qi_index_target * group["products"] if use_qi else 0.0)
        )

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
            management = ma_index_target * max(1, workers + engineers)
            denominator = old_products * 1.2 + production
            quality = qi_index_target * max(1.0, denominator) if use_qi else 0.0
            marketing = dict(marketing_targets)
            total = staff_cost + material_cost + storage_cost + agent_cost + management + quality + sum(marketing.values())
            return {
                "worker_delta": worker_delta, "engineer_delta": engineer_delta,
                "workers": workers, "engineers": engineers, "management": management,
                "quality": quality, "marketing": marketing, "staff_cost": staff_cost,
                "agent_cost": agent_cost,
                "core_total": staff_cost + material_cost + storage_cost + agent_cost,
                "total": total,
            }

        last_round = official_round >= total_rounds
        # For strategy construction the new loan is simply additional usable
        # capital. Existing debt is deliberately not subtracted a second time;
        # settlement still records debt and interest normally.
        cash_budget = max(0.0, float(bot["cash"]) + loan_change)

        # Never submit an MI plan that consumes the whole company before a
        # single product can be made. Postpone that line until the large
        # threshold and a minimal operating plan are both affordable.
        if use_mi and float(budget_for(0)["total"]) > cash_budget * 0.72:
            use_mi = False
            mi_selected = set()
            marketing_targets = {index: 0.0 for index in marketing_targets}

        opened_investment_pools = 1 + int(use_qi) + int(use_mi)
        if market_pressure < 0.45:
            target_fraction = rng.uniform(0.54, 0.66)
        elif market_pressure < 0.60:
            target_fraction = min(0.68, (0.16 * opened_investment_pools + 0.10) * rng.uniform(0.90, 1.10))
        else:
            target_fraction = min(
                0.70,
                (0.18 * opened_investment_pools + (0.18 if super_mode else 0.10)) * rng.uniform(0.88, 1.08),
            )
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
                expansion_factor = rng.uniform(1.55, 2.12)
                target_available = min(
                    target_available,
                    max(desired_available, int(prior_sold * expansion_factor + old_products)),
                )
        if not prior_production and old_products >= desired_available:
            # Existing inventory already covers the plan: control stock and
            # release surplus staff instead of treating new city capacity as a
            # reason to manufacture another batch immediately.
            target_available = old_products
        demand_target = max(0, target_available - old_products)
        fixed_before_groups = agent_cost + sum(marketing_targets.values())
        # b = floor((cash - Agent - MI) / complete group cost).  A group
        # already contains payroll, component/product costs, MA and QI, so all
        # five outputs stay in the exact F:G:H:I ratio supplied by the KDS.
        demand_groups = (
            math.ceil(demand_target / max(group["products"], 1.0))
            if demand_target > 0 else 0
        )
        # Hard cash rule supplied by the organiser: after Agent and MI, every
        # Bot buys the maximum number of complete groups. The only exception is
        # when every market was at least 95% full in the previous round.
        formula_groups = math.floor(
            max(0.0, cash_budget - fixed_before_groups) / max(group_cost, 1.0)
        )
        low_groups = 0
        if all_markets_full:
            high_groups = max(0, demand_groups)
        else:
            high_groups = max(1, demand_groups, formula_groups)
            while budget_for(int(math.floor(high_groups * group["products"])))["total"] <= cash_budget + 1e-9:
                low_groups = high_groups
                high_groups *= 2
        while low_groups < high_groups:
            middle_groups = (low_groups + high_groups + 1) // 2
            middle_products = int(math.floor(middle_groups * group["products"]))
            if budget_for(middle_products)["total"] <= cash_budget + 1e-9:
                low_groups = middle_groups
            else:
                high_groups = middle_groups - 1
        production = int(math.floor(low_groups * group["products"]))
        budget = budget_for(production)
        inventory_heavy = (
            old_products > max(20, prior_sold * 0.20)
            or (prior_production and prior_total > 0 and prior_sold / prior_total < 0.90)
            or old_products + production > demand_target
        )

        def make_city_rows(
            aggressive: bool = False,
            price_ratio_override: float | None = None,
        ) -> list[dict[str, float | int | str]]:
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
                float(budget["core_total"]) + available_units * transport_cost * non_home_share
            ) / available_units
            for index, market in enumerate(markets):
                delta, agents_after = agent_plan[index]
                marketing = budget["marketing"].get(index, 0.0)
                cap = min(price_max, float(market["max_price"]))
                city_pressure = max(utilization.get(index, 0.0), current_pressure.get(index, 0.0))
                previous_price = previous_prices.get(index, float(market["initial_avg_price"]))
                low_price_unlocked = previous_price >= cap * 0.75
                reference = cap * high_price_ratio
                if price_ratio_override is not None and index in selected:
                    reference = cap * (
                        price_ratio_override
                        if low_price_unlocked or price_ratio_override >= 0.75
                        else high_price_ratio
                    )
                if (
                    price_ratio_override is None
                    and index in selected
                    and low_price_unlocked
                    and (city_pressure >= 0.60 or aggressive)
                ):
                    reference = previous_price - price_cut
                if (
                    super_mode
                    and price_ratio_override is None
                    and index in selected
                    and low_price_unlocked
                    and (city_pressure >= 0.40 or aggressive)
                ):
                    rival_prices = [item["price"] for item in rival_metrics[index] if item["price"] > 0]
                    rival_floor = min(rival_prices, default=previous_price)
                    factor = aggressive_price_factor if aggressive else normal_price_factor
                    reference = min(previous_price - price_cut, rival_floor * factor)
                component_labor = component_need * worker_need * worker_hours / 504.0 * worker_salary * 3
                product_labor = engineer_need * engineer_hours / 504.0 * engineer_salary * 3
                direct_unit_cost = (
                    component_need * float(home_market["component_material"]) * material_factor
                    + float(home_market["product_material"]) * material_factor
                    + component_labor + product_labor
                    + (transport_cost if str(market["city"]) != home else 0.0)
                )
                margin = 1.12 if super_mode else 1.06
                if super_mode and price_ratio_override is not None:
                    # An explicit optimiser candidate may run a pure low-price
                    # strategy. Candidate scoring already rejects a total-plan
                    # loss, so only protect the direct unit contribution here.
                    reference = max(reference, direct_unit_cost * 1.03)
                else:
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
                own_cpi = allocate_city_cpi_for_company(
                    entries, market_size=size, max_price=float(market["max_price"]),
                    ma_large_threshold=ma_threshold, average_price=average_price,
                    market_average_price=previous_prices[index],
                    target_company_id=company_id,
                )
                capacity += size * own_cpi / 100.0
            return capacity

        city_rows = make_city_rows(inventory_heavy)
        if super_mode:
            # Search the permitted strategy space instead of hard-coding a
            # low-price personality. Every candidate is passed through the same
            # CPI allocator used by settlement and through the same complete
            # group-cost formula used by normal Bots.
            base_group_cost = max(
                0.0,
                group_cost
                - ma_index_target * (group["workers"] + group["engineers"])
                - qi_index_target * group["products"],
            )
            ma_candidates = sorted({
                1.0, 800.0, 1600.0, 2800.0, 4400.0, 6200.0, 8000.0,
                min(8000.0, max(1.0, ma_index_target)),
            })
            qi_candidates = sorted({
                1.0, 500.0, 1000.0, 1750.0, 2500.0, 3000.0,
                min(3000.0, max(1.0, qi_index_target)),
            })
            mi_ratio_candidates = (
                {0.0} if not use_mi else {0.0, 1.0, 1.5, 2.25, 3.0, 4.5, 6.0}
            )
            price_ratio_candidates = {
                min(0.98, max(0.75, high_price_ratio)), 0.95, 0.88, 0.81, 0.75,
            }
            if previous_leader:
                ma_candidates = sorted({
                    *ma_candidates,
                    min(8000.0, max(1.0, float(previous_leader["ma"]))),
                })
                qi_candidates = sorted({
                    *qi_candidates,
                    min(3000.0, max(1.0, float(previous_leader["qi"]))),
                })
                for index in selected:
                    leader_city = previous_leader["cities"].get(str(markets[index]["city"]))
                    if not leader_city:
                        continue
                    if use_mi and mi_thresholds.get(index, 0.0) > 0:
                        mi_ratio_candidates.add(min(
                            6.0,
                            max(0.0, float(leader_city["marketing"]) / mi_thresholds[index]),
                        ))
                    cap = min(price_max, float(markets[index]["max_price"]))
                    if cap > 0:
                        price_ratio_candidates.add(min(
                            0.98,
                            max(price_min / cap, float(leader_city["price"]) / cap),
                        ))
            if any(
                previous_prices[index] >= min(price_max, float(markets[index]["max_price"])) * 0.75
                for index in selected
            ):
                # Once the low-price condition opens, search continuously down
                # to the configured floor and include small undercuts of every
                # visible rival price. MA/QI=1 and MI=0 form the pure-price path.
                minimum_ratio = max(
                    price_min / max(1.0, min(price_max, float(markets[index]["max_price"])))
                    for index in selected
                )
                price_ratio_candidates.update(
                    max(minimum_ratio, ratio)
                    for ratio in (0.70, 0.64, 0.58, 0.52, 0.46, 0.40, 0.34, 0.28, 0.22, 0.16)
                )
                price_ratio_candidates.add(minimum_ratio)
                for index in selected:
                    cap = min(price_max, float(markets[index]["max_price"]))
                    if cap <= 0 or previous_prices[index] < cap * 0.75:
                        continue
                    for rival in rival_metrics[index]:
                        if rival["price"] > 0:
                            price_ratio_candidates.add(
                                min(0.98, max(minimum_ratio, rival["price"] / cap - 0.004))
                            )

            def evaluate_candidate(
                candidate_ma: float,
                candidate_qi: float,
                candidate_mi_ratio: float,
                candidate_price_ratio: float,
            ) -> dict[str, Any]:
                candidate_marketing = {
                    index: (
                        mi_thresholds.get(index, 0.0) * candidate_mi_ratio
                        if index in mi_selected and agent_plan[index][1] > 0 else 0.0
                    )
                    for index in selected
                }
                candidate_group_cost = (
                    base_group_cost
                    + candidate_ma * (group["workers"] + group["engineers"])
                    + candidate_qi * group["products"]
                )
                candidate_fixed = agent_cost + sum(candidate_marketing.values())
                candidate_group_limit = math.floor(
                    max(0.0, cash_budget - candidate_fixed) / max(candidate_group_cost, 1.0)
                )
                candidate_groups = _affordable_group_count(
                    cash_budget,
                    candidate_fixed,
                    candidate_group_cost,
                    candidate_group_limit,
                )
                candidate_production = int(math.floor(candidate_groups * group["products"]))
                candidate_available = old_products + candidate_production
                if candidate_available <= 0:
                    return {
                        "score": (-1, -math.inf, 0.0, candidate_price_ratio),
                        "ma": candidate_ma, "qi": candidate_qi,
                        "marketing": candidate_marketing, "groups": candidate_groups,
                        "price_ratio": candidate_price_ratio,
                    }

                city_capacities: list[tuple[int, float, float]] = []
                active_market_count = max(1, sum(1 for index in selected if agent_plan[index][1] > 0))
                for index in selected:
                    agents_after = agent_plan[index][1]
                    if agents_after <= 0:
                        continue
                    market = markets[index]
                    cap = min(price_max, float(market["max_price"]))
                    low_price_unlocked = previous_prices[index] >= cap * 0.75
                    effective_ratio = (
                        candidate_price_ratio
                        if low_price_unlocked or candidate_price_ratio >= 0.75
                        else min(0.98, max(0.75, high_price_ratio))
                    )
                    candidate_price = cap * effective_ratio
                    component_labor = component_need * worker_need * worker_hours / 504.0 * worker_salary * 3
                    product_labor = engineer_need * engineer_hours / 504.0 * engineer_salary * 3
                    direct_unit_cost = (
                        component_need * float(home_market["component_material"]) * material_factor
                        + float(home_market["product_material"]) * material_factor
                        + component_labor + product_labor
                        + (transport_cost if str(market["city"]) != home else 0.0)
                    )
                    candidate_price = min(cap, max(price_min, candidate_price, direct_unit_cost * 1.03))
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
                        "company_id": company_id, "ma_index": candidate_ma,
                        "qi_index": candidate_qi,
                        "mi_investment": candidate_marketing[index],
                        "price": candidate_price, "agents": agents_after,
                    })
                    weighted_prices.append((candidate_price, max(1.0, candidate_available / active_market_count)))
                    weighted_total = sum(weight for _, weight in weighted_prices)
                    average_price = sum(price * weight for price, weight in weighted_prices) / max(1.0, weighted_total)
                    size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                    own_cpi = allocate_city_cpi_for_company(
                        entries,
                        target_company_id=company_id,
                        market_size=size,
                        max_price=float(market["max_price"]),
                        ma_large_threshold=ma_threshold,
                        average_price=average_price,
                        market_average_price=previous_prices[index],
                    )
                    city_capacities.append((index, size * own_cpi / 100.0, candidate_price))

                total_capacity = sum(capacity for _, capacity, _ in city_capacities)
                predicted_sold = min(float(candidate_available), total_capacity)
                sell_ratio = predicted_sold / max(1.0, float(candidate_available))
                cpi_coverage = total_capacity / max(1.0, float(candidate_available))
                capacity_weight = sum(capacity for _, capacity, _ in city_capacities)
                predicted_price = sum(capacity * price for _, capacity, price in city_capacities) / max(1.0, capacity_weight)
                non_home_capacity = sum(
                    capacity for index, capacity, _ in city_capacities
                    if str(markets[index]["city"]) != home
                )
                predicted_transport = predicted_sold * transport_cost * non_home_capacity / max(1.0, capacity_weight)
                predicted_cost = candidate_fixed + candidate_groups * candidate_group_cost + predicted_transport
                predicted_profit = predicted_sold * predicted_price - predicted_cost
                # Profit is the primary objective. Sell-through and CPI/stock
                # fit break ties between similarly profitable strategies, so a
                # pure low-price plan is selected only when it truly earns more.
                return {
                    "score": (
                        int(predicted_profit > 0),
                        predicted_profit,
                        sell_ratio,
                        -abs(cpi_coverage - 1.0),
                        candidate_price_ratio,
                    ),
                    "ma": candidate_ma, "qi": candidate_qi,
                    "marketing": candidate_marketing, "groups": candidate_groups,
                    "price_ratio": candidate_price_ratio,
                }

            best_candidate: dict[str, Any] | None = None
            for candidate_ma in ma_candidates:
                for candidate_qi in qi_candidates:
                    for candidate_mi_ratio in sorted(mi_ratio_candidates):
                        for candidate_price_ratio in sorted(price_ratio_candidates, reverse=True):
                            candidate = evaluate_candidate(
                                candidate_ma,
                                candidate_qi,
                                candidate_mi_ratio,
                                candidate_price_ratio,
                            )
                            if best_candidate is None or candidate["score"] > best_candidate["score"]:
                                best_candidate = candidate

            if best_candidate is not None:
                ma_index_target = float(best_candidate["ma"])
                qi_index_target = float(best_candidate["qi"])
                marketing_targets = dict(best_candidate["marketing"])
                low_groups = 0
                high_groups = max(1, int(best_candidate["groups"]))
                while budget_for(int(math.floor(high_groups * group["products"])))["total"] <= cash_budget + 1e-9:
                    low_groups = high_groups
                    high_groups *= 2
                while low_groups < high_groups:
                    middle_groups = (low_groups + high_groups + 1) // 2
                    middle_products = int(math.floor(middle_groups * group["products"]))
                    if budget_for(middle_products)["total"] <= cash_budget + 1e-9:
                        low_groups = middle_groups
                    else:
                        high_groups = middle_groups - 1
                chosen_groups = low_groups
                production = int(math.floor(chosen_groups * group["products"]))
                budget = budget_for(production)
                city_rows = make_city_rows(
                    False,
                    float(best_candidate["price_ratio"]),
                )

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
            cash_budget - float(budget["total"])
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
                    and cash_budget >= research_goal * 4.0
                    and projected_cash - research_needed >= cash_budget * 1.05
                )
            )
        )
        research = research_needed if research_ready else 0.0

        conn.execute(
            "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,"
            "engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, loan_change, budget["worker_delta"], worker_salary, budget["engineer_delta"],
             engineer_salary, budget["management"], production, budget["quality"], research, now_iso()),
        )
        for row in city_rows:
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                "VALUES(?,?,?,?,?,?,0)",
                (company_id, round_no, row["city"], row["agent_delta"], row["marketing"], row["price"]),
            )
        # Release the SQLite writer lock between expensive Super Bot searches.
        # A rerun is safe because submit_super_bot_decisions clears and rebuilds
        # the complete Super Bot submission set before starting again.
        if super_mode:
            conn.commit()
            submitted_super_ids.add(company_id)
        submitted += 1
        if progress_callback:
            progress_callback(submitted, max(1, progress_total), str(bot["code"]))
    if super_mode and submitted:
        _rebalance_super_bot_production(conn, round_no, markets)
        if progress_callback:
            progress_callback(progress_total, progress_total, "联合复算")
    return submitted


def submit_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Ordinary bots submit immediately when a round opens."""
    return _submit_bots(conn, round_no, False)


def _submit_super_bot_decisions_locked(
    conn: sqlite3.Connection,
    round_no: int,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    """Super bots wait until every non-super team has submitted."""
    missing = one(
        conn,
        "SELECT COUNT(*) AS n FROM companies c WHERE c.is_super_bot=0 AND NOT EXISTS "
        "(SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=? AND d.submitted_at IS NOT NULL)",
        (round_no,),
    )
    if missing and int(missing["n"]) > 0:
        raise ValueError("仍有真人玩家或普通 Bot 未提交，超级 Bot 暂不能读取本轮数据。")
    # End the preceding read snapshot, then perform cleanup as one short write
    # transaction. Streamlit can rerun a session while another request is
    # finishing, so retry SQLITE_BUSY/locked instead of crashing the app.
    conn.commit()
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        for attempt in range(4):
            try:
                conn.execute("BEGIN IMMEDIATE")
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
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if not any(word in str(exc).lower() for word in ("locked", "busy")) or attempt == 3:
                    raise
                time.sleep(0.15 * (2 ** attempt))
    finally:
        conn.execute("PRAGMA busy_timeout=30000")
    return _submit_bots(conn, round_no, True, progress_callback)


def submit_super_bot_decisions(
    conn: sqlite3.Connection,
    round_no: int,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> int:
    """Serialize expensive submissions inside one Streamlit worker process."""
    with _SUPER_BOT_SUBMISSION_LOCK:
        return _submit_super_bot_decisions_locked(conn, round_no, progress_callback)
