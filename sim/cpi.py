from __future__ import annotations

from typing import Any


GIFT_CPI = 0.01
LAYER1_TOTAL_CPI = 5.0
LAYER2_TOTAL_CPI = 10.0
WELFARE_PART1_BASE = 1.5
WELFARE_PART2_BASE = 3.5
PRICE_CPI_TOTAL = 40.0


def minimum_threshold(large_threshold: float) -> float:
    """Exact helper used by the supplied CPI generator: large / 5 / 100."""
    return large_threshold / 500.0 if large_threshold > 0 else 0.0


def allocate_index_cpi(
    min_threshold: float,
    large_threshold: float,
    investments: list[float],
    prices: list[float],
    average_price: float,
) -> list[dict[str, Any]]:
    """Python port of calculateCPIAlgorithm from the supplied admin.js.

    The intentionally unusual adjusted-threshold comparisons are retained so
    the Streamlit settlement produces the same results as the source simulator.
    """
    max_price_factor = 1.0
    players: list[dict[str, Any]] = []
    for index, raw_investment in enumerate(investments):
        investment = max(0.0, float(raw_investment))
        price = float(prices[index]) if index < len(prices) else 0.0
        price_factor = average_price / price if average_price > 0 and price > 0 else 1.0
        max_price_factor = max(max_price_factor, price_factor or 1.0)
        players.append(
            {
                "player_index": index + 1,
                "original": investment,
                "price": price,
                "price_factor": price_factor,
                "adjusted": investment * price_factor,
            }
        )

    adjusted_min = min_threshold * max_price_factor
    adjusted_large = large_threshold * max_price_factor
    layer2_threshold = large_threshold
    gift_total = 0.0
    for player in players:
        player["below_min_adjusted"] = player["adjusted"] < adjusted_min
        player["above_large_adjusted"] = player["adjusted"] >= layer2_threshold
        player["below_min_original"] = player["original"] < min_threshold
        player["above_large_original"] = player["original"] >= large_threshold
        player["gift"] = GIFT_CPI if player["below_min_adjusted"] else 0.0
        gift_total += player["gift"]

    layer1_adjusted: list[float] = []
    layer1_original: list[float] = []
    layer1_total_adjusted = 0.0
    layer1_total_original = 0.0
    layer1_max_original = 0.0
    for player in players:
        adjusted_value = (
            player["adjusted"]
            if player["below_min_adjusted"]
            else min(player["adjusted"], adjusted_large)
        )
        layer1_adjusted.append(adjusted_value)
        layer1_total_adjusted += adjusted_value

        original_value = 0.0
        if not player["below_min_original"]:
            original_value = min(player["original"], large_threshold)
            layer1_total_original += original_value
            layer1_max_original = max(layer1_max_original, original_value)
        layer1_original.append(original_value)

    has_above_large = any(player["above_large_adjusted"] for player in players)
    layer1_max_adjusted = max(layer1_adjusted, default=0.0)
    layer1_available = LAYER1_TOTAL_CPI
    if not has_above_large and adjusted_large > 0 and layer1_max_adjusted > 0:
        layer1_available *= layer1_max_adjusted / adjusted_large
    for index, player in enumerate(players):
        player["layer1"] = (
            layer1_adjusted[index] / layer1_total_adjusted * layer1_available
            if layer1_total_adjusted > 0 and layer1_adjusted[index] > 0
            else 0.0
        )

    layer2_adjusted: list[float] = []
    layer2_original: list[float] = []
    layer2_total_adjusted = 0.0
    layer2_total_original = 0.0
    for player in players:
        adjusted_value = 0.0
        if not player["below_min_adjusted"] and player["above_large_adjusted"]:
            remaining = max(player["adjusted"] - layer2_threshold, 0.0)
            base_cap = min(remaining, layer2_threshold * 3.0)
            extra = max(remaining - base_cap, 0.0)
            adjusted_value = base_cap + extra * 0.1
        layer2_adjusted.append(adjusted_value)
        layer2_total_adjusted += adjusted_value

        original_value = 0.0
        if not player["below_min_original"] and player["above_large_original"]:
            remaining = max(player["original"] - large_threshold, 0.0)
            base_cap = min(remaining, large_threshold * 3.0)
            extra = max(remaining - base_cap, 0.0)
            original_value = base_cap + extra * 0.1
        layer2_original.append(original_value)
        layer2_total_original += original_value

    for index, player in enumerate(players):
        player["layer2"] = (
            layer2_adjusted[index] / layer2_total_adjusted * LAYER2_TOTAL_CPI
            if layer2_total_adjusted > 0 and layer2_adjusted[index] > 0
            else 0.0
        )

    welfare_total = WELFARE_PART1_BASE + WELFARE_PART2_BASE
    welfare_ratio = max(0.0, welfare_total - gift_total) / welfare_total if welfare_total else 0.0
    welfare1_base = WELFARE_PART1_BASE * welfare_ratio
    welfare2_available = WELFARE_PART2_BASE * welfare_ratio
    welfare1_available = (
        welfare1_base * layer1_max_original / large_threshold
        if large_threshold > 0 and layer1_max_original > 0
        else 0.0
    )
    for index, player in enumerate(players):
        player["welfare1"] = (
            layer1_original[index] / layer1_total_original * welfare1_available
            if not player["below_min_original"] and layer1_total_original > 0 and welfare1_available > 0
            else 0.0
        )
        player["welfare2"] = (
            layer2_original[index] / layer2_total_original * welfare2_available
            if player["above_large_original"] and layer2_total_original > 0 and welfare2_available > 0
            else 0.0
        )

    results: list[dict[str, Any]] = []
    for player in players:
        breakdown = {
            "gift_cpi": player["gift"],
            "layer1_cpi": player["layer1"],
            "layer2_cpi": player["layer2"],
            "welfare1_cpi": player["welfare1"],
            "welfare2_cpi": player["welfare2"],
        }
        results.append(
            {
                "player_index": player["player_index"],
                "investment": player["original"],
                "price": player["price"],
                "price_factor": player["price_factor"],
                "cpi": sum(breakdown.values()),
                "breakdown": breakdown,
            }
        )
    return results


def allocate_city_cpi(
    entries: list[dict[str, Any]],
    *,
    market_size: float,
    max_price: float,
    ma_large_threshold: float,
    price_power: int = 8,
    average_price: float | None = None,
    market_average_price: float | None = None,
) -> list[dict[str, Any]]:
    """Apply the supplied admin simulator independently inside one city."""
    if not entries:
        return []
    prices = [max(0.0, float(entry["price"])) for entry in entries]
    current_average = sum(prices) / len(prices) if prices else 0.0
    average_price = current_average if average_price is None else float(average_price)
    market_average_price = current_average if market_average_price is None else float(market_average_price)

    qi_large = max(0.0, max_price / 50.0)
    qi_min = minimum_threshold(qi_large)
    ma_large = max(0.0, float(ma_large_threshold))
    ma_min = minimum_threshold(ma_large)
    mi_large = qi_large * market_size * 0.20
    mi_min = qi_min * market_size * 0.20

    qi_results = allocate_index_cpi(qi_min, qi_large, [float(e["qi_index"]) for e in entries], prices, average_price)
    ma_results = allocate_index_cpi(ma_min, ma_large, [float(e["ma_index"]) for e in entries], prices, average_price)
    mi_results = allocate_index_cpi(mi_min, mi_large, [float(e["mi_investment"]) for e in entries], prices, average_price)

    price_cpis = [0.0] * len(entries)
    eligible: list[tuple[int, float]] = []
    for index, price in enumerate(prices):
        if price > 0 and price <= market_average_price:
            difference = market_average_price - price
            eligible.append((index, difference ** max(1, int(price_power))))
    denominator = sum(weight for _, weight in eligible)
    if denominator > 0:
        for index, weight in eligible:
            price_cpis[index] = PRICE_CPI_TOTAL * weight / denominator

    output: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        total = qi_results[index]["cpi"] + ma_results[index]["cpi"] + mi_results[index]["cpi"] + price_cpis[index]
        output.append(
            {
                "company_id": int(entry["company_id"]),
                "price": prices[index],
                "ma_cpi": ma_results[index]["cpi"],
                "qi_cpi": qi_results[index]["cpi"],
                "mi_cpi": mi_results[index]["cpi"],
                "price_cpi": price_cpis[index],
                "total_cpi": total,
                "breakdown": {
                    "ma": ma_results[index]["breakdown"],
                    "qi": qi_results[index]["breakdown"],
                    "mi": mi_results[index]["breakdown"],
                },
                "thresholds": {
                    "qi_min": qi_min,
                    "qi_large": qi_large,
                    "ma_min": ma_min,
                    "ma_large": ma_large,
                    "mi_min": mi_min,
                    "mi_large": mi_large,
                },
                "average_price": average_price,
                "market_average_price": market_average_price,
            }
        )
    return output
