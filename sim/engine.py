from __future__ import annotations

import json
import math
import random
import sqlite3
from typing import Any

from .cpi import allocate_city_cpi
from .db import all_rows, effective_employee_count, employee_count, get_setting, now_iso, one, remove_employees


def market_size(market: sqlite3.Row | dict[str, Any], round_no: int, growth: float) -> float:
    return float(market["population"]) * float(market["penetration"]) * (growth ** max(0, round_no - 1))


def weighted_market_average(base_average_price: float, size: float, price_quantity_pairs: list[tuple[float, float]]) -> float:
    """Blend actual player sales with the reference price for unserved demand."""
    base = max(0.0, float(base_average_price))
    market_capacity = max(0.0, float(size))
    if market_capacity <= 0:
        return base
    pairs = [(max(0.0, float(price)), max(0.0, float(quantity))) for price, quantity in price_quantity_pairs]
    player_total = sum(quantity for _, quantity in pairs)
    if player_total <= 0:
        return base
    scale = min(1.0, market_capacity / player_total)
    player_value = sum(price * quantity * scale for price, quantity in pairs)
    served = min(player_total, market_capacity)
    return (player_value + base * (market_capacity - served)) / market_capacity


def reference_market_average(conn: sqlite3.Connection, city: str, round_no: int, initial_average: float) -> float:
    previous = one(
        conn,
        "SELECT average_price FROM market_round_stats WHERE city=? AND round_no<? ORDER BY round_no DESC LIMIT 1",
        (city, round_no),
    )
    return float(previous["average_price"]) if previous else float(initial_average)


def research_probability(investment: float, amount_25: float, amount_75: float) -> float:
    if investment <= 0:
        return 0.0
    if investment <= amount_25:
        return 0.25 * investment / max(amount_25, 1.0)
    if investment <= amount_75:
        return 0.25 + 0.50 * (investment - amount_25) / max(amount_75 - amount_25, 1.0)
    return min(0.95, 0.75 + 0.20 * (investment - amount_75) / max(amount_75, 1.0))


def available_loan_limit(net_assets: float, threshold: float, minimum: float, maximum: float) -> float:
    """Return this round's permitted new loan using the configured KDS range."""
    ceiling = max(0.0, float(maximum))
    floor = min(ceiling, max(0.0, float(minimum)))
    if threshold <= 0:
        return ceiling
    calculated = max(0.0, float(net_assets)) / float(threshold) * ceiling
    return min(ceiling, max(floor, calculated))


def current_company_net_assets(conn: sqlite3.Connection, company: sqlite3.Row | dict[str, Any]) -> float:
    """Return the latest settled net assets, including unsold inventory value."""
    latest = one(
        conn,
        "SELECT net_assets FROM results WHERE company_id=? ORDER BY round_no DESC LIMIT 1",
        (int(company["id"]),),
    )
    if latest is not None:
        return float(latest["net_assets"])
    inventory_value = 0.0
    if company["home_city"] and int(company["product_inventory"] or 0) > 0:
        home = one(conn, "SELECT product_material FROM market_config WHERE city=?", (company["home_city"],))
        if home is not None:
            patent_factor = get_setting(conn, "patent_factor", 0.70)
            inventory_value = (
                int(company["product_inventory"])
                * float(home["product_material"])
                * (float(patent_factor) ** int(company["patents"] or 0))
            )
    return float(company["cash"]) + inventory_value - float(company["debt"])


def weighted_salary_average(rows: list[tuple[int, float, float]], fallback: float) -> float:
    """Three-month average: one player-paid month plus two KDS-base months."""
    total_people = sum(max(0, int(count)) for count, _, _ in rows)
    if total_people <= 0:
        return max(0.0, float(fallback))
    numerator = sum(
        max(0, int(count)) * (max(0.0, float(salary)) + 2.0 * max(0.0, float(initial_salary)))
        for count, salary, initial_salary in rows
    )
    return numerator / (total_people * 3.0)


def spend(available_cash: float, requested: float) -> tuple[float, float]:
    """Pay as much as possible without ever making cash negative."""
    paid = min(max(0.0, float(available_cash)), max(0.0, float(requested)))
    return max(0.0, float(available_cash) - paid), paid


def allocate_integer_sales(city_sales: dict[str, float], available_units: int | float) -> dict[str, int]:
    """Convert fractional city allocations to units without losing inventory.

    CPI allocation is continuous, but products are whole units. Flooring every
    city separately can turn one product split across two markets into zero
    sales in both. The largest-remainder method rounds the company total once,
    then assigns the remaining units to the strongest fractional allocations.
    """
    available = max(0, int(available_units))
    targets = {city: max(0.0, float(value)) for city, value in city_sales.items()}
    continuous_total = sum(targets.values())
    target_total = min(available, max(0, math.floor(continuous_total + 0.5 + 1e-9)))
    units = {city: max(0, math.floor(value + 1e-9)) for city, value in targets.items()}
    assigned = sum(units.values())
    remaining = max(0, target_total - assigned)
    ranked = sorted(
        targets,
        key=lambda city: (-(targets[city] - math.floor(targets[city])), city),
    )
    for city in ranked:
        if remaining <= 0:
            break
        if targets[city] <= 0:
            continue
        units[city] += 1
        remaining -= 1
    return units


def _previous_salary(conn: sqlite3.Connection, company_id: int, round_no: int, field: str, fallback: float) -> float:
    if field not in {"worker_salary", "engineer_salary"}:
        raise ValueError("Invalid salary field")
    row = one(
        conn,
        f"SELECT d.{field} AS salary FROM decisions d JOIN results r ON r.company_id=d.company_id "
        "AND r.round_no=d.round_no WHERE d.company_id=? AND d.round_no<? ORDER BY d.round_no DESC LIMIT 1",
        (company_id, round_no),
    )
    return float(row["salary"]) if row else float(fallback)


def settle_round(conn: sqlite3.Connection, round_no: int) -> None:
    round_row = one(conn, "SELECT * FROM rounds WHERE round_no=?", (round_no,))
    if round_row is None:
        raise ValueError("回合不存在。")
    if one(conn, "SELECT COUNT(*) AS n FROM results WHERE round_no=?", (round_no,))["n"]:
        raise ValueError("本轮已经结算。")

    companies = all_rows(conn, "SELECT * FROM companies ORDER BY id")
    markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
    if not companies or not markets:
        raise ValueError("缺少队伍或市场配置。")
    missing = [
        company["code"]
        for company in companies
        if one(conn, "SELECT submitted_at FROM decisions WHERE company_id=? AND round_no=? AND submitted_at IS NOT NULL", (company["id"], round_no)) is None
    ]
    if missing:
        raise ValueError("仍有队伍未提交：" + "、".join(missing))

    a = get_setting(conn, "component_workers", 3.0)
    b = get_setting(conn, "component_hours", 7.0)
    c = get_setting(conn, "product_engineers", 4.0)
    d_hours = get_setting(conn, "product_hours", 14.0)
    components_per_product = get_setting(conn, "components_per_product", 7.0)
    salary_min = get_setting(conn, "salary_min", 1_000.0)
    salary_max = get_setting(conn, "salary_max", 10_000.0)
    salary_change_limit = max(0.0, get_setting(conn, "salary_change_limit", 1_000.0))
    price_min = get_setting(conn, "price_min", 3_500.0)
    global_price_max = get_setting(conn, "price_max", 25_000.0)
    growth = get_setting(conn, "market_growth", 1.10)
    patent_factor = get_setting(conn, "patent_factor", 0.70)
    agent_add_cost = get_setting(conn, "agent_add_cost", 300_000.0)
    agent_remove_cost = get_setting(conn, "agent_remove_cost", 100_000.0)
    max_agent_add = max(0, get_setting(conn, "max_agent_add_per_city_round", 3, int))
    report_cost_each = get_setting(conn, "report_cost", 200_000.0)
    tax_rate = get_setting(conn, "tax_rate", 0.20)
    loan_threshold = get_setting(conn, "loan_asset_threshold", 15_000_000.0)
    ma_large_threshold = get_setting(conn, "cpi_ma_large_threshold", 1_300.0)
    price_power = get_setting(conn, "cpi_price_power", 8, int)

    decisions: dict[int, dict[str, Any]] = {}
    worker_average_rows: list[tuple[int, float, float]] = []
    engineer_average_rows: list[tuple[int, float, float]] = []
    for company_row in companies:
        company = dict(company_row)
        company_id = int(company["id"])
        decision_row = one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company_id, round_no))
        if decision_row is None:
            raise ValueError(f"{company['code']} 没有本轮决策。")
        decision = dict(decision_row)
        home_row = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        if home_row is None:
            raise ValueError(f"{company['code']} 尚未设置有效主场。")
        home = dict(home_row)
        previous_workers = employee_count(conn, company_id, "worker")
        previous_engineers = employee_count(conn, company_id, "engineer")
        worker_delta = max(int(decision["worker_delta"]), -previous_workers)
        engineer_delta = max(int(decision["engineer_delta"]), -previous_engineers)
        workers_after = previous_workers + worker_delta
        engineers_after = previous_engineers + engineer_delta

        previous_worker_salary = _previous_salary(conn, company_id, round_no, "worker_salary", float(home["worker_initial_salary"]))
        previous_engineer_salary = _previous_salary(conn, company_id, round_no, "engineer_salary", float(home["engineer_initial_salary"]))
        worker_reference = min(max(previous_worker_salary, salary_min), salary_max)
        engineer_reference = min(max(previous_engineer_salary, salary_min), salary_max)
        worker_low = max(salary_min, worker_reference - salary_change_limit)
        worker_high = min(salary_max, worker_reference + salary_change_limit)
        engineer_low = max(salary_min, engineer_reference - salary_change_limit)
        engineer_high = min(salary_max, engineer_reference + salary_change_limit)
        decision["worker_salary"] = min(max(float(decision["worker_salary"]), worker_low), worker_high)
        decision["engineer_salary"] = min(max(float(decision["engineer_salary"]), engineer_low), engineer_high)
        decision["worker_delta"] = worker_delta
        decision["engineer_delta"] = engineer_delta
        decision["home"] = home
        decisions[company_id] = decision
        worker_average_rows.append((workers_after, decision["worker_salary"], float(home["worker_initial_salary"])))
        engineer_average_rows.append((engineers_after, decision["engineer_salary"], float(home["engineer_initial_salary"])))

    fallback_worker = sum(float(decisions[int(row["id"])]["home"]["worker_initial_salary"]) for row in companies) / len(companies)
    fallback_engineer = sum(float(decisions[int(row["id"])]["home"]["engineer_initial_salary"]) for row in companies) / len(companies)
    average_worker_wage = weighted_salary_average(worker_average_rows, fallback_worker)
    average_engineer_wage = weighted_salary_average(engineer_average_rows, fallback_engineer)
    market_base_averages = {
        str(market["city"]): reference_market_average(conn, str(market["city"]), round_no, float(market["initial_avg_price"]))
        for market in markets
    }

    states: dict[int, dict[str, Any]] = {}
    for company_row in companies:
        company = dict(company_row)
        company_id = int(company["id"])
        decision = decisions[company_id]
        home = decision["home"]
        previous_workers = employee_count(conn, company_id, "worker")
        previous_engineers = employee_count(conn, company_id, "engineer")
        actual_worker_delta = int(decision["worker_delta"])
        actual_engineer_delta = int(decision["engineer_delta"])

        if actual_worker_delta > 0:
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,?)", (company_id, "worker", actual_worker_delta, round_no))
        elif actual_worker_delta < 0:
            remove_employees(conn, company_id, "worker", -actual_worker_delta)
        if actual_engineer_delta > 0:
            conn.execute("INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,?)", (company_id, "engineer", actual_engineer_delta, round_no))
        elif actual_engineer_delta < 0:
            remove_employees(conn, company_id, "engineer", -actual_engineer_delta)

        workers = employee_count(conn, company_id, "worker")
        engineers = employee_count(conn, company_id, "engineer")
        worker_multiplier = min(float(decision["worker_salary"]) / max(average_worker_wage, 1.0), 1.10)
        engineer_multiplier = min(float(decision["engineer_salary"]) / max(average_engineer_wage, 1.0), 1.10)
        effective_workers = effective_employee_count(conn, company_id, "worker", round_no) * worker_multiplier
        effective_engineers = effective_employee_count(conn, company_id, "engineer", round_no) * engineer_multiplier
        component_capacity = (504.0 / b) * (effective_workers / a) if a and b else 0.0
        engineer_product_capacity = (504.0 / d_hours) * (effective_engineers / c) if c and d_hours else 0.0
        component_product_capacity = component_capacity / components_per_product if components_per_product else 0.0
        planned = max(0, int(decision["production_volume"]))
        capacity_produced = max(0, math.floor(min(planned, engineer_product_capacity, component_product_capacity)))

        debt = float(company["debt"])
        cash = max(0.0, float(company["cash"]))
        loan_base_net_assets = current_company_net_assets(conn, company)
        loan_limit = available_loan_limit(loan_base_net_assets, loan_threshold, float(home.get("min_loan", 0.0)), float(home["max_loan"]))
        requested_loan_change = float(decision["loan_change"])
        if requested_loan_change >= 0:
            requested_new_loan = requested_loan_change if requested_loan_change + 1e-9 >= float(home.get("min_loan", 0.0)) else 0.0
            actual_loan_change = min(requested_new_loan, loan_limit)
            debt += actual_loan_change
            cash += actual_loan_change
        else:
            repayment = min(-requested_loan_change, debt, cash)
            actual_loan_change = -repayment
            debt -= repayment
            cash -= repayment

        cash, worker_wage_cost = spend(cash, workers * float(decision["worker_salary"]) * 3)
        cash, engineer_wage_cost = spend(cash, engineers * float(decision["engineer_salary"]) * 3)
        requested_layoff = max(-actual_worker_delta, 0) * float(decision["worker_salary"]) + max(-actual_engineer_delta, 0) * float(decision["engineer_salary"])
        cash, layoff_cost = spend(cash, requested_layoff)
        requested_training = max(actual_worker_delta, 0) * float(home["worker_training_cost"]) + max(actual_engineer_delta, 0) * float(home["engineer_training_cost"])
        cash, training_cost = spend(cash, requested_training)

        active_patents = int(company["patents"])
        material_multiplier = patent_factor ** active_patents
        old_products = int(company["product_inventory"])
        component_storage_before = int(company["component_storage_capacity"])
        product_storage_before = int(company["product_storage_capacity"])

        def production_cost(quantity: int) -> tuple[int, float, float, float, float, int, int]:
            component_units = math.ceil(max(0, quantity) * components_per_product)
            component_storage_increase = max(0, component_units - component_storage_before)
            product_storage_increase = max(0, old_products + max(0, quantity) - product_storage_before)
            return (
                component_units,
                component_units * float(home["component_material"]) * material_multiplier,
                component_storage_increase * float(home["component_storage"]),
                max(0, quantity) * float(home["product_material"]) * material_multiplier,
                product_storage_increase * float(home["product_storage"]),
                component_storage_increase,
                product_storage_increase,
            )

        low, high = 0, capacity_produced
        while low < high:
            mid = (low + high + 1) // 2
            candidate = production_cost(mid)
            if sum(candidate[1:5]) <= cash + 1e-9:
                low = mid
            else:
                high = mid - 1
        produced = low
        (components, requested_component_material, requested_component_storage, requested_product_material, requested_product_storage, component_storage_increase, product_storage_increase) = production_cost(produced)
        cash, component_material_cost = spend(cash, requested_component_material)
        cash, component_storage_cost = spend(cash, requested_component_storage)
        cash, product_material_cost = spend(cash, requested_product_material)
        cash, product_storage_cost = spend(cash, requested_product_storage)
        storage_cost = component_storage_cost + product_storage_cost
        conn.execute(
            "UPDATE companies SET component_storage_capacity=MAX(component_storage_capacity,?),product_storage_capacity=MAX(product_storage_capacity,?) WHERE id=?",
            (components, old_products + produced, company_id),
        )

        city_decisions: dict[str, dict[str, Any]] = {}
        total_agent_cost = 0.0
        for market_row in markets:
            market = dict(market_row)
            city = str(market["city"])
            city_row = one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=? AND city=?", (company_id, round_no, city))
            city_decision = dict(city_row) if city_row else {"agent_delta": 0, "marketing_investment": 0.0, "price": market["initial_avg_price"], "order_report": 0}
            old_agent_row = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company_id, city))
            old_agents = int(old_agent_row["count"]) if old_agent_row else 0
            requested_delta = max(-old_agents, min(int(city_decision["agent_delta"]), max_agent_add))
            unit_cost = agent_add_cost if requested_delta >= 0 else agent_remove_cost
            affordable_units = abs(requested_delta) if unit_cost <= 0 else min(abs(requested_delta), int(cash // unit_cost))
            actual_agent_delta = affordable_units if requested_delta >= 0 else -affordable_units
            cash, agent_cost = spend(cash, affordable_units * unit_cost)
            total_agent_cost += agent_cost
            new_agents = old_agents + actual_agent_delta
            conn.execute("INSERT INTO agents(company_id,city,count) VALUES(?,?,?) ON CONFLICT(company_id,city) DO UPDATE SET count=excluded.count", (company_id, city, new_agents))
            city_decision["agents_after"] = new_agents
            city_decision["agent_delta_actual"] = actual_agent_delta
            city_decision["price"] = min(max(float(city_decision["price"] or market["initial_avg_price"]), price_min), min(global_price_max, float(market["max_price"])))
            city_decision["marketing_requested"] = max(0.0, float(city_decision["marketing_investment"]))
            city_decision["report_requested"] = bool(city_decision["order_report"])
            city_decisions[city] = city_decision

        total_marketing = 0.0
        for market_row in markets:
            city = str(market_row["city"])
            cash, paid_marketing = spend(cash, city_decisions[city]["marketing_requested"])
            city_decisions[city]["marketing_investment"] = paid_marketing
            total_marketing += paid_marketing
        cash, quality = spend(cash, max(0.0, float(decision["quality_investment"])))
        cash, management = spend(cash, max(0.0, float(decision["management_investment"])))
        ma_index = management / max(workers + engineers, 1)
        qi_index = quality / max(old_products * 1.2 + produced, 1.0)

        states[company_id] = {
            "company": company, "decision": decision, "home": home,
            "workers": workers, "engineers": engineers,
            "previous_workers": previous_workers, "previous_engineers": previous_engineers,
            "worker_delta": actual_worker_delta, "engineer_delta": actual_engineer_delta,
            "effective_workers": effective_workers, "effective_engineers": effective_engineers,
            "worker_multiplier": worker_multiplier, "engineer_multiplier": engineer_multiplier,
            "average_worker_wage": average_worker_wage, "average_engineer_wage": average_engineer_wage,
            "produced": produced, "components": components, "available": old_products + produced, "old_products": old_products,
            "cash_pre_sales": cash, "debt_before_interest": debt, "loan_base_net_assets": loan_base_net_assets,
            "loan_limit": loan_limit, "loan_change": actual_loan_change,
            "worker_wage_cost": worker_wage_cost, "engineer_wage_cost": engineer_wage_cost, "wage_cost": worker_wage_cost + engineer_wage_cost,
            "layoff_cost": layoff_cost, "training_cost": training_cost,
            "component_material_cost": component_material_cost, "product_material_cost": product_material_cost,
            "component_storage_cost": component_storage_cost, "product_storage_cost": product_storage_cost, "storage_cost": storage_cost,
            "component_storage_before": component_storage_before, "product_storage_before": product_storage_before,
            "component_storage_increase": component_storage_increase, "product_storage_increase": product_storage_increase,
            "agent_cost": total_agent_cost, "marketing_total": total_marketing, "management": management, "quality": quality,
            "research_requested": max(0.0, float(decision["research_investment"])), "active_patents": active_patents,
            "ma_index": ma_index, "qi_index": qi_index, "city_decisions": city_decisions,
            "city_sales": {str(m["city"]): 0.0 for m in markets}, "city_secondary": {str(m["city"]): 0.0 for m in markets},
            "city_cpi": {str(m["city"]): 0.0 for m in markets}, "city_cpi_units": {str(m["city"]): 0.0 for m in markets},
            "city_breakdown": {str(m["city"]): {} for m in markets},
        }

    for market_row in markets:
        market = dict(market_row)
        city = str(market["city"])
        size = market_size(market, round_no, growth)
        entries = []
        for company_id, state in states.items():
            city_decision = state["city_decisions"][city]
            if int(city_decision["agents_after"]) <= 0:
                continue
            entries.append({"company_id": company_id, "ma_index": state["ma_index"], "qi_index": state["qi_index"], "mi_investment": float(city_decision["marketing_investment"]), "price": float(city_decision["price"])})
        allocations = allocate_city_cpi(entries, market_size=size, max_price=float(market["max_price"]), ma_large_threshold=ma_large_threshold, price_power=price_power, average_price=market_base_averages[city], market_average_price=market_base_averages[city])
        for allocation in allocations:
            company_id = int(allocation["company_id"])
            cpi = float(allocation["total_cpi"])
            states[company_id]["city_cpi"][city] = cpi
            states[company_id]["city_cpi_units"][city] = size * cpi / 100.0
            states[company_id]["city_breakdown"][city] = allocation

    for state in states.values():
        total_capacity = sum(state["city_cpi_units"].values())
        available = float(state["available"])
        if total_capacity <= 0 or available <= 0:
            continue
        factor = min(1.0, available / total_capacity)
        for city, capacity in state["city_cpi_units"].items():
            state["city_sales"][city] = capacity * factor

    for _ in range(10):
        remaining = {company_id: max(0.0, state["available"] - sum(state["city_sales"].values())) for company_id, state in states.items()}
        moved = 0.0
        for market_row in markets:
            market = dict(market_row)
            city = str(market["city"])
            size = market_size(market, round_no, growth)
            gap = max(0.0, size - sum(state["city_sales"][city] for state in states.values()))
            candidates = []
            for company_id, state in states.items():
                if remaining[company_id] <= 0.5 or int(state["city_decisions"][city]["agents_after"]) <= 0:
                    continue
                candidates.append((company_id, max(float(state["city_cpi_units"][city]), size * 0.0001)))
            if gap <= 0.5 or not candidates:
                continue
            score_sum = sum(score for _, score in candidates)
            for company_id, score in candidates:
                addition = min(remaining[company_id], gap * score / score_sum)
                states[company_id]["city_sales"][city] += addition
                states[company_id]["city_secondary"][city] += addition
                remaining[company_id] -= addition
                moved += addition
        if moved < 1.0:
            break

    for state in states.values():
        state["city_sales_units"] = allocate_integer_sales(state["city_sales"], state["available"])

    market_round_stats: dict[str, dict[str, float]] = {}
    for market_row in markets:
        market = dict(market_row)
        city = str(market["city"])
        size = market_size(market, round_no, growth)
        pairs = [(float(state["city_decisions"][city]["price"]), float(state["city_sales_units"][city])) for state in states.values()]
        total_volume = sum(quantity for _, quantity in pairs)
        base_average = market_base_averages[city]
        average_price = weighted_market_average(base_average, size, pairs)
        market_round_stats[city] = {"base_average_price": base_average, "average_price": average_price, "market_size": size, "player_total_volume": total_volume}
        conn.execute(
            "INSERT INTO market_round_stats(city,round_no,base_average_price,average_price,market_size,player_total_volume) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(city,round_no) DO UPDATE SET base_average_price=excluded.base_average_price,average_price=excluded.average_price,market_size=excluded.market_size,player_total_volume=excluded.player_total_volume",
            (city, round_no, base_average, average_price, size, total_volume),
        )

    amount_25 = get_setting(conn, "research_25", 1_500_000.0)
    amount_75 = get_setting(conn, "research_75", 6_000_000.0)
    for company_id, state in states.items():
        company = state["company"]
        home = state["home"]
        revenue = 0.0
        sold = 0
        city_report_rows: list[dict[str, Any]] = []
        for market_row in markets:
            market = dict(market_row)
            city = str(market["city"])
            city_decision = state["city_decisions"][city]
            units = int(state["city_sales_units"][city])
            sold += units
            transport = float(market["transport_cost"]) if company["home_city"] != city else 0.0
            transport_total = units * transport
            net_city_revenue = units * float(city_decision["price"]) - transport_total
            revenue += net_city_revenue
            size = market_size(market, round_no, growth)
            share = units / max(size, 1.0)
            allocation = state["city_breakdown"][city]
            allocated_before_secondary = state["city_cpi_units"][city]
            secondary_units = float(state["city_secondary"][city])
            breakdown = dict(allocation) if allocation else {}
            breakdown.update({"market_size": size, "cpi_units": allocated_before_secondary, "secondary_units": secondary_units, "agents": int(city_decision["agents_after"])})
            if int(city_decision["agents_after"]) > 0:
                conn.execute(
                    "INSERT INTO city_results(company_id,round_no,city,cpi,cpi_units,sold,revenue,price,marketing,market_share,breakdown_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (company_id, round_no, city, float(state["city_cpi"][city]), allocated_before_secondary, units, net_city_revenue, float(city_decision["price"]), float(city_decision["marketing_investment"]), share, json.dumps(breakdown, ensure_ascii=False)),
                )
            public_allocation = {key: value for key, value in allocation.items() if key not in {"average_price", "market_average_price"}} if allocation else {}
            city_report_rows.append({
                "city": city, "agents": int(city_decision["agents_after"]), "marketing": float(city_decision["marketing_investment"]),
                "price": float(city_decision["price"]), "cpi": float(state["city_cpi"][city]), "cpi_units": allocated_before_secondary,
                "secondary_units": secondary_units, "sold": units, "market_share": share, "market_size": size, "transport": transport_total,
                "report_requested": bool(city_decision["report_requested"]), "report_purchased": False, "breakdown": public_allocation,
            })

        inventory = max(0, int(state["available"] - sold))
        cash = state["cash_pre_sales"] + revenue
        cash, research = spend(cash, state["research_requested"])
        total_report_cost = 0.0
        for item in city_report_rows:
            if not item["report_requested"] or cash + 1e-9 < report_cost_each:
                continue
            cash, paid = spend(cash, report_cost_each)
            total_report_cost += paid
            item["report_purchased"] = True

        interest = max(0.0, float(state["debt_before_interest"])) * float(home["interest_rate"])
        debt = float(state["debt_before_interest"]) + interest
        pre_sales_cost = state["wage_cost"] + state["layoff_cost"] + state["training_cost"] + state["component_material_cost"] + state["product_material_cost"] + state["storage_cost"] + state["agent_cost"] + state["marketing_total"] + state["quality"] + state["management"]
        operating_cost = pre_sales_cost + research + total_report_cost + interest
        pre_tax_profit = revenue - operating_cost
        cash, tax = spend(cash, max(0.0, pre_tax_profit * tax_rate))
        total_cost = operating_cost + tax
        net_profit = revenue - total_cost

        probability = research_probability(research, amount_25, amount_75)
        research_success = 1 if random.Random(f"{round_no}:{company_id}:patent").random() < probability else 0
        patents_after = int(state["active_patents"]) + research_success
        inventory_book_value = inventory * float(home["product_material"]) * (patent_factor ** state["active_patents"])
        total_assets = cash + inventory_book_value
        net_assets = total_assets - debt
        report = {
            "key_metrics": {"total_assets": total_assets, "debt": debt, "net_assets": net_assets, "sales_revenue": revenue, "cost": total_cost, "net_profit": net_profit, "inventory_book_value": inventory_book_value},
            "finance": {
                "round_begins": company["cash"], "starting_debt": company["debt"], "loan_base_net_assets": state["loan_base_net_assets"],
                "loan_limit": state["loan_limit"], "loan_change": state["loan_change"],
                "worker_wages": state["worker_wage_cost"], "engineer_wages": state["engineer_wage_cost"], "wages": state["wage_cost"],
                "layoff": state["layoff_cost"], "training": state["training_cost"],
                "component_material": state["component_material_cost"], "product_material": state["product_material_cost"],
                "component_storage": state["component_storage_cost"], "product_storage": state["product_storage_cost"],
                "materials": state["component_material_cost"] + state["product_material_cost"], "storage": state["storage_cost"],
                "agents": state["agent_cost"], "marketing": state["marketing_total"], "quality": state["quality"], "management": state["management"],
                "sales_revenue": revenue, "research": research, "market_reports": total_report_cost, "interest": interest, "tax": tax, "round_ends": cash,
            },
            "human_resources": {
                "workers": state["workers"], "engineers": state["engineers"], "previous_workers": state["previous_workers"], "previous_engineers": state["previous_engineers"],
                "worker_delta": state["worker_delta"], "engineer_delta": state["engineer_delta"], "effective_workers": state["effective_workers"], "effective_engineers": state["effective_engineers"],
                "worker_salary": state["decision"]["worker_salary"], "engineer_salary": state["decision"]["engineer_salary"],
                "average_worker_salary": state["average_worker_wage"], "average_engineer_salary": state["average_engineer_wage"],
                "worker_wage_multiplier": state["worker_multiplier"], "engineer_wage_multiplier": state["engineer_multiplier"],
            },
            "production": {
                "planned": state["decision"]["production_volume"], "produced": state["produced"], "components": state["components"], "old_products": state["old_products"],
                "sold": sold, "surplus": inventory, "ma_index": state["ma_index"], "qi_index": state["qi_index"],
                "component_storage_before": state["component_storage_before"], "component_storage_after": state["component_storage_before"] + state["component_storage_increase"], "component_storage_increase": state["component_storage_increase"],
                "product_storage_before": state["product_storage_before"], "product_storage_after": state["product_storage_before"] + state["product_storage_increase"], "product_storage_increase": state["product_storage_increase"],
            },
            "research": {"investment": research, "probability": probability, "success": bool(research_success), "active_patents_this_round": state["active_patents"], "patents_after": patents_after, "effective_from_round": round_no + 1 if research_success else None},
            "sales": city_report_rows,
            "cpi_algorithm": {"version": get_setting(conn, "cpi_algorithm_version", "cpi-generator-admin-v1", str), "description": "赠品 + 第一层 + 第二层 + 福利1/2；价格差按设定幂次分配 40 CPI；各城市独立计算。", "price_power": price_power},
        }
        conn.execute(
            "INSERT INTO results(company_id,round_no,total_assets,debt,net_assets,cash,sales_revenue,total_cost,net_profit,produced,sold,inventory,ma_index,qi_index,research_success,report_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, total_assets, debt, net_assets, cash, revenue, total_cost, net_profit, state["produced"], sold, inventory, state["ma_index"], state["qi_index"], research_success, json.dumps(report, ensure_ascii=False)),
        )
        conn.execute("UPDATE companies SET cash=?,debt=?,patents=?,product_inventory=? WHERE id=?", (cash, debt, patents_after, inventory, company_id))
    conn.execute("UPDATE rounds SET status='settled',settled_at=? WHERE round_no=?", (now_iso(), round_no))
