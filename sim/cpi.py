from __future__ import annotations

from typing import Any


CPI_API_VERSION = 3
GIFT_CPI = 0.01
LAYER1_TOTAL_CPI = 5.0
LAYER2_TOTAL_CPI = 10.0
WELFARE_PART1_BASE = 1.5
WELFARE_PART2_BASE = 3.5
PRICE_CPI_TOTAL = 40.0
INDEX_CPI_TOTAL = LAYER1_TOTAL_CPI + LAYER2_TOTAL_CPI + WELFARE_PART1_BASE + WELFARE_PART2_BASE


def agent_mi_benefit(agent_count: int | float) -> float:
    """Every sales agent increases the effectiveness of MI by 10%."""
    return 1.0 + max(0.0, float(agent_count)) * 0.10


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
        # A zero investment must contribute exactly zero CPI. The 0.01 gift is
        # only for a positive investment that misses the minimum threshold.
        player["gift"] = GIFT_CPI if player["original"] > 0 and player["below_min_adjusted"] else 0.0
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
    # Defensive cap: each MA/QI/MI pool is strictly limited to 20 CPI even
    # under unusually large player counts or floating-point accumulation.
    allocated_total = sum(float(result["cpi"]) for result in results)
    if allocated_total > INDEX_CPI_TOTAL:
        scale = INDEX_CPI_TOTAL / allocated_total
        for result in results:
            result["breakdown"] = {
                key: float(value) * scale for key, value in result["breakdown"].items()
            }
            result["cpi"] = sum(result["breakdown"].values())
    return results


def _allocate_index_cpi_for_target(
    min_threshold: float,
    large_threshold: float,
    investments: list[float],
    prices: list[float],
    average_price: float,
    target_index: int,
) -> float:
    """Return one player's index CPI without building every breakdown.

    This follows :func:`allocate_index_cpi` operation-for-operation. It is
    used by the Super Bot search, where complete rival breakdown dictionaries
    were previously created thousands of times and immediately discarded.
    """
    originals = [max(0.0, float(value)) for value in investments]
    resolved_prices = [
        float(prices[index]) if index < len(prices) else 0.0
        for index in range(len(originals))
    ]
    factors = [
        average_price / price if average_price > 0 and price > 0 else 1.0
        for price in resolved_prices
    ]
    adjusted = [value * factors[index] for index, value in enumerate(originals)]
    max_price_factor = max([1.0, *factors])
    adjusted_min = min_threshold * max_price_factor
    adjusted_large = large_threshold * max_price_factor

    below_min_adjusted = [value < adjusted_min for value in adjusted]
    above_large_adjusted = [value >= large_threshold for value in adjusted]
    below_min_original = [value < min_threshold for value in originals]
    above_large_original = [value >= large_threshold for value in originals]
    gifts = [
        GIFT_CPI if originals[index] > 0 and below_min_adjusted[index] else 0.0
        for index in range(len(originals))
    ]
    gift_total = sum(gifts)

    layer1_adjusted = [
        adjusted[index]
        if below_min_adjusted[index]
        else min(adjusted[index], adjusted_large)
        for index in range(len(originals))
    ]
    layer1_total_adjusted = sum(layer1_adjusted)
    layer1_original = [
        0.0 if below_min_original[index] else min(originals[index], large_threshold)
        for index in range(len(originals))
    ]
    layer1_total_original = sum(layer1_original)
    layer1_max_original = max(layer1_original, default=0.0)
    layer1_max_adjusted = max(layer1_adjusted, default=0.0)
    layer1_available = LAYER1_TOTAL_CPI
    if not any(above_large_adjusted) and adjusted_large > 0 and layer1_max_adjusted > 0:
        layer1_available *= layer1_max_adjusted / adjusted_large

    layer2_adjusted: list[float] = []
    layer2_original: list[float] = []
    for index in range(len(originals)):
        adjusted_value = 0.0
        if not below_min_adjusted[index] and above_large_adjusted[index]:
            remaining = max(adjusted[index] - large_threshold, 0.0)
            base_cap = min(remaining, large_threshold * 3.0)
            adjusted_value = base_cap + max(remaining - base_cap, 0.0) * 0.1
        layer2_adjusted.append(adjusted_value)

        original_value = 0.0
        if not below_min_original[index] and above_large_original[index]:
            remaining = max(originals[index] - large_threshold, 0.0)
            base_cap = min(remaining, large_threshold * 3.0)
            original_value = base_cap + max(remaining - base_cap, 0.0) * 0.1
        layer2_original.append(original_value)
    layer2_total_adjusted = sum(layer2_adjusted)
    layer2_total_original = sum(layer2_original)

    welfare_total = WELFARE_PART1_BASE + WELFARE_PART2_BASE
    welfare_ratio = max(0.0, welfare_total - gift_total) / welfare_total if welfare_total else 0.0
    welfare1_available = (
        WELFARE_PART1_BASE * welfare_ratio * layer1_max_original / large_threshold
        if large_threshold > 0 and layer1_max_original > 0 else 0.0
    )
    welfare2_available = WELFARE_PART2_BASE * welfare_ratio

    target = target_index
    target_total = gifts[target]
    if layer1_total_adjusted > 0 and layer1_adjusted[target] > 0:
        target_total += layer1_adjusted[target] / layer1_total_adjusted * layer1_available
    if layer2_total_adjusted > 0 and layer2_adjusted[target] > 0:
        target_total += layer2_adjusted[target] / layer2_total_adjusted * LAYER2_TOTAL_CPI
    if not below_min_original[target] and layer1_total_original > 0 and welfare1_available > 0:
        target_total += layer1_original[target] / layer1_total_original * welfare1_available
    if above_large_original[target] and layer2_total_original > 0 and welfare2_available > 0:
        target_total += layer2_original[target] / layer2_total_original * welfare2_available

    allocated_total = gift_total
    if layer1_total_adjusted > 0:
        allocated_total += layer1_available
    if layer2_total_adjusted > 0:
        allocated_total += LAYER2_TOTAL_CPI
    if layer1_total_original > 0 and welfare1_available > 0:
        allocated_total += welfare1_available
    if layer2_total_original > 0 and welfare2_available > 0:
        allocated_total += welfare2_available
    if allocated_total > INDEX_CPI_TOTAL:
        target_total *= INDEX_CPI_TOTAL / allocated_total
    return target_total


def allocate_city_cpi_for_company(
    entries: list[dict[str, Any]],
    *,
    target_company_id: int,
    market_size: float,
    max_price: float,
    ma_large_threshold: float,
    price_power: int = 8,
    average_price: float | None = None,
    market_average_price: float | None = None,
) -> float:
    """Return only one company's total CPI using the canonical city formula."""
    if not entries:
        return 0.0
    target_index = next(
        (index for index, entry in enumerate(entries) if int(entry["company_id"]) == target_company_id),
        None,
    )
    if target_index is None:
        return 0.0

    prices = [max(0.0, float(entry["price"])) for entry in entries]
    current_average = sum(prices) / len(prices) if prices else 0.0
    average_price = current_average if average_price is None else float(average_price)
    market_average_price = current_average if market_average_price is None else float(market_average_price)
    qi_large = max(0.0, max_price / 50.0)
    ma_large = max(0.0, float(ma_large_threshold))
    mi_large = qi_large * market_size * 0.20 / 1.5 / 2.0

    qi_cpi = _allocate_index_cpi_for_target(
        minimum_threshold(qi_large), qi_large,
        [float(entry["qi_index"]) for entry in entries], prices, average_price, target_index,
    )
    ma_cpi = _allocate_index_cpi_for_target(
        minimum_threshold(ma_large), ma_large,
        [float(entry["ma_index"]) for entry in entries], prices, average_price, target_index,
    )
    effective_mi = [
        float(entry["mi_investment"]) * agent_mi_benefit(entry.get("agents", 0))
        for entry in entries
    ]
    mi_cpi = _allocate_index_cpi_for_target(
        minimum_threshold(mi_large), mi_large, effective_mi, prices, average_price, target_index,
    )

    price_weights = [0.0] * len(entries)
    for index, price in enumerate(prices):
        if price > 0 and price <= market_average_price:
            price_weights[index] = (market_average_price - price) ** max(1, int(price_power))
    price_denominator = sum(price_weights)
    price_cpi = (
        PRICE_CPI_TOTAL * price_weights[target_index] / price_denominator
        if price_denominator > 0 else 0.0
    )
    return qi_cpi + ma_cpi + mi_cpi + price_cpi


class _PreparedIndexCPI:
    """Invariant rival work for repeated evaluations of one index pool."""

    def __init__(self, originals: list[float], large: float, target: int):
        self.large = large
        self.minimum = minimum_threshold(large)
        self.target = target
        self.originals = [max(0.0, value) for value in originals]
        self.layer1_original = [
            min(value, large) if value >= self.minimum else 0.0
            for value in self.originals
        ]
        self.layer2_original = [self._layer2(value) for value in self.originals]
        self.max_rival_original = max(
            (value for index, value in enumerate(self.layer1_original) if index != target),
            default=0.0,
        )

    def _layer2(self, value: float) -> float:
        if value < self.minimum or value < self.large:
            return 0.0
        remaining = max(value - self.large, 0.0)
        base_cap = min(remaining, self.large * 3.0)
        return base_cap + max(remaining - base_cap, 0.0) * 0.1

    def evaluate(self, original: float, factors: list[float], max_factor: float) -> float:
        original = max(0.0, float(original))
        # A zero investment receives none of this pool, regardless of rivals.
        if original == 0.0:
            return 0.0
        target = self.target
        large = self.large
        minimum = self.minimum
        adjusted_min = minimum * max_factor
        adjusted_large = large * max_factor
        originals = self.originals.copy()
        originals[target] = original
        layer1: list[float] = []
        layer2: list[float] = []
        gifts: list[float] = []
        has_above_large = False
        max_layer1 = 0.0
        for value, factor in zip(originals, factors):
            adjusted = value * factor
            below_min = adjusted < adjusted_min
            above_large = adjusted >= large
            has_above_large = has_above_large or above_large
            gifts.append(GIFT_CPI if value > 0 and below_min else 0.0)
            first = adjusted if below_min else min(adjusted, adjusted_large)
            layer1.append(first)
            max_layer1 = max(max_layer1, first)
            second = 0.0
            if not below_min and above_large:
                remaining = max(adjusted - large, 0.0)
                base_cap = min(remaining, large * 3.0)
                second = base_cap + max(remaining - base_cap, 0.0) * 0.1
            layer2.append(second)

        layer1_total = sum(layer1)
        layer2_total = sum(layer2)
        layer1_available = LAYER1_TOTAL_CPI
        if not has_above_large and adjusted_large > 0 and max_layer1 > 0:
            layer1_available *= max_layer1 / adjusted_large
        original_first = min(original, large) if original >= minimum else 0.0
        original_second = self._layer2(original)
        first_originals = self.layer1_original.copy()
        first_originals[target] = original_first
        second_originals = self.layer2_original.copy()
        second_originals[target] = original_second
        first_total = sum(first_originals)
        second_total = sum(second_originals)
        max_original = max(self.max_rival_original, original_first)
        gift_total = sum(gifts)
        welfare_total = WELFARE_PART1_BASE + WELFARE_PART2_BASE
        welfare_ratio = max(0.0, welfare_total - gift_total) / welfare_total if welfare_total else 0.0
        welfare1_available = (
            WELFARE_PART1_BASE * welfare_ratio * max_original / large
            if large > 0 and max_original > 0 else 0.0
        )
        welfare2_available = WELFARE_PART2_BASE * welfare_ratio
        result = gifts[target]
        if layer1_total > 0 and layer1[target] > 0:
            result += layer1[target] / layer1_total * layer1_available
        if layer2_total > 0 and layer2[target] > 0:
            result += layer2[target] / layer2_total * LAYER2_TOTAL_CPI
        if original >= minimum and first_total > 0 and welfare1_available > 0:
            result += original_first / first_total * welfare1_available
        if original >= large and second_total > 0 and welfare2_available > 0:
            result += original_second / second_total * welfare2_available
        allocated_total = gift_total
        if layer1_total > 0:
            allocated_total += layer1_available
        if layer2_total > 0:
            allocated_total += LAYER2_TOTAL_CPI
        if first_total > 0 and welfare1_available > 0:
            allocated_total += welfare1_available
        if second_total > 0 and welfare2_available > 0:
            allocated_total += welfare2_available
        if allocated_total > INDEX_CPI_TOTAL:
            result *= INDEX_CPI_TOTAL / allocated_total
        return result


class PreparedCityCPI:
    """Reusable exact target evaluator while the other companies stay fixed.

    Settlement continues to use ``allocate_city_cpi``. This only avoids
    rebuilding rival inputs and original-investment layers during bot search.
    All sums retain their original entry order, including on Python 3.12+.
    """

    def __init__(
        self, entries: list[dict[str, Any]], *, target_company_id: int,
        market_size: float, max_price: float, ma_large_threshold: float,
        price_power: int = 8, market_average_price: float | None = None,
    ):
        self.target = next(
            (i for i, entry in enumerate(entries) if int(entry["company_id"]) == target_company_id),
            None,
        )
        self.prices = [max(0.0, float(entry["price"])) for entry in entries]
        self.market_average = market_average_price
        self.power = max(1, int(price_power))
        if self.target is None:
            return
        self.agent_benefit = agent_mi_benefit(entries[self.target].get("agents", 0))
        qi_large = max(0.0, max_price / 50.0)
        mi_large = qi_large * market_size * 0.20 / 1.5 / 2.0
        self.qi = _PreparedIndexCPI([float(e["qi_index"]) for e in entries], qi_large, self.target)
        self.ma = _PreparedIndexCPI([float(e["ma_index"]) for e in entries], max(0.0, float(ma_large_threshold)), self.target)
        self.mi = _PreparedIndexCPI([
            float(e["mi_investment"]) * agent_mi_benefit(e.get("agents", 0)) for e in entries
        ], mi_large, self.target)
        self.price_weights = self._price_weights(float(market_average_price)) if market_average_price is not None else []

    def _price_weights(self, average: float) -> list[float]:
        return [(average - price) ** self.power if price > 0 and price <= average else 0.0 for price in self.prices]

    def evaluate(
        self, *, ma_index: float, qi_index: float, mi_investment: float,
        price: float, average_price: float | None = None, agents: int | float | None = None,
    ) -> float:
        if self.target is None:
            return 0.0
        target = self.target
        price = max(0.0, float(price))
        prices = self.prices.copy()
        prices[target] = price
        average = sum(prices) / len(prices) if average_price is None else float(average_price)
        market_average = sum(prices) / len(prices) if self.market_average is None else float(self.market_average)
        factors = [average / value if average > 0 and value > 0 else 1.0 for value in prices]
        max_factor = max([1.0, *factors])
        qi_cpi = self.qi.evaluate(qi_index, factors, max_factor)
        ma_cpi = self.ma.evaluate(ma_index, factors, max_factor)
        benefit = self.agent_benefit if agents is None else agent_mi_benefit(agents)
        mi_cpi = self.mi.evaluate(float(mi_investment) * benefit, factors, max_factor)
        price_cpi = 0.0
        if price > 0 and price <= market_average:
            weights = self.price_weights.copy() if self.market_average is not None else [
                (market_average - value) ** self.power if value > 0 and value <= market_average else 0.0
                for value in prices
            ]
            weights[target] = (market_average - price) ** self.power
            denominator = sum(weights)
            if denominator > 0:
                price_cpi = PRICE_CPI_TOTAL * weights[target] / denominator
        return qi_cpi + ma_cpi + mi_cpi + price_cpi


def prepare_city_cpi_for_company(
    entries: list[dict[str, Any]], *, target_company_id: int,
    market_size: float, max_price: float, ma_large_threshold: float,
    price_power: int = 8, market_average_price: float | None = None,
) -> PreparedCityCPI:
    return PreparedCityCPI(
        entries, target_company_id=target_company_id, market_size=market_size,
        max_price=max_price, ma_large_threshold=ma_large_threshold,
        price_power=price_power, market_average_price=market_average_price,
    )


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
    # The large-MI base is QI threshold × market size × 20% ÷ 1.5 ÷ 2.
    # Player-specific thresholds are lower when the player has more agents;
    # multiplying MI by the agent benefit before allocation is algebraically
    # equivalent and keeps one common comparison scale for all players.
    mi_large_base = qi_large * market_size * 0.20 / 1.5 / 2.0
    mi_min_base = minimum_threshold(mi_large_base)

    qi_results = allocate_index_cpi(qi_min, qi_large, [float(e["qi_index"]) for e in entries], prices, average_price)
    ma_results = allocate_index_cpi(ma_min, ma_large, [float(e["ma_index"]) for e in entries], prices, average_price)
    agent_benefits = [agent_mi_benefit(e.get("agents", 0)) for e in entries]
    effective_mi = [float(entry["mi_investment"]) * agent_benefits[index] for index, entry in enumerate(entries)]
    mi_results = allocate_index_cpi(mi_min_base, mi_large_base, effective_mi, prices, average_price)

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
                    "mi_min": mi_min_base / agent_benefits[index],
                    "mi_large": mi_large_base / agent_benefits[index],
                    "mi_base_min": mi_min_base,
                    "mi_base_large": mi_large_base,
                    "mi_agent_benefit": agent_benefits[index],
                },
                "average_price": average_price,
                "market_average_price": market_average_price,
            }
        )
    return output
