from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .cpi import allocate_city_cpi


FORECAST_API_VERSION = 1


def _weighted_player_average(
    price_quantity_pairs: Sequence[tuple[float, float]],
    fallback: float,
) -> float:
    pairs = [
        (max(0.0, float(price)), max(0.0, float(quantity)))
        for price, quantity in price_quantity_pairs
    ]
    total_quantity = sum(quantity for _, quantity in pairs)
    if total_quantity <= 0:
        return max(0.0, float(fallback))
    return sum(price * quantity for price, quantity in pairs) / total_quantity


def _integer_sales(city_sales: Mapping[str, float], available_units: int | float) -> dict[str, int]:
    """Mirror engine.allocate_integer_sales without importing the settlement module."""
    available = max(0, int(available_units))
    targets = {city: max(0.0, float(value)) for city, value in city_sales.items()}
    continuous_total = sum(targets.values())
    target_total = min(available, max(0, math.floor(continuous_total + 0.5 + 1e-9)))
    units = {city: max(0, math.floor(value + 1e-9)) for city, value in targets.items()}
    remaining = max(0, target_total - sum(units.values()))
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


def redistribute_secondary_sales(
    *,
    company_available: Mapping[int, float],
    city_primary_sales: Mapping[int, Mapping[str, float]],
    city_category_capacities: Mapping[int, Mapping[str, Mapping[str, float]]],
    city_order: Sequence[str],
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, dict[str, float]]]]:
    """Apply settlement's price/investment secondary allocation exactly.

    ``city_category_capacities[company][city]`` must contain ``price`` and
    ``investment`` visible capacities.  Unused price capacity can only move
    to a company with positive visible price capacity in that city; investment
    capacity follows the same isolated rule.  Inputs are copied and never
    mutated.
    """
    company_ids = list(company_available)
    sales = {
        company_id: {
            city: max(0.0, float(city_primary_sales.get(company_id, {}).get(city, 0.0)))
            for city in city_order
        }
        for company_id in company_ids
    }
    secondary = {
        company_id: {
            city: {"price": 0.0, "investment": 0.0}
            for city in city_order
        }
        for company_id in company_ids
    }
    pools = {city: {"price": 0.0, "investment": 0.0} for city in city_order}
    for city in city_order:
        for company_id in company_ids:
            categories = city_category_capacities.get(company_id, {}).get(city, {})
            price_capacity = max(0.0, float(categories.get("price", 0.0)))
            investment_capacity = max(0.0, float(categories.get("investment", 0.0)))
            total_capacity = price_capacity + investment_capacity
            used_ratio = (
                min(1.0, sales[company_id][city] / total_capacity)
                if total_capacity > 0 else 0.0
            )
            pools[city]["price"] += price_capacity * (1.0 - used_ratio)
            pools[city]["investment"] += investment_capacity * (1.0 - used_ratio)

    for _ in range(max(1, len(company_ids) + len(city_order))):
        remaining = {
            company_id: max(
                0.0,
                float(company_available[company_id]) - sum(sales[company_id].values()),
            )
            for company_id in company_ids
        }
        moved = 0.0
        for city in city_order:
            for category in ("price", "investment"):
                pool = pools[city][category]
                candidates = [
                    (
                        company_id,
                        max(
                            0.0,
                            float(
                                city_category_capacities
                                .get(company_id, {})
                                .get(city, {})
                                .get(category, 0.0)
                            ),
                        ),
                    )
                    for company_id in company_ids
                    if remaining[company_id] > 1e-9
                    and float(
                        city_category_capacities
                        .get(company_id, {})
                        .get(city, {})
                        .get(category, 0.0)
                    ) > 1e-9
                ]
                if pool <= 1e-9 or not candidates:
                    continue
                score_sum = sum(score for _, score in candidates)
                category_moved = 0.0
                for company_id, score in candidates:
                    addition = min(remaining[company_id], pool * score / score_sum)
                    sales[company_id][city] += addition
                    secondary[company_id][city][category] += addition
                    remaining[company_id] -= addition
                    category_moved += addition
                pools[city][category] = max(0.0, pool - category_moved)
                moved += category_moved
        if moved <= 1e-9:
            break
    return sales, secondary


def forecast_market_sales(
    *,
    players: Sequence[Mapping[str, Any]],
    markets: Sequence[Mapping[str, Any]],
    ma_large_threshold: float,
    price_power: int,
    max_iterations: int = 25,
    average_tolerance: float = 0.005,
) -> dict[str, Any]:
    """Forecast settlement CPI, primary sales and secondary sales read-only.

    The caller must resolve every live KDS value before calling this function.
    In particular, ``price_power`` is required and has no default, preventing a
    custom KDS from silently falling back to exponent 8.

    Player shape::

        {"company_id": 1, "available": 1000, "ma_index": 1300,
         "qi_index": 500, "cities": {"A": {"agents": 1,
         "marketing": 10000, "price": 8000}}}

    Market shape::

        {"city": "A", "market_size": 10000, "max_price": 25000,
         "base_average_price": 9800}

    The function performs no SQL and mutates neither argument.
    """
    resolved_power = max(1, int(price_power))
    resolved_markets = [dict(market) for market in markets]
    city_order = [str(market["city"]) for market in resolved_markets]
    if len(city_order) != len(set(city_order)):
        raise ValueError("markets must contain each city exactly once")

    states: dict[int, dict[str, Any]] = {}
    for raw_player in players:
        company_id = int(raw_player["company_id"])
        if company_id in states:
            raise ValueError(f"duplicate company_id: {company_id}")
        raw_cities = raw_player.get("cities", {})
        states[company_id] = {
            "available": max(0.0, float(raw_player.get("available", 0.0))),
            "ma_index": max(0.0, float(raw_player.get("ma_index", 0.0))),
            "qi_index": max(0.0, float(raw_player.get("qi_index", 0.0))),
            "cities": {
                city: {
                    "agents": max(0, int(raw_cities.get(city, {}).get("agents", 0))),
                    "marketing": max(0.0, float(raw_cities.get(city, {}).get("marketing", 0.0))),
                    "price": max(0.0, float(raw_cities.get(city, {}).get("price", 0.0))),
                }
                for city in city_order
            },
        }

    base_averages = {
        str(market["city"]): max(0.0, float(market["base_average_price"]))
        for market in resolved_markets
    }
    market_sizes = {
        str(market["city"]): max(0.0, float(market["market_size"]))
        for market in resolved_markets
    }
    player_averages: dict[str, float] = {}
    for city in city_order:
        active = [
            (state["cities"][city]["price"], state["available"])
            for state in states.values()
            if state["cities"][city]["agents"] > 0
        ]
        active_prices = [price for price, _ in active]
        fallback = (
            sum(active_prices) / len(active_prices)
            if active_prices else base_averages[city]
        )
        player_averages[city] = _weighted_player_average(active, fallback)

    def calculate_once(averages: Mapping[str, float]) -> dict[int, dict[str, Any]]:
        result = {
            company_id: {
                "visible": {city: 0.0 for city in city_order},
                "price": {city: 0.0 for city in city_order},
                "investment": {city: 0.0 for city in city_order},
                "breakdown": {city: {} for city in city_order},
                "primary": {city: 0.0 for city in city_order},
            }
            for company_id in states
        }
        for market in resolved_markets:
            city = str(market["city"])
            entries = [
                {
                    "company_id": company_id,
                    "ma_index": state["ma_index"],
                    "qi_index": state["qi_index"],
                    "mi_investment": state["cities"][city]["marketing"],
                    "price": state["cities"][city]["price"],
                    "agents": state["cities"][city]["agents"],
                }
                for company_id, state in states.items()
                if state["cities"][city]["agents"] > 0
            ]
            allocations = allocate_city_cpi(
                entries,
                market_size=market_sizes[city],
                max_price=float(market["max_price"]),
                ma_large_threshold=float(ma_large_threshold),
                price_power=resolved_power,
                average_price=float(averages[city]),
                market_average_price=base_averages[city],
            )
            for allocation in allocations:
                company_id = int(allocation["company_id"])
                visible = market_sizes[city] * float(allocation["total_cpi"]) / 100.0
                price = market_sizes[city] * float(allocation["price_cpi"]) / 100.0
                investment = market_sizes[city] * (
                    float(allocation["ma_cpi"])
                    + float(allocation["qi_cpi"])
                    + float(allocation["mi_cpi"])
                ) / 100.0
                result[company_id]["visible"][city] = visible
                result[company_id]["price"][city] = price
                result[company_id]["investment"][city] = investment
                result[company_id]["breakdown"][city] = dict(allocation)

        for company_id, state in states.items():
            total_capacity = sum(result[company_id]["visible"].values())
            factor = (
                min(1.0, state["available"] / total_capacity)
                if total_capacity > 0 and state["available"] > 0 else 0.0
            )
            for city in city_order:
                result[company_id]["primary"][city] = (
                    result[company_id]["visible"][city] * factor
                )

        final_sales, secondary = redistribute_secondary_sales(
            company_available={
                company_id: state["available"]
                for company_id, state in states.items()
            },
            city_primary_sales={
                company_id: data["primary"]
                for company_id, data in result.items()
            },
            city_category_capacities={
                company_id: {
                    city: {
                        "price": data["price"][city],
                        "investment": data["investment"][city],
                    }
                    for city in city_order
                }
                for company_id, data in result.items()
            },
            city_order=city_order,
        )
        for company_id, state in states.items():
            result[company_id]["sales"] = final_sales[company_id]
            result[company_id]["secondary_by_category"] = secondary[company_id]
            result[company_id]["secondary"] = {
                city: sum(secondary[company_id][city].values())
                for city in city_order
            }
            result[company_id]["sold_units"] = _integer_sales(
                final_sales[company_id], state["available"]
            )
        return result

    calculated = calculate_once(player_averages)
    for _ in range(max(1, int(max_iterations))):
        next_averages = {}
        for city in city_order:
            sold_pairs = [
                (
                    state["cities"][city]["price"],
                    calculated[company_id]["sold_units"][city],
                )
                for company_id, state in states.items()
            ]
            next_averages[city] = _weighted_player_average(
                sold_pairs, player_averages[city]
            )
        if all(
            math.isclose(
                next_averages[city],
                player_averages[city],
                abs_tol=float(average_tolerance),
            )
            for city in city_order
        ):
            player_averages = next_averages
            calculated = calculate_once(player_averages)
            break
        player_averages = next_averages
        calculated = calculate_once(player_averages)

    companies: dict[int, dict[str, Any]] = {}
    for company_id, data in calculated.items():
        companies[company_id] = {
            **data,
            "visible_total": sum(data["visible"].values()),
            "primary_total": sum(data["primary"].values()),
            "secondary_total": sum(data["secondary"].values()),
            "sold_total": sum(data["sold_units"].values()),
            "available": states[company_id]["available"],
        }
    return {
        "companies": companies,
        "player_average_prices": player_averages,
        "price_power": resolved_power,
    }
