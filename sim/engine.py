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


def weighted_market_average(
    base_average_price: float,
    size: float,
    price_quantity_pairs: list[tuple[float, float]],
) -> float:
    """Blend player sales with the reference price for unserved demand.

    Formula supplied by the user:
    (sum(player price * player volume) + base average * remaining market) / market size.
    """
    base = max(0.0, float(base_average_price))
    market_capacity = max(0.0, float(size))
    if market_capacity <= 0:
        return base
    pairs = [(max(0.0, float(price)), max(0.0, float(quantity))) for price, quantity in price_quantity_pairs]
    player_total = sum(quantity for _, quantity in pairs)
    if player_total <= 0:
        return base
    # Settlement already caps city volume at market size. The scale keeps this
    # helper safe for imported/legacy data that might exceed that capacity.
    scale = min(1.0, market_capacity / player_total)
    player_value = sum(price * quantity * scale for price, quantity in pairs)
    served = min(player_total, market_capacity)
    return (player_value + base * (market_capacity - served)) / market_capacity


def reference_market_average(
    conn: sqlite3.Connection,
    city: str,
    round_no: int,
    initial_average: float,
) -> float:
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
        if one(
            conn,
            "SELECT submitted_at FROM decisions WHERE company_id=? AND round_no=? AND submitted_at IS NOT NULL",
            (company["id"], round_no),
        )
        is None
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
    price_min = get_setting(conn, "price_min", 3_500.0)
    global_price_max = get_setting(conn, "price_max", 25_000.0)
    growth = get_setting(conn, "market_growth", 1.10)
    patent_factor = get_setting(conn, "patent_factor", 0.70)
    agent_add_cost = get_setting(conn, "agent_add_cost", 300_000.0)
    agent_remove_cost = get_setting(conn, "agent_remove_cost", 100_000.0)
    report_cost_each = get_setting(conn, "report_cost", 200_000.0)
    tax_rate = get_setting(conn, "tax_rate", 0.20)
    ma_large_threshold = get_setting(conn, "cpi_ma_large_threshold", 1_300.0)
    price_power = get_setting(conn, "cpi_price_power", 8, int)

    decisions: dict[int, dict[str, Any]] = {}
    worker_wages: list[float] = []
    engineer_wages: list[float] = []
    for company in companies:
        decision = dict(
            one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        )
        decision["worker_salary"] = min(max(float(decision["worker_salary"]), salary_min), salary_max)
        decision["engineer_salary"] = min(max(float(decision["engineer_salary"]), salary_min), salary_max)
        decisions[int(company["id"])] = decision
        worker_wages.append(decision["worker_salary"])
        engineer_wages.append(decision["engineer_salary"])
    average_worker_wage = sum(worker_wages) / len(worker_wages)
    average_engineer_wage = sum(engineer_wages) / len(engineer_wages)

    states: dict[int, dict[str, Any]] = {}
    market_base_averages: dict[str, float] = {
        str(market["city"]): reference_market_average(
            conn,
            str(market["city"]),
            round_no,
            float(market["initial_avg_price"]),
        )
        for market in markets
    }
    for company_row in companies:
        company = dict(company_row)
        company_id = int(company["id"])
        decision = decisions[company_id]
        home_row = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        if home_row is None:
            raise ValueError(f"{company['code']} 尚未设置有效主场。")
        home = dict(home_row)

        worker_delta = int(decision["worker_delta"])
        engineer_delta = int(decision["engineer_delta"])
        previous_workers = employee_count(conn, company_id, "worker")
        previous_engineers = employee_count(conn, company_id, "engineer")
        actual_worker_delta = max(worker_delta, -previous_workers)
        actual_engineer_delta = max(engineer_delta, -previous_engineers)
        if actual_worker_delta > 0:
            conn.execute(
                "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,?)",
                (company_id, "worker", actual_worker_delta, round_no),
            )
        elif actual_worker_delta < 0:
            remove_employees(conn, company_id, "worker", -actual_worker_delta)
        if actual_engineer_delta > 0:
            conn.execute(
                "INSERT INTO employee_cohorts(company_id,role,count,hire_round) VALUES(?,?,?,?)",
                (company_id, "engineer", actual_engineer_delta, round_no),
            )
        elif actual_engineer_delta < 0:
            remove_employees(conn, company_id, "engineer", -actual_engineer_delta)

        workers = employee_count(conn, company_id, "worker")
        engineers = employee_count(conn, company_id, "engineer")
        worker_multiplier = min(decision["worker_salary"] / max(average_worker_wage, 1.0), 1.10)
        engineer_multiplier = min(decision["engineer_salary"] / max(average_engineer_wage, 1.0), 1.10)
        effective_workers = effective_employee_count(conn, company_id, "worker", round_no) * worker_multiplier
        effective_engineers = effective_employee_count(conn, company_id, "engineer", round_no) * engineer_multiplier
        component_capacity = (504.0 / b) * (effective_workers / a) if a and b else 0.0
        engineer_product_capacity = (504.0 / d_hours) * (effective_engineers / c) if c and d_hours else 0.0
        component_product_capacity = component_capacity / components_per_product if components_per_product else 0.0
        planned = max(0, int(decision["production_volume"]))
        produced = max(0, math.floor(min(planned, engineer_product_capacity, component_product_capacity)))
        components = math.ceil(produced * components_per_product)

        debt = float(company["debt"])
        cash = float(company["cash"])
        requested_loan_change = float(decision["loan_change"])
        if requested_loan_change >= 0:
            actual_loan_change = min(requested_loan_change, max(0.0, float(home["max_loan"]) - debt))
            debt += actual_loan_change
            cash += actual_loan_change
        else:
            repayment = min(-requested_loan_change, debt, max(0.0, cash))
            actual_loan_change = -repayment
            debt -= repayment
            cash -= repayment

        wage_cost = workers * decision["worker_salary"] * 3 + engineers * decision["engineer_salary"] * 3
        layoff_cost = max(-actual_worker_delta, 0) * decision["worker_salary"] + max(-actual_engineer_delta, 0) * decision["engineer_salary"]
        training_cost = max(actual_worker_delta, 0) * float(home["worker_training_cost"]) + max(actual_engineer_delta, 0) * float(home["engineer_training_cost"])
        material_multiplier = patent_factor ** int(company["patents"])
        component_material_cost = components * float(home["component_material"]) * material_multiplier
        product_material_cost = produced * float(home["product_material"]) * material_multiplier
        old_products = int(company["product_inventory"])
        component_need = components
        product_need = old_products + produced
        component_storage_increase = max(0, component_need - int(company["component_storage_capacity"]))
        product_storage_increase = max(0, product_need - int(company["product_storage_capacity"]))
        storage_cost = component_storage_increase * float(home["component_storage"]) + product_storage_increase * float(home["product_storage"])
        conn.execute(
            "UPDATE companies SET component_storage_capacity=MAX(component_storage_capacity,?),"
            "product_storage_capacity=MAX(product_storage_capacity,?) WHERE id=?",
            (component_need, product_need, company_id),
        )

        city_decisions: dict[str, dict[str, Any]] = {}
        total_agent_cost = 0.0
        total_report_cost = 0.0
        total_marketing = 0.0
        for market_row in markets:
            market = dict(market_row)
            city = str(market["city"])
            city_decision_row = one(
                conn,
                "SELECT * FROM city_decisions WHERE company_id=? AND round_no=? AND city=?",
                (company_id, round_no, city),
            )
            city_decision = dict(city_decision_row) if city_decision_row else {
                "agent_delta": 0,
                "marketing_investment": 0.0,
                "price": market["initial_avg_price"],
                "order_report": 0,
            }
            old_agent_row = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company_id, city))
            old_agents = int(old_agent_row["count"]) if old_agent_row else 0
            new_agents = max(0, old_agents + int(city_decision["agent_delta"]))
            actual_agent_delta = new_agents - old_agents
            total_agent_cost += max(actual_agent_delta, 0) * agent_add_cost + max(-actual_agent_delta, 0) * agent_remove_cost
            conn.execute(
                "INSERT INTO agents(company_id,city,count) VALUES(?,?,?) "
                "ON CONFLICT(company_id,city) DO UPDATE SET count=excluded.count",
                (company_id, city, new_agents),
            )
            total_report_cost += report_cost_each if int(city_decision["order_report"]) else 0.0
            total_marketing += max(0.0, float(city_decision["marketing_investment"]))
            city_decision["agents_after"] = new_agents
            city_decision["price"] = min(
                max(float(city_decision["price"] or market["initial_avg_price"]), price_min),
                min(global_price_max, float(market["max_price"])),
            )
            city_decisions[city] = city_decision

        management = max(0.0, float(decision["management_investment"]))
        quality = max(0.0, float(decision["quality_investment"]))
        research = max(0.0, float(decision["research_investment"]))
        pre_sales_cost = (
            wage_cost
            + layoff_cost
            + training_cost
            + component_material_cost
            + product_material_cost
            + storage_cost
            + total_agent_cost
            + total_report_cost
            + total_marketing
            + management
            + quality
        )
        cash -= pre_sales_cost
        ma_index = management / max(workers + engineers, 1)
        qi_index = quality / max(old_products * 1.2 + produced, 1.0)
        states[company_id] = {
            "company": company,
            "decision": decision,
            "home": home,
            "workers": workers,
            "engineers": engineers,
            "effective_workers": effective_workers,
            "effective_engineers": effective_engineers,
            "worker_multiplier": worker_multiplier,
            "engineer_multiplier": engineer_multiplier,
            "produced": produced,
            "components": components,
            "available": old_products + produced,
            "old_products": old_products,
            "cash_pre_sales": cash,
            "debt": debt,
            "loan_change": actual_loan_change,
            "wage_cost": wage_cost,
            "layoff_cost": layoff_cost,
            "training_cost": training_cost,
            "component_material_cost": component_material_cost,
            "product_material_cost": product_material_cost,
            "storage_cost": storage_cost,
            "agent_cost": total_agent_cost,
            "report_cost": total_report_cost,
            "marketing_total": total_marketing,
            "management": management,
            "quality": quality,
            "research": research,
            "ma_index": ma_index,
            "qi_index": qi_index,
            "city_decisions": city_decisions,
            "city_sales": {str(m["city"]): 0.0 for m in markets},
            "city_secondary": {str(m["city"]): 0.0 for m in markets},
            "city_cpi": {str(m["city"]): 0.0 for m in markets},
            "city_cpi_units": {str(m["city"]): 0.0 for m in markets},
            "city_breakdown": {str(m["city"]): {} for m in markets},
        }

    for market_row in markets:
        market = dict(market_row)
        city = str(market["city"])
        size = market_size(market, round_no, growth)
        entries: list[dict[str, Any]] = []
        for company_id, state in states.items():
            city_decision = state["city_decisions"][city]
            if int(city_decision["agents_after"]) <= 0:
                continue
            entries.append(
                {
                    "company_id": company_id,
                    "ma_index": state["ma_index"],
                    "qi_index": state["qi_index"],
                    "mi_investment": max(0.0, float(city_decision["marketing_investment"])),
                    "price": float(city_decision["price"]),
                }
            )
        allocations = allocate_city_cpi(
            entries,
            market_size=size,
            max_price=float(market["max_price"]),
            ma_large_threshold=ma_large_threshold,
            price_power=price_power,
            average_price=market_base_averages[city],
            market_average_price=market_base_averages[city],
        )
        for allocation in allocations:
            company_id = int(allocation["company_id"])
            cpi = float(allocation["total_cpi"])
            cpi_units = size * cpi / 100.0
            states[company_id]["city_cpi"][city] = cpi
            states[company_id]["city_cpi_units"][city] = cpi_units
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
        remaining = {
            company_id: max(0.0, state["available"] - sum(state["city_sales"].values()))
            for company_id, state in states.items()
        }
        moved = 0.0
        for market_row in markets:
            market = dict(market_row)
            city = str(market["city"])
            size = market_size(market, round_no, growth)
            used = sum(state["city_sales"][city] for state in states.values())
            gap = max(0.0, size - used)
            candidates: list[tuple[int, float]] = []
            for company_id, state in states.items():
                if remaining[company_id] <= 0.5 or int(state["city_decisions"][city]["agents_after"]) <= 0:
                    continue
                score = max(float(state["city_cpi_units"][city]), size * 0.0001)
                candidates.append((company_id, score))
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

    market_round_stats: dict[str, dict[str, float]] = {}
    for market_row in markets:
        market = dict(market_row)
        city = str(market["city"])
        size = market_size(market, round_no, growth)
        pairs = [
            (
                float(state["city_decisions"][city]["price"]),
                float(max(0, math.floor(state["city_sales"][city]))),
            )
            for state in states.values()
        ]
        total_volume = sum(quantity for _, quantity in pairs)
        base_average = market_base_averages[city]
        average_price = weighted_market_average(base_average, size, pairs)
        market_round_stats[city] = {
            "base_average_price": base_average,
            "average_price": average_price,
            "market_size": size,
            "player_total_volume": total_volume,
        }
        conn.execute(
            "INSERT INTO market_round_stats(city,round_no,base_average_price,average_price,market_size,player_total_volume) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(city,round_no) DO UPDATE SET "
            "base_average_price=excluded.base_average_price,average_price=excluded.average_price,"
            "market_size=excluded.market_size,player_total_volume=excluded.player_total_volume",
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
            units = max(0, math.floor(state["city_sales"][city]))
            sold += units
            transport = float(market["transport_cost"]) if company["home_city"] != city else 0.0
            gross_revenue = units * float(city_decision["price"])
            transport_total = units * transport
            net_city_revenue = gross_revenue - transport_total
            revenue += net_city_revenue
            size = market_size(market, round_no, growth)
            share = units / max(size, 1.0)
            allocation = state["city_breakdown"][city]
            allocated_before_secondary = state["city_cpi_units"][city]
            secondary_units = float(state["city_secondary"][city])
            breakdown = allocation | {
                "market_size": size,
                "cpi_units": allocated_before_secondary,
                "secondary_units": secondary_units,
            } if allocation else {
                "market_size": size,
                "cpi_units": 0.0,
                "secondary_units": 0.0,
            }
            conn.execute(
                "INSERT INTO city_results(company_id,round_no,city,cpi,cpi_units,sold,revenue,price,marketing,market_share,breakdown_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    company_id,
                    round_no,
                    city,
                    float(state["city_cpi"][city]),
                    allocated_before_secondary,
                    units,
                    net_city_revenue,
                    float(city_decision["price"]),
                    float(city_decision["marketing_investment"]),
                    share,
                    json.dumps(breakdown, ensure_ascii=False),
                ),
            )
            city_report_rows.append(
                {
                    "city": city,
                    "agents": int(city_decision["agents_after"]),
                    "marketing": float(city_decision["marketing_investment"]),
                    "price": float(city_decision["price"]),
                    "cpi": float(state["city_cpi"][city]),
                    "cpi_units": allocated_before_secondary,
                    "secondary_units": secondary_units,
                    "sold": units,
                    "market_share": share,
                    "market_size": size,
                    "base_average_price": market_round_stats[city]["base_average_price"],
                    "market_average_price": market_round_stats[city]["average_price"],
                    "player_total_volume": market_round_stats[city]["player_total_volume"],
                    "transport": transport_total,
                    "breakdown": allocation,
                }
            )

        inventory = max(0, int(state["available"] - sold))
        interest = max(0.0, float(state["debt"])) * float(home["interest_rate"])
        cash = state["cash_pre_sales"] + revenue - state["research"] - interest
        operating_cost = (
            state["wage_cost"]
            + state["layoff_cost"]
            + state["training_cost"]
            + state["component_material_cost"]
            + state["product_material_cost"]
            + state["storage_cost"]
            + state["agent_cost"]
            + state["report_cost"]
            + state["marketing_total"]
            + state["management"]
            + state["quality"]
            + state["research"]
            + interest
        )
        pre_tax_profit = revenue - operating_cost
        tax = max(0.0, pre_tax_profit * tax_rate)
        cash -= tax
        total_cost = operating_cost + tax
        net_profit = revenue - total_cost
        probability = research_probability(state["research"], amount_25, amount_75)
        rng = random.Random(f"{round_no}:{company_id}:patent")
        research_success = 1 if rng.random() < probability else 0
        patents_after = int(company["patents"]) + research_success
        inventory_book_value = inventory * float(home["product_material"]) * (patent_factor ** patents_after)
        total_assets = cash + inventory_book_value
        # The supplied rules make Net Cash the winning metric; unsold inventory
        # remains visible in total assets but does not improve the ranking.
        net_assets = cash - float(state["debt"])
        report = {
            "key_metrics": {
                "total_assets": total_assets,
                "debt": state["debt"],
                "net_assets": net_assets,
                "sales_revenue": revenue,
                "cost": total_cost,
                "net_profit": net_profit,
                "inventory_book_value": inventory_book_value,
            },
            "finance": {
                "round_begins": company["cash"],
                "loan_change": state["loan_change"],
                "wages": state["wage_cost"],
                "layoff": state["layoff_cost"],
                "training": state["training_cost"],
                "materials": state["component_material_cost"] + state["product_material_cost"],
                "storage": state["storage_cost"],
                "agents": state["agent_cost"],
                "marketing": state["marketing_total"],
                "management": state["management"],
                "quality": state["quality"],
                "market_reports": state["report_cost"],
                "research": state["research"],
                "interest": interest,
                "tax": tax,
                "round_ends": cash,
            },
            "human_resources": {
                "workers": state["workers"],
                "engineers": state["engineers"],
                "effective_workers": state["effective_workers"],
                "effective_engineers": state["effective_engineers"],
                "worker_salary": state["decision"]["worker_salary"],
                "engineer_salary": state["decision"]["engineer_salary"],
                "worker_wage_multiplier": state["worker_multiplier"],
                "engineer_wage_multiplier": state["engineer_multiplier"],
            },
            "production": {
                "planned": state["decision"]["production_volume"],
                "produced": state["produced"],
                "old_products": state["old_products"],
                "sold": sold,
                "surplus": inventory,
                "ma_index": state["ma_index"],
                "qi_index": state["qi_index"],
            },
            "research": {
                "investment": state["research"],
                "probability": probability,
                "success": bool(research_success),
                "patents_after": patents_after,
            },
            "sales": city_report_rows,
            "cpi_algorithm": {
                "version": get_setting(conn, "cpi_algorithm_version", "cpi-generator-admin-v1", str),
                "description": "赠品 + 第一层 + 第二层 + 福利1/2；价格差按设定幂次分配 40 CPI；各城市独立计算。",
                "price_power": price_power,
            },
        }
        conn.execute(
            "INSERT INTO results(company_id,round_no,total_assets,debt,net_assets,cash,sales_revenue,total_cost,net_profit,"
            "produced,sold,inventory,ma_index,qi_index,research_success,report_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                company_id,
                round_no,
                total_assets,
                state["debt"],
                net_assets,
                cash,
                revenue,
                total_cost,
                net_profit,
                state["produced"],
                sold,
                inventory,
                state["ma_index"],
                state["qi_index"],
                research_success,
                json.dumps(report, ensure_ascii=False),
            ),
        )
        conn.execute(
            "UPDATE companies SET cash=?,debt=?,patents=?,product_inventory=? WHERE id=?",
            (cash, state["debt"], patents_after, inventory, company_id),
        )
    conn.execute("UPDATE rounds SET status='settled',settled_at=? WHERE round_no=?", (now_iso(), round_no))
