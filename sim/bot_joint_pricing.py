"""Synchronous, KDS-aware price planning for a group of Super Bots.

This module deliberately has no database or bot-engine dependencies.  A caller
reads the current KDS, builds :class:`JointPriceTarget` values, and applies all
returned prices together.  Solving the prices as one system avoids the price
ladder caused by submitting one bot and then letting the next bot undercut it.

``target_share`` and ``share_cap`` are fractions of the 40-CPI price pool, not
percentages and not total CPI.  For example, ``0.20`` requests 8 price CPI.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Hashable, Iterable, Mapping


@dataclass(frozen=True)
class JointPriceTarget:
    """One participant in a synchronous Super Bot price calculation."""

    key: Hashable
    target_share: float
    cost_floor: float = 0.0
    share_cap: float = 1.0


@dataclass(frozen=True)
class JointPriceResult:
    """Price and the price-pool share implied by the solved joint field."""

    price: float
    requested_share: float
    planned_share: float
    achieved_share: float
    normalized_gap: float
    normalized_weight: float
    constrained: bool


def _finite(value: float, fallback: float = 0.0) -> float:
    value = float(value)
    return value if math.isfinite(value) else fallback


def _weight_from_gap(gap: float, power: int) -> float:
    """Return ``gap ** power`` with a bounded, normalized base.

    CPI settlement uses an unnormalised currency gap.  Dividing every gap by
    the same positive market average does not change any CPI share, while it
    prevents very large KDS prices raised to power 20 from overflowing.
    """

    gap = min(1.0, max(0.0, _finite(gap)))
    if gap <= 0.0:
        return 0.0
    return math.exp(power * math.log(gap))


def _gap_from_weight(weight: float, power: int) -> float:
    weight = max(0.0, _finite(weight))
    if weight <= 0.0:
        return 0.0
    return min(1.0, math.exp(math.log(weight) / power))


def _weighted_waterfill(
    preferences: list[float],
    total: float,
    lower: list[float],
    upper: list[float],
) -> list[float]:
    """Allocate ``total`` proportionally, redistributing values at a bound."""

    count = len(preferences)
    result = [max(0.0, lower[index]) for index in range(count)]
    upper = [max(result[index], upper[index]) for index in range(count)]
    target = min(max(total, sum(result)), sum(upper))
    remaining = max(0.0, target - sum(result))
    active = {index for index in range(count) if upper[index] > result[index]}

    # At least one item reaches a bound on each non-final pass.
    while active and remaining > 1e-15:
        preference_total = sum(max(0.0, preferences[index]) for index in active)
        if preference_total <= 0.0:
            shares = {index: 1.0 / len(active) for index in active}
        else:
            shares = {
                index: max(0.0, preferences[index]) / preference_total
                for index in active
            }
        saturated: set[int] = set()
        distributed = 0.0
        for index in active:
            proposed = remaining * shares[index]
            addition = min(proposed, upper[index] - result[index])
            result[index] += addition
            distributed += addition
            if upper[index] - result[index] <= 1e-15:
                saturated.add(index)
        remaining -= distributed
        active -= saturated
        if not saturated:
            break
    return result


def solve_joint_prices(
    *,
    base_average: float,
    external_prices: Iterable[float],
    targets: Iterable[JointPriceTarget],
    price_power: int,
    price_min: float,
    price_max: float,
    total_share_cap: float = 0.95,
    zero_external_anchor_gap: float = 0.02,
) -> Mapping[Hashable, JointPriceResult]:
    """Solve one simultaneous set of prices from the current KDS.

    Args:
        base_average: Current market average price ``B`` used by CPI.
        external_prices: Prices of all non-participating competitors.
        targets: Super Bot target price-pool shares and individual constraints.
        price_power: Current KDS ``cpi_price_power`` (required; no stale default).
        price_min: Current KDS minimum allowed product price.
        price_max: Current KDS maximum allowed product price.
        total_share_cap: Maximum combined target share while external price
            competitors exist.  Requests above it are proportionally
            water-filled subject to each bot's ``share_cap``.
        zero_external_anchor_gap: When external price weight is zero, anchor the
            deepest bot this fraction below ``base_average``.  This fixes the
            otherwise scale-free solution without manufacturing a deep price.

    Returns:
        A mapping keyed by ``JointPriceTarget.key``.  Callers should apply every
        returned price together, then use the normal settlement implementation
        as the source of truth.
    """

    base = _finite(base_average)
    if base <= 0.0:
        raise ValueError("base_average must be a positive finite number")
    power = int(price_power)
    if power < 1:
        raise ValueError("price_power must be at least 1")

    global_min = max(0.0, _finite(price_min))
    global_max = _finite(price_max, base)
    if global_max < global_min:
        raise ValueError("price_max must be greater than or equal to price_min")

    items = list(targets)
    keys = [item.key for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("JointPriceTarget keys must be unique")
    if not items:
        return {}

    # Stable key ordering makes floating-point tie handling independent of the
    # caller's iteration order.  Results are still returned by the original key.
    items.sort(key=lambda item: (type(item.key).__name__, repr(item.key)))
    requested = [max(0.0, _finite(item.target_share)) for item in items]
    share_caps = [min(1.0, max(0.0, _finite(item.share_cap))) for item in items]

    external_weights = []
    for raw_price in external_prices:
        price = _finite(raw_price, base)
        gap = max(0.0, (base - price) / base)
        external_weights.append(_weight_from_gap(gap, power))
    external_weight = math.fsum(external_weights)

    combined_cap = min(1.0 - 1e-12, max(0.0, _finite(total_share_cap)))
    if external_weight > 0.0:
        share_budget = min(sum(requested), combined_cap, sum(share_caps))
    else:
        # With no external price weight, every positive bot weight necessarily
        # receives the full pool collectively.  Use a shallow anchor and solve
        # only their relative shares.
        share_budget = min(1.0, sum(share_caps)) if sum(requested) > 0.0 else 0.0
    planned = _weighted_waterfill(
        requested,
        share_budget,
        [0.0] * len(items),
        share_caps,
    )

    lower_prices: list[float] = []
    upper_prices: list[float] = []
    minimum_weights: list[float] = []
    maximum_weights: list[float] = []
    for item in items:
        lower_price = min(global_max, max(global_min, _finite(item.cost_floor)))
        # A price above B receives zero price CPI, so B is the useful upper
        # boundary.  Retain the legal KDS boundary when it is lower than B.
        upper_price = max(lower_price, min(global_max, base))
        lower_prices.append(lower_price)
        upper_prices.append(upper_price)
        minimum_weights.append(
            _weight_from_gap(max(0.0, (base - upper_price) / base), power)
        )
        maximum_weights.append(
            _weight_from_gap(max(0.0, (base - lower_price) / base), power)
        )

    if external_weight > 0.0:
        total_planned_share = min(sum(planned), 1.0 - 1e-12)
        desired_weight = external_weight * total_planned_share / (1.0 - total_planned_share)
        # Individual price-pool caps become exact weight caps once the desired
        # denominator E + W has been fixed.
        desired_denominator = external_weight + desired_weight
        weight_caps = [
            min(maximum_weights[index], share_caps[index] * desired_denominator)
            for index in range(len(items))
        ]
        weights = _weighted_waterfill(
            planned,
            desired_weight,
            minimum_weights,
            weight_caps,
        )
    else:
        positive = [share for share in planned if share > 0.0]
        if not positive:
            weights = minimum_weights[:]
        else:
            deepest_gap = min(1.0, max(1e-9, _finite(zero_external_anchor_gap, 0.02)))
            anchor_weight = _weight_from_gap(deepest_gap, power)
            largest_share = max(positive)
            raw_weights = [
                anchor_weight * share / largest_share if share > 0.0 else 0.0
                for share in planned
            ]
            weights = [
                min(maximum_weights[index], max(minimum_weights[index], raw_weights[index]))
                for index in range(len(items))
            ]

    # Convert every solved weight to its legal KDS price first.  Cost floors
    # and price bounds can change the weight represented by that price (and
    # even an unconstrained inverse conversion can move it by a few ulps).
    # Settlement uses those actual bounded prices, so result metadata must use
    # the same field-wide denominator rather than the pre-bound solve weights.
    bounded_prices: list[float] = []
    bounded_gaps: list[float] = []
    bounded_weights: list[float] = []
    for index in range(len(items)):
        gap = _gap_from_weight(weights[index], power)
        price = min(upper_prices[index], max(lower_prices[index], base * (1.0 - gap)))
        bounded_gap = max(0.0, (base - price) / base)
        bounded_prices.append(price)
        bounded_gaps.append(bounded_gap)
        bounded_weights.append(_weight_from_gap(bounded_gap, power))

    denominator = external_weight + math.fsum(bounded_weights)
    results: dict[Hashable, JointPriceResult] = {}
    for index, item in enumerate(items):
        price = bounded_prices[index]
        bounded_gap = bounded_gaps[index]
        bounded_weight = bounded_weights[index]
        achieved = bounded_weight / denominator if denominator > 0.0 else 0.0
        constrained = (
            abs(achieved - planned[index]) > 1e-7
            or abs(price - lower_prices[index]) <= 1e-7
            or abs(price - upper_prices[index]) <= 1e-7
        )
        results[item.key] = JointPriceResult(
            price=price,
            requested_share=requested[index],
            planned_share=planned[index],
            achieved_share=achieved,
            normalized_gap=bounded_gap,
            normalized_weight=bounded_weight,
            constrained=constrained,
        )
    return results
