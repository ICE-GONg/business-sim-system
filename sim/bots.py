from __future__ import annotations

import json
import hashlib
import math
import random
import sqlite3
import threading
from typing import Any, Callable

from .bot_joint_pricing import JointPriceTarget, solve_joint_prices
from .bot_market_forecast import forecast_market_sales
from .cpi import (
    PRICE_CPI_TOTAL,
    allocate_city_cpi,
    allocate_city_cpi_for_company,
    investment_average_prices,
    prepare_city_cpi_for_company,
)
from .db import all_rows, effective_employee_count, employee_count, get_setting, now_iso, one
from .engine import available_loan_limit, current_company_net_assets, loan_ceiling_for_round


BOT_API_VERSION = 30
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

# Ordinary Bots rotate through inexpensive strategy mixes instead of carrying
# one exaggerated personality through all seven rounds.  These are threshold
# multipliers (not cash amounts), so they continue to work with a custom KDS.
# The rotation is O(1): unlike Super Bots, ordinary Bots do not run a search.
NORMAL_INVESTMENT_MIXES = (
    {"ma": 1.05, "qi": 1.04, "mi": 1.03},  # lean balanced
    {"ma": 1.22, "qi": 1.04, "mi": 1.12},  # MA focus
    {"ma": 1.05, "qi": 1.28, "mi": 1.08},  # QI focus
    {"ma": 1.07, "qi": 1.05, "mi": 2.90},  # MI focus
    {"ma": 1.20, "qi": 1.22, "mi": 1.04},  # MA + QI
    {"ma": 1.24, "qi": 1.05, "mi": 1.80},  # MA + MI
    {"ma": 1.05, "qi": 1.22, "mi": 2.30},  # QI + MI
    {"ma": 1.16, "qi": 1.16, "mi": 1.45},  # broad balanced
    {"ma": 1.31, "qi": 1.09, "mi": 1.15},
    {"ma": 1.09, "qi": 1.34, "mi": 1.25},
    {"ma": 1.11, "qi": 1.11, "mi": 2.65},
    {"ma": 1.27, "qi": 1.25, "mi": 2.00},
)


def _select_empirical_super_candidate(
    candidates: list[dict[str, Any]],
    profile: int,
    *,
    tactical_price_allowed: bool,
    profit_target: float = 0.0,
    competitive_mode: bool = False,
    sacrifice_allowed: bool = False,
) -> dict[str, Any] | None:
    """Choose the plan with the largest relative advantage in the live field.

    Profiles still diversify the candidate grid, rival presets and tie-breaks,
    but may no longer choose a passive plan merely to remain individually safe.
    A plan may accept a large own loss when its forecast denial of rival revenue
    is even larger. Cash affordability remains a hard constraint in candidate
    construction; solvency is deliberately not one here.
    """
    valid = [
        candidate for candidate in candidates
        if math.isfinite(float(candidate.get("predicted_profit", -math.inf)))
    ]
    if not valid:
        return None
    if competitive_mode and not sacrifice_allowed:
        # Saturation and penultimate-round attackers must first look for a
        # profitable way to apply pressure. Deliberate capital sacrifice is
        # reserved for one trailing attacker in the final round so several
        # aggressive Bots cannot drag the whole field into collective losses.
        profitable_attacks = [
            candidate for candidate in valid
            if float(candidate.get("predicted_profit", -math.inf)) >= 0.0
        ]
        if profitable_attacks:
            valid = profitable_attacks
    # Relative advantage is measured against the whole modelled field in the
    # same currency: own net profit plus aggregate rival revenue removed by
    # capacity capture/undercutting. Downside receives a deliberately modest
    # stability penalty; it prevents pointless crashes but still permits a
    # 1.9B own loss when the all-rival damage is materially larger (for example
    # 2.5B). When nobody can be hurt, this collapses to own-profit maximum.
    def strategic_value(candidate: dict[str, Any]) -> float:
        own_profit = float(candidate.get("predicted_profit", -math.inf))
        rival_damage = float(candidate.get("rival_damage", 0.0))
        risk_profit = float(candidate.get("risk_profit", own_profit))
        starting_assets = max(1.0, float(candidate.get("starting_assets", 1.0)))
        ending_assets = float(candidate.get("ending_assets", starting_assets + own_profit))
        capital_ratio = ending_assets / starting_assets
        own_loss_penalty = 0.35 if competitive_mode else 0.10
        risk_loss_penalty = 0.10 if competitive_mode else 0.05
        stability_penalty = (
            max(0.0, -own_profit) * own_loss_penalty
            + max(0.0, -risk_profit) * risk_loss_penalty
            + starting_assets * max(0.0, 0.10 - capital_ratio) ** 2 * 50.0
            + max(0.0, -ending_assets) * 3.0
        )
        targeted_damage = float(candidate.get("targeted_damage", 0.0))
        # Generic rival damage is already part of the shared strategic value.
        # Targeted damage adds direction, not a second full valuation of the
        # same denial, so keep it below one to avoid collective suicide.
        attack_multiplier = 0.90 if competitive_mode else 0.0
        return own_profit + rival_damage + targeted_damage * attack_multiplier - stability_penalty

    style = int(profile) % 7
    return max(
        valid,
        key=lambda candidate: (
            strategic_value(candidate),
            int(
                float(candidate.get("predicted_profit", -math.inf)) > 0
                and float(candidate.get("rival_damage", 0.0)) > 0
            ),
            float(candidate.get("rival_damage", 0.0)),
            float(candidate.get("target_cpi_drop", 0.0)) if competitive_mode else 0.0,
            float(candidate.get("target_surplus", 0.0)) if competitive_mode else 0.0,
            float(candidate.get("predicted_profit", -math.inf)),
            float(candidate.get("risk_profit", -math.inf)),
            int(float(candidate.get("predicted_profit", -math.inf)) > float(profit_target)),
            float(candidate.get("predicted_sold", 0.0)),
            float(candidate.get("sell_ratio", 0.0)),
            -abs(float(candidate.get("coverage", 0.0)) - 1.0),
            # Deterministic final tie-break preserves small profile differences
            # only when the economic forecast is otherwise identical.
            ((-1.0 if tactical_price_allowed and style == 6 else 1.0)
             * float(candidate.get("price_ratio", 0.0))),
        ),
    )


def _competitive_super_attackers(
    ranked_super_ids: list[int],
    *,
    official_round: int,
    total_rounds: int,
    markets_saturated: bool,
) -> set[int]:
    """Choose a small deterministic set of lower-ranked aggressive bots."""
    count = len(ranked_super_ids)
    if count <= 1:
        return set()
    protected = 3 if count >= 3 else 1
    eligible = ranked_super_ids[protected:]
    if not eligible:
        return set()
    if official_round >= max(1, total_rounds - 1):
        attack_count = max(1, math.ceil(count / 3))
    elif markets_saturated:
        attack_count = 2 if count >= 6 else 1
    else:
        return set()
    return set(eligible[-min(len(eligible), attack_count):])


def _strict_profit_improvement(value: float, rate: float) -> float:
    value = float(value)
    return value + max(1.0, abs(value) * rate)


def _super_profit_target(
    conn: sqlite3.Connection,
    company_id: int,
    round_no: int,
    previous_super_id: int | None = None,
) -> float:
    """Benchmark against own growth, the preceding Bot and higher ranks.

    Rankings and profits are taken from the latest completed official round;
    current submitted decisions are still read separately by the CPI forecast.
    This keeps the benchmark deterministic for local and remote/split analysis.
    """
    if round_no <= 1:
        return 0.0
    own = one(
        conn,
        "SELECT round_no,net_assets,net_profit FROM results WHERE company_id=? "
        "AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
        (company_id, round_no),
    )
    if not own:
        return 0.0
    benchmark_round = int(own["round_no"])
    targets = [_strict_profit_improvement(float(own["net_profit"] or 0.0), 0.03)]
    higher = one(
        conn,
        "SELECT MAX(net_profit) AS profit FROM results WHERE round_no=? AND company_id<>? "
        "AND (net_assets>? OR (net_assets=? AND company_id<?))",
        (benchmark_round, company_id, own["net_assets"], own["net_assets"], company_id),
    )
    if higher and higher["profit"] is not None:
        targets.append(_strict_profit_improvement(float(higher["profit"]), 0.01))
    if previous_super_id is not None:
        previous_bot = one(
            conn,
            "SELECT net_profit FROM results WHERE company_id=? AND round_no=?",
            (previous_super_id, benchmark_round),
        )
        if previous_bot:
            targets.append(_strict_profit_improvement(float(previous_bot["net_profit"] or 0.0), 0.01))
    return max(0.0, *targets)


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


def _kds_price_depth(base_depth: float, price_power: int) -> float:
    """Translate an 8th-power price style to the current KDS exponent.

    Price CPI compares ``price_gap ** power``.  Transforming the normalized
    gap by ``8 / power`` preserves the intended relative CPI weight instead of
    treating the same cash undercut as equivalent in every competition.
    """
    return min(1.0, max(0.0, float(base_depth))) ** (8.0 / max(1, int(price_power)))


def _price_for_weight_advantage(
    market_average: float,
    rival_price: float,
    price_floor: float,
    price_power: int,
    weight_multiplier: float = 1.5,
) -> float:
    """Return the smallest KDS-aware undercut for a CPI-weight advantage."""
    average = float(market_average)
    rival_gap = max(0.0, average - float(rival_price))
    if rival_gap <= 0:
        return max(float(price_floor), min(average, float(rival_price)))
    target_gap = rival_gap * max(1.0, float(weight_multiplier)) ** (1.0 / max(1, int(price_power)))
    return max(float(price_floor), average - target_gap)


def _all_markets_near_capacity(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
    threshold: float = 0.90,
) -> bool:
    """Cash may be held only when every market was full in the same latest round."""
    if round_no <= 1 or not markets:
        return False
    latest = one(
        conn,
        "SELECT MAX(round_no) AS round_no FROM market_round_stats "
        "WHERE round_no>=1 AND round_no<?",
        (round_no,),
    )
    if not latest or latest["round_no"] is None:
        return False
    latest_round = int(latest["round_no"])
    for market in markets:
        stats = one(
            conn,
            "SELECT player_total_volume,market_size FROM market_round_stats "
            "WHERE city=? AND round_no=?",
            (market["city"], latest_round),
        )
        if not stats:
            return False
        utilization = (
            float(stats["player_total_volume"] or 0)
            / max(1.0, float(stats["market_size"] or 0))
        )
        if utilization + 1e-9 < threshold:
            return False
    return True


def _forecast_submitted_market(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Forecast the complete submitted field with the settlement algorithm.

    This uses the same average-price fixed point and category-isolated
    secondary allocation as settlement.  Reading the KDS here (rather than
    accepting cached planner defaults) also means an unlocked KDS change is
    reflected the next time a round is analysed.
    """
    official_round = 1 if round_no < 0 else round_no
    growth = float(get_setting(conn, "market_growth", 1.10))
    total_rounds = max(1, int(get_setting(conn, "total_rounds", 5)))
    ma_threshold = float(get_setting(conn, "cpi_ma_large_threshold", 1300))
    price_power = max(1, int(get_setting(conn, "cpi_price_power", 8)))
    players: dict[int, dict[str, Any]] = {}
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
        players[company_id] = {
            "company_id": company_id,
            "available": old_products + production,
            "ma_index": float(row["management_investment"] or 0) / max(1, workers + engineers),
            "qi_index": float(row["quality_investment"] or 0) / max(1.0, old_products * 1.2 + production),
            "cities": {},
        }
    if not players:
        return {"companies": {}, "player_average_prices": {}, "price_power": price_power}

    resolved_markets: list[dict[str, Any]] = []
    for market in markets:
        city = str(market["city"])
        previous = one(
            conn,
            "SELECT average_price FROM market_round_stats WHERE city=? AND round_no>=1 "
            "AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (city, max(1, round_no)),
        )
        resolved_markets.append({
            "city": city,
            "market_size": (
                float(market["population"])
                * float(market["penetration"])
                * growth ** max(0, official_round - 1)
            ),
            "max_price": float(market["max_price"]),
            "base_average_price": (
                float(previous["average_price"])
                if previous else float(market["initial_avg_price"])
            ),
        })
        for row in all_rows(
            conn,
            "SELECT cd.company_id,cd.agent_delta,cd.marketing_investment,cd.price,COALESCE(a.count,0) AS current_agents "
            "FROM city_decisions cd LEFT JOIN agents a ON a.company_id=cd.company_id AND a.city=cd.city "
            "WHERE cd.round_no=? AND cd.city=?",
            (round_no, city),
        ):
            company_id = int(row["company_id"])
            if company_id not in players:
                continue
            agents = max(0, int(row["current_agents"] or 0) + int(row["agent_delta"] or 0))
            if agents > 0:
                players[company_id]["cities"][city] = {
                    "agents": agents,
                    "marketing": float(row["marketing_investment"] or 0),
                    "price": float(row["price"] or 0),
                }
    return forecast_market_sales(
        players=list(players.values()),
        markets=resolved_markets,
        ma_large_threshold=ma_threshold,
        price_power=price_power,
    )


def _forecast_submitted_cpi_capacity(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> dict[int, float]:
    """Return visible CPI capacity before stock and secondary allocation."""
    forecast = _forecast_submitted_market(conn, round_no, markets)
    return {
        int(company_id): float(result["visible_total"])
        for company_id, result in forecast["companies"].items()
    }


def _forecast_submitted_sales_capacity(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> dict[int, float]:
    """Return exact sellable units, including category-isolated redistribution."""
    forecast = _forecast_submitted_market(conn, round_no, markets)
    return {
        int(company_id): float(result["sold_total"])
        for company_id, result in forecast["companies"].items()
    }


def _coordinate_super_bot_prices(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> bool:
    """Apply one simultaneous KDS-aware price plan to low-price Super Bots.

    Individual candidates are deliberately persisted one at a time so an
    interrupted analysis is resumable.  Their prices cannot, however, be
    reconciled one at a time when the KDS uses a high price exponent: a later
    tiny undercut would erase an earlier Bot's forecast.  This final pass works
    in price-CPI weight space and updates the whole vector together.
    """
    if not markets:
        return False
    official_round = 1 if round_no < 0 else round_no
    total_rounds = max(1, int(get_setting(conn, "total_rounds", 5)))
    price_power = max(1, int(get_setting(conn, "cpi_price_power", 8)))
    price_min = float(get_setting(conn, "price_min", 3500))
    global_price_max = float(get_setting(conn, "price_max", 25000))
    growth = float(get_setting(conn, "market_growth", 1.10))
    worker_need = float(get_setting(conn, "component_workers", 3))
    worker_hours = float(get_setting(conn, "component_hours", 7))
    engineer_need = float(get_setting(conn, "product_engineers", 4))
    engineer_hours = float(get_setting(conn, "product_hours", 14))
    component_need = max(1, int(round(get_setting(conn, "components_per_product", 7))))
    patent_factor = float(get_setting(conn, "patent_factor", 0.70))
    transport_cost = float(get_setting(conn, "transport_cost", 0))
    market_by_city = {str(market["city"]): market for market in markets}

    previous_stats: dict[str, dict[str, float]] = {}
    for market in markets:
        city = str(market["city"])
        stats = one(
            conn,
            "SELECT average_price,player_total_volume,market_size FROM market_round_stats "
            "WHERE city=? AND round_no>=1 AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (city, max(1, round_no)),
        )
        previous_stats[city] = {
            "average": float(stats["average_price"]) if stats else float(market["initial_avg_price"]),
            "utilization": (
                float(stats["player_total_volume"] or 0) / max(1.0, float(stats["market_size"] or 0))
                if stats else 0.0
            ),
        }
    all_super_field = not bool(one(
        conn,
        "SELECT 1 FROM companies WHERE is_super_bot=0 LIMIT 1",
    ))
    low_price_open = (
        all_super_field
        or official_round >= max(1, total_rounds - 1)
        or all(value["utilization"] >= 0.90 for value in previous_stats.values())
        or any(
            value["average"] >= min(global_price_max, float(market_by_city[city]["max_price"])) * 0.75
            for city, value in previous_stats.items()
        )
    )
    if not low_price_open:
        return False

    rows = [dict(row) for row in all_rows(
        conn,
        "SELECT c.id,c.is_super_bot,c.bot_profile,c.home_city,c.cash,c.patents,"
        "c.component_inventory,c.product_inventory,c.component_storage_capacity,c.product_storage_capacity,"
        "d.loan_change,d.worker_salary,d.engineer_salary,d.worker_delta,d.engineer_delta,d.production_volume,"
        "d.management_investment,d.quality_investment,cd.city,cd.price,cd.marketing_investment,"
        "cd.agent_delta,COALESCE(a.count,0) AS agents_before,"
        "COALESCE(a.count,0)+cd.agent_delta AS agents_after "
        "FROM companies c JOIN decisions d ON d.company_id=c.id "
        "JOIN city_decisions cd ON cd.company_id=c.id AND cd.round_no=d.round_no "
        "LEFT JOIN agents a ON a.company_id=c.id AND a.city=cd.city "
        "WHERE d.round_no=? AND d.submitted_at IS NOT NULL",
        (round_no,),
    )]
    if not rows:
        return False

    company_rows: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if int(row["agents_after"] or 0) > 0:
            company_rows.setdefault(int(row["id"]), []).append(row)
    price_led_ids: set[int] = set()
    for company_id, active_rows in company_rows.items():
        sample = active_rows[0]
        if not int(sample["is_super_bot"] or 0):
            continue
        workers = max(
            0,
            employee_count(conn, company_id, "worker")
            + int(sample["worker_delta"] or 0),
        )
        engineers = max(
            0,
            employee_count(conn, company_id, "engineer")
            + int(sample["engineer_delta"] or 0),
        )
        available = (
            float(sample["product_inventory"] or 0) * 1.2
            + float(sample["production_volume"] or 0)
        )
        ma_index = float(sample["management_investment"] or 0) / max(1, workers + engineers)
        qi_index = float(sample["quality_investment"] or 0) / max(1.0, available)
        if ma_index <= 1.01 and qi_index <= 1.01:
            # Price-led plans may be pure price or price + per-city MI. Both
            # deliberately chose their undercut in the profit search.
            price_led_ids.add(company_id)
    prior_sellthrough: dict[int, float] = {}
    for company_id in company_rows:
        prior = one(
            conn,
            "SELECT sold,produced,inventory FROM results WHERE company_id=? AND round_no>=1 "
            "AND round_no<? ORDER BY round_no DESC LIMIT 1",
            (company_id, max(1, round_no)),
        )
        previous_available = (
            float(prior["sold"] or 0) + float(prior["inventory"] or 0)
            if prior else 0.0
        )
        prior_sellthrough[company_id] = (
            float(prior["sold"] or 0) / previous_available
            if prior and previous_available > 0 else 1.0
        )

    forecast = _forecast_submitted_market(conn, round_no, markets)
    forecast_companies = forecast["companies"]

    company_active_size = {
        company_id: sum(
            float(market_by_city[str(row["city"])]["population"])
            * float(market_by_city[str(row["city"])]["penetration"])
            * growth ** max(0, official_round - 1)
            for row in active_rows
        )
        for company_id, active_rows in company_rows.items()
    }
    updates: list[tuple[float, int, str]] = []
    original_prices = {
        (int(row["id"]), str(row["city"])): float(row["price"] or 0)
        for row in rows
    }
    for market in markets:
        city = str(market["city"])
        base_average = previous_stats[city]["average"]
        market_size = (
            float(market["population"])
            * float(market["penetration"])
            * growth ** max(0, official_round - 1)
        )
        active_rows = [row for row in rows if str(row["city"]) == city and int(row["agents_after"] or 0) > 0]
        specialists: list[dict[str, Any]] = []
        for row in active_rows:
            if not int(row["is_super_bot"] or 0):
                continue
            company_id = int(row["id"])
            if company_id in price_led_ids:
                # The optimiser explicitly selected price-led competition.
                # Joint coordination may use it as an external anchor but must
                # not raise away the deep undercut it just selected.
                continue
            inventory_crisis = (
                int(row["product_inventory"] or 0) > 0
                and prior_sellthrough.get(company_id, 1.0) < 0.80
            )
            if (
                float(row["price"] or 0) < base_average
                or inventory_crisis
            ):
                specialists.append(row)
        if not specialists:
            continue

        specialist_ids = {int(row["id"]) for row in specialists}
        external_prices = [
            float(row["price"] or 0)
            for row in active_rows
            if int(row["id"]) not in specialist_ids
        ]
        targets: list[JointPriceTarget] = []
        for row in specialists:
            company_id = int(row["id"])
            home = market_by_city.get(str(row["home_city"]), market)
            material_factor = patent_factor ** int(row["patents"] or 0)
            component_labor = (
                component_need * worker_need * worker_hours / 504.0
                * float(row["worker_salary"] or home["worker_initial_salary"]) * 3
            )
            product_labor = (
                engineer_need * engineer_hours / 504.0
                * float(row["engineer_salary"] or home["engineer_initial_salary"]) * 3
            )
            direct_cost = (
                component_need * float(home["component_material"]) * material_factor
                + float(home["product_material"]) * material_factor
                + component_labor + product_labor
                + (transport_cost if city != str(row["home_city"]) else 0.0)
            )
            available = float(row["product_inventory"] or 0) + float(row["production_volume"] or 0)
            company_marketing = sum(
                float(item["marketing_investment"] or 0)
                for item in company_rows.get(company_id, [])
            )
            investment_unit_cost = (
                float(row["management_investment"] or 0)
                + float(row["quality_investment"] or 0)
                + company_marketing
            ) / max(1.0, available)
            active_size = max(1.0, company_active_size.get(company_id, market_size))
            personality = int(row["bot_profile"] or company_id) % 7
            sellthrough_target = 0.78 + personality * 0.01
            desired_city_units = available * sellthrough_target * market_size / active_size
            city_forecast = forecast_companies.get(company_id, {})
            investment_units = float(city_forecast.get("investment", {}).get(city, 0.0))
            required_price_units = max(0.0, desired_city_units - investment_units)
            requested_share = required_price_units / max(1.0, market_size * 0.40)
            requested_share *= 0.92 + personality * 0.025
            # Keep several profitable participants in the price route. A
            # single Bot taking the entire pool is the exact failure mode this
            # pass is designed to prevent.
            requested_share = min(0.22, max(0.025, requested_share))
            targets.append(JointPriceTarget(
                key=company_id,
                target_share=requested_share,
                share_cap=0.24,
                cost_floor=max(price_min, (direct_cost + investment_unit_cost) * 1.04),
            ))
        solved = solve_joint_prices(
            base_average=base_average,
            external_prices=external_prices,
            targets=targets,
            price_power=price_power,
            price_min=price_min,
            price_max=min(global_price_max, float(market["max_price"])),
            total_share_cap=0.68,
            zero_external_anchor_gap=0.025,
        )
        for company_id, result in solved.items():
            # Joint coordination may spread the price CPI, but it must never
            # push a profit-selected candidate below its own price. Production
            # can be controlled instead; forced undercutting caused the old
            # final-round crashes.
            safe_price = max(
                float(result.price),
                original_prices.get((int(company_id), city), price_min),
            )
            updates.append((round(safe_price, 2), int(company_id), city))

    for price, company_id, city in updates:
        conn.execute(
            "UPDATE city_decisions SET price=? WHERE company_id=? AND round_no=? AND city=?",
            (price, company_id, round_no, city),
        )
    return bool(updates or price_led_ids)


def _rebalance_super_bot_production(
    conn: sqlite3.Connection,
    round_no: int,
    markets: list[dict[str, Any]],
) -> None:
    """Reconcile mixed-field joint CPI without erasing chosen aggression."""
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
    total_rounds = max(1, int(get_setting(conn, "total_rounds", 5)))
    ma_large_threshold = max(1.0, float(get_setting(conn, "cpi_ma_large_threshold", 1300)))
    ma_index_ceiling = ma_large_threshold * (8000.0 / 1300.0)
    official_round = 1 if round_no < 0 else round_no

    market_by_city = {str(market["city"]): market for market in markets}
    if not bool(one(conn, "SELECT 1 FROM companies WHERE is_super_bot=0 LIMIT 1")):
        # In an all-Super-Bot pressure test, every unresolved rival was already
        # preset during each search and later Bots responded to earlier saved
        # decisions. A final trim/price-raising pass would erase deliberate
        # full-output undercuts and high-investment denial strategies.
        return
    _coordinate_super_bot_prices(conn, round_no, markets)

    if (
        official_round <= max(2, math.ceil(total_rounds * 0.50))
        and not _all_markets_near_capacity(conn, round_no, markets, threshold=0.60)
    ):
        # During the unsaturated expansion phase, keep the maximum affordable
        # complete-group production selected by the search. Do not trim output
        # merely to accommodate the current competitors' conservative volume.
        return

    # The candidate search has already compared affordable production levels.
    # This pass may trim newly revealed surplus after simultaneous price
    # coordination, but it must never expand back to the cash maximum.
    for _ in range(12):
        # Production follows the exact units this field can sell after the
        # category-isolated secondary pass. Visible CPI alone misses legitimate
        # price-to-price and investment-to-investment redistribution.
        capacities = _forecast_submitted_sales_capacity(conn, round_no, markets)
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
            current_groups = max(
                0,
                int(round(current_production / max(group["products"], 1.0))),
            )
            sellable = max(0.0, capacities.get(company_id, 0.0))
            if sellable >= old_products + current_production * 0.97:
                continue
            target_new = max(0.0, sellable * 1.02 - old_products)
            target_groups = min(
                current_groups,
                max(0, int(math.ceil(target_new / max(group["products"], 1.0)))),
            )
            selected_plan = plan_for(target_groups)
            if float(selected_plan["total"]) > total_funds + 1e-9:
                continue
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
            changed = True
        if not changed:
            break


def _submit_bots(
    conn: sqlite3.Connection,
    round_no: int,
    super_mode: bool,
    progress_callback: Callable[[int, int, str], None] | None = None,
    replace_existing: bool = False,
    *,
    target_ids: set[int] | None = None,
    defer_rebalance: bool = False,
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
    price_power = max(1, int(setting("cpi_price_power", 8)))
    max_agent_add = max(0, int(setting("max_agent_add_per_city_round", 3)))
    growth = setting("market_growth", 1.10)
    worker_need, worker_hours = setting("component_workers", 3), setting("component_hours", 7)
    engineer_need, engineer_hours = setting("product_engineers", 4), setting("product_hours", 14)
    component_need = max(1, int(round(setting("components_per_product", 7))))
    worker_training = setting("worker_training_cost", 0)
    engineer_training = setting("engineer_training_cost", 0)
    add_agent_cost = setting("agent_add_cost", 300000)
    remove_agent_cost = setting("agent_remove_cost", 100000)
    transport_cost = setting("transport_cost", 0)
    patent_factor = setting("patent_factor", 0.70)
    tax_rate = min(1.0, max(0.0, setting("tax_rate", 0.20)))
    ma_threshold = setting("cpi_ma_large_threshold", 1300)
    qi_safe_multiplier = max(1.0, setting("qi_safe_multiplier", 1.10))
    initial_cash = max(1.0, setting("initial_cash", 15_000_000))
    total_rounds = max(1, int(setting("total_rounds", 5)))
    loan_threshold = setting("loan_asset_threshold", 15_000_000)
    global_max_loan = setting("global_max_loan", 10_000_000)
    research_goal = setting("research_75", 6000000) * setting("research_hidden_threshold_multiplier", 4 / 3)
    research_goal += setting("research_buffer", 150000)

    human_present = bool(one(conn, "SELECT 1 FROM companies WHERE is_bot=0 LIMIT 1"))
    all_super_field = bool(super_mode) and not bool(one(
        conn,
        "SELECT 1 FROM companies WHERE is_super_bot=0 LIMIT 1",
    ))
    benchmark_round_row = one(
        conn,
        "SELECT MAX(round_no) AS round_no FROM results WHERE round_no>=1 AND round_no<?",
        (max(1, round_no),),
    )
    benchmark_round = int(benchmark_round_row["round_no"] or 0) if benchmark_round_row else 0
    ranked_rows = [dict(row) for row in all_rows(
        conn,
        "SELECT c.id,c.is_bot,c.is_super_bot,r.net_assets,r.net_profit,r.sales_revenue "
        "FROM companies c JOIN results r ON r.company_id=c.id "
        "WHERE r.round_no=? ORDER BY r.net_assets DESC,c.id",
        (benchmark_round,),
    )] if benchmark_round else []
    rank_by_company = {
        int(row["id"]): index + 1 for index, row in enumerate(ranked_rows)
    }
    prior_margin_by_company = {
        int(row["id"]): (
            float(row["net_profit"] or 0.0)
            / max(1.0, float(row["sales_revenue"] or 0.0))
        )
        for row in ranked_rows
    }
    ranked_super_ids = [
        int(row["id"]) for row in ranked_rows if int(row["is_super_bot"] or 0)
    ]
    ranked_super_ids.extend(
        int(bot["id"]) for bot in bots if int(bot["id"]) not in ranked_super_ids
    )
    competitive_super_ids = (
        _competitive_super_attackers(
            ranked_super_ids,
            official_round=official_round,
            total_rounds=total_rounds,
            markets_saturated=all_markets_full,
        )
        if super_mode else set()
    )
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
        late_market_expansion_round = max(3, math.ceil(total_rounds * 0.65))
        if super_mode and official_round >= late_market_expansion_round:
            # Late Super Bots compete everywhere. Leaving cities unopened
            # wastes market capacity and the independent per-city MI route.
            market_count = len(markets)
        elif official_round >= total_rounds:
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
        if (target_ids is None or int(bot["id"]) in target_ids)
        and (replace_existing or not one(
            conn,
            "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?",
            (int(bot["id"]), round_no),
        ))
    )
    progress_total = pending_total + (1 if super_mode and pending_total and not defer_rebalance else 0)
    # Later Super Bots see decisions completed earlier in this pass, while
    # unresolved peers stay synthetic. This preserves resumability and gives
    # later positions a real opportunity to counter or deeply undercut.
    frozen_super_ids: set[int] = {
        int(saved["id"])
        for saved in bots
        if one(
            conn,
            "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?",
            (int(saved["id"]), round_no),
        )
        and not (
            replace_existing
            and (target_ids is None or int(saved["id"]) in target_ids)
        )
    }
    for bot_position, bot_row in enumerate(bots):
        bot = dict(bot_row)
        company_id = int(bot["id"])
        if target_ids is not None and company_id not in target_ids:
            continue
        existing_decision = one(
            conn,
            "SELECT 1 FROM decisions WHERE company_id=? AND round_no=?",
            (company_id, round_no),
        )
        if existing_decision and not replace_existing:
            continue
        competitive_mode = bool(super_mode and company_id in competitive_super_ids)
        profile = int(bot["bot_profile"] if bot["bot_profile"] is not None else company_id) % 7
        profit_target = 0.0
        if super_mode:
            previous_super_id = int(bots[bot_position - 1]["id"]) if bot_position > 0 else None
            profit_target = _super_profit_target(
                conn, company_id, round_no, previous_super_id,
            )
        chosen_predicted_profit: float | None = None
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
        sequence_strength = 1.0 + 0.18 * bot_position / max(1, len(bots) - 1)
        if super_mode:
            # Later Super Bots have observed more real decisions. Convert that
            # information advantage into a wider, still capped response range.
            super_aggression *= sequence_strength
            super_mi_cap = min(6.0, super_mi_cap * sequence_strength)
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
            delta = max(-current, min(max_agent_add, desired - current))
            if super_mode:
                # Never pay to remove a 10%-MI amplifier or abandon a city.
                delta = max(0, delta)
            agent_plan[index] = (delta, current + delta)
            if delta > 0:
                agent_cost += delta * add_agent_cost
            elif delta < 0:
                agent_cost += -delta * remove_agent_cost

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
            utilization[index] = (
                float(stats["player_total_volume"] or 0) / max(1.0, float(stats["market_size"] or 0))
                if stats else 0.0
            )
            saturated[index] = utilization[index] >= 0.60
            previous_prices[index] = float(stats["average_price"]) if stats else float(market["initial_avg_price"])

        late_game_low_price = official_round >= max(1, total_rounds - 1)
        saturated_low_price_indices = {
            index for index in selected if utilization.get(index, 0.0) >= 0.95
        }

        old_products = int(bot["product_inventory"] or 0)
        old_components = int(bot["component_inventory"] or 0)
        own_strategy_assets = max(1.0, current_company_net_assets(conn, bot))
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
        round_market_ramp = (0.34, 0.52, 0.72, 0.88, 1.00, 1.08, 1.15)[plan_index]
        fair_market_units = sum(
            (
                float(markets[index]["population"])
                * float(markets[index]["penetration"])
                * growth ** max(0, official_round - 1)
            ) / max(1, city_competitors[index])
            for index in selected
        )
        # Production anchors scale with the live KDS market size and actual
        # field density. The seven-round table supplies only the strategy
        # phase; no historic event's raw unit count leaks into this KDS.
        desired_available = int(fair_market_units * round_market_ramp * variation * capital_scale)
        prior_production = report.get("production", {})
        prior_sold = 0
        prior_total = 0
        prior_surplus = 0
        if prior_production:
            prior_sold = int(prior_production.get("sold", 0) or 0)
            prior_total = int(prior_production.get("old_products", 0) or 0) + int(prior_production.get("produced", 0) or 0)
            prior_surplus = int(prior_production.get("surplus", 0) or 0)
            if prior_total and prior_sold >= prior_total * 0.95 and not any(saturated.values()):
                desired_available = max(desired_available, int(prior_sold * 1.35))
            elif prior_surplus > max(20, prior_sold * 0.30):
                desired_available = min(desired_available, int(prior_sold * 1.12 + old_products))
        prior_surplus_ratio = prior_surplus / max(1, prior_total)
        cash_ratio = float(bot["cash"]) / initial_cash
        # Low price is an emergency tool, not the normal response to a small
        # amount of stock. Keep the profit strategy unless fewer than half of
        # last round's available products were sold.
        distress_liquidation = bool(
            not super_mode
            and prior_surplus > max(10, prior_total * 0.06)
            and prior_sold < prior_total * 0.50
        )
        if distress_liquidation:
            # Do not pay to expand the network while rescuing existing stock.
            # Existing agents remain in place and can sell the inventory.
            for index, (delta, agents_after) in tuple(agent_plan.items()):
                if delta > 0:
                    agent_plan[index] = (0, agents_after - delta)
            agent_cost = sum(
                max(0, delta) * add_agent_cost
                for delta, _ in agent_plan.values()
            )
        production_goal = max(0, desired_available - old_products)
        material_factor = patent_factor ** int(bot["patents"] or 0)
        research_balance = max(0.0, float(bot["research_balance"] or 0))
        research_needed = max(0.0, research_goal - research_balance)
        research = 0.0

        rival_metrics: dict[int, list[dict[str, float]]] = {index: [] for index in range(len(markets))}
        if super_mode:
            rival_asset_weights: dict[int, float] = {}
            own_rank = rank_by_company.get(company_id, len(rank_by_company) + 1)

            def attack_priority(
                rival_id: int,
                *,
                rival_is_bot: bool,
                rival_is_super: bool,
                index: int,
                mi_effective: float,
                price: float,
                predicted_margin: float,
            ) -> float:
                if not competitive_mode:
                    return 0.0
                rival_rank = rank_by_company.get(rival_id, len(rank_by_company) + 1)
                higher_ranked = rival_rank < own_rank
                if not rival_is_bot:
                    # 1) higher-ranked human; 4) lower-ranked human.
                    priority = 4.0 if higher_ranked else 1.0
                elif rival_is_super and higher_ranked:
                    # 2) a Super Bot that is currently ahead.
                    priority = 3.0
                elif not rival_is_super and predicted_margin >= 0.18:
                    # 3) an ordinary Bot with a strong profit forecast.
                    priority = 2.0
                else:
                    # 5) everything else remains a weak fallback target.
                    priority = 0.15
                if not human_present and rival_is_super and higher_ranked:
                    priority = max(priority, 3.0)
                market = markets[index]
                size = (
                    float(market["population"]) * float(market["penetration"])
                    * growth ** max(0, official_round - 1)
                )
                mi_large = (float(market["max_price"]) / 50.0) * size * 0.20 / 1.5 / 2.0
                mi_ratio = mi_effective / max(1.0, mi_large)
                if mi_ratio >= 2.0:
                    priority += min(1.25, (mi_ratio - 2.0) * 0.20 + 0.35)
                reference = previous_prices.get(index, float(market["initial_avg_price"]))
                if price > 0.0 and price <= reference * 0.80:
                    priority += 1.0
                priority += min(0.80, max(0.0, predicted_margin) * 1.5)
                return priority

            def rival_asset_weight(rival_id: int) -> float:
                cached = rival_asset_weights.get(rival_id)
                if cached is not None:
                    return cached
                latest_assets = one(
                    conn,
                    "SELECT net_assets FROM results WHERE company_id=? AND round_no>=1 "
                    "AND round_no<? ORDER BY round_no DESC LIMIT 1",
                    (rival_id, max(1, round_no)),
                )
                if latest_assets:
                    rival_assets = max(1.0, float(latest_assets["net_assets"] or 0))
                else:
                    rival_company = one(conn, "SELECT * FROM companies WHERE id=?", (rival_id,))
                    rival_assets = max(
                        1.0,
                        current_company_net_assets(conn, dict(rival_company))
                        if rival_company else 1.0,
                    )
                weight = min(3.0, max(0.35, math.sqrt(rival_assets / own_strategy_assets)))
                rival_asset_weights[rival_id] = weight
                return weight

            rival_rows = all_rows(
                conn,
                "SELECT d.company_id,d.worker_delta,d.engineer_delta,d.management_investment,d.production_volume,"
                "d.quality_investment,cd.city,cd.agent_delta,cd.marketing_investment,cd.price,c.product_inventory,"
                "c.is_bot AS rival_is_bot,c.is_super_bot AS rival_is_super "
                "FROM decisions d JOIN companies c ON c.id=d.company_id "
                "JOIN city_decisions cd ON cd.company_id=d.company_id AND cd.round_no=d.round_no "
                "WHERE d.round_no=? AND d.submitted_at IS NOT NULL AND d.company_id<>?",
                (round_no, company_id),
            )
            market_index = {str(market["city"]): index for index, market in enumerate(markets)}
            for rival in rival_rows:
                if int(rival["rival_is_super"] or 0) and int(rival["company_id"]) not in frozen_super_ids:
                    continue
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
                available_per_city = (
                    float(rival["product_inventory"] or 0) + float(rival["production_volume"] or 0)
                ) / max(1, int(active_count["n"] or 0))
                mi_effective = float(rival["marketing_investment"] or 0) * (1.0 + agents * 0.10)
                revenue_proxy = max(0.0, float(rival["price"] or 0)) * available_per_city
                fixed_proxy = (
                    float(rival["management_investment"] or 0)
                    + float(rival["quality_investment"] or 0)
                ) / max(1, int(active_count["n"] or 0)) + float(rival["marketing_investment"] or 0)
                predicted_margin = max(
                    prior_margin_by_company.get(int(rival["company_id"]), 0.0),
                    (revenue_proxy - fixed_proxy) / max(1.0, revenue_proxy),
                )
                rival_metrics[index].append({
                    "company_id": float(rival["company_id"]),
                    "ma": ma_index,
                    "qi": qi_index,
                    "mi_effective": mi_effective,
                    "price": float(rival["price"] or 0),
                    "agents": float(agents),
                    "attack_weight": attack_priority(
                        int(rival["company_id"]),
                        rival_is_bot=bool(rival["rival_is_bot"]),
                        rival_is_super=bool(rival["rival_is_super"]),
                        index=index,
                        mi_effective=mi_effective,
                        price=float(rival["price"] or 0),
                        predicted_margin=predicted_margin,
                    ),
                    "strategic_weight": rival_asset_weight(int(rival["company_id"])),
                    "available": available_per_city,
                })

            # Super Bots are analysed and committed one at a time, but every
            # one sees the same synthetic peers. This keeps a resumed/split run
            # identical to an uninterrupted batch; the final joint pass uses
            # the complete real vector.
            for other_row in bots:
                other = dict(other_row)
                other_id = int(other["id"])
                if other_id == company_id or other_id in frozen_super_ids:
                    continue
                other_profile = int(other["bot_profile"] if other["bot_profile"] is not None else other_id) % 7
                other_style = BOT_STYLES[other_profile]
                other_variation = 0.94 + other_profile * 0.02
                other_selected = selected_by_bot[other_id]
                prior_other = one(
                    conn,
                    "SELECT round_no,ma_index,qi_index,sold,inventory FROM results "
                    "WHERE company_id=? AND round_no>=1 AND round_no<? "
                    "ORDER BY round_no DESC LIMIT 1",
                    (other_id, max(1, round_no)),
                )
                prior_active = 1
                prior_cities: dict[str, sqlite3.Row] = {}
                if prior_other:
                    prior_active_row = one(
                        conn,
                        "SELECT COUNT(*) AS n FROM city_results WHERE company_id=? "
                        "AND round_no=?",
                        (other_id, int(prior_other["round_no"])),
                    )
                    prior_active = max(1, int(prior_active_row["n"] or 0))
                    prior_cities = {
                        str(row["city"]): row
                        for row in all_rows(
                            conn,
                            "SELECT city,price,marketing FROM city_results "
                            "WHERE company_id=? AND round_no=?",
                            (other_id, int(prior_other["round_no"])),
                        )
                    }
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
                    cap = min(price_max, float(market["max_price"]))
                    prior_average = previous_prices.get(index, float(market["initial_avg_price"]))
                    prior_city = prior_cities.get(str(market["city"]))
                    if (
                        prior_average >= cap * 0.75
                        or late_game_low_price
                        or index in saturated_low_price_indices
                        or all_super_field
                    ):
                        # Unresolved Super Bots must be modelled as possible
                        # low-price competitors once that strategy is unlocked.
                        # Otherwise every sequential Bot falsely believes it can
                        # monopolise the 40-CPI price pool at exactly 75%.
                        # Model unresolved rivals in price-gap/CPI-weight
                        # space.  A fixed percentage of max price is wildly
                        # wrong when one KDS uses power 8 and another uses 20.
                        base_depths = (0.18, 0.26, 0.34, 0.43, 0.53, 0.64, 0.76)
                        depth = _kds_price_depth(base_depths[other_profile], price_power)
                        expected_price = prior_average - (
                            prior_average - price_min
                        ) * depth
                        if prior_city and float(prior_city["price"] or 0) > 0:
                            # The unresolved peer starts from its proven route,
                            # then applies the current KDS-scaled pressure depth.
                            proven = min(cap, max(price_min, float(prior_city["price"])))
                            expected_price = min(expected_price, proven)
                        expected_price = min(cap, max(price_min, expected_price))
                    else:
                        expected_price = cap * float(other_style["high"])
                    default_ma = (
                        ma_threshold
                        * (1.02 + plan_index * 0.16)
                        * other_variation
                        * float(other_style["ma"])
                    )
                    default_qi = qi_large * float(other_style["qi"]) if plan_index >= 1 else 0.0
                    default_mi = mi_large * float(other_style["mi"]) if plan_index >= 2 else 0.0
                    predicted_available = (
                        size
                        / max(1, city_competitors[index])
                        * round_market_ramp
                        * other_variation
                    )
                    if prior_other:
                        default_ma = max(default_ma, float(prior_other["ma_index"] or 0) * 1.03)
                        default_qi = max(default_qi, float(prior_other["qi_index"] or 0) * 1.03)
                        predicted_available = max(
                            predicted_available,
                            (
                                float(prior_other["sold"] or 0)
                                + float(prior_other["inventory"] or 0)
                            ) / prior_active * growth,
                        )
                    if prior_city:
                        default_mi = max(
                            default_mi,
                            float(prior_city["marketing"] or 0)
                            * (1.0 + other_agents * 0.10)
                            * 1.03,
                        )
                    rival_metrics[index].append({
                        "company_id": float(other_id),
                        "ma": default_ma,
                        "qi": default_qi,
                        "mi_effective": default_mi,
                        "price": expected_price,
                        "agents": float(other_agents),
                        "attack_weight": attack_priority(
                            other_id,
                            rival_is_bot=True,
                            rival_is_super=True,
                            index=index,
                            mi_effective=default_mi,
                            price=expected_price,
                            predicted_margin=prior_margin_by_company.get(other_id, 0.0),
                        ),
                        "strategic_weight": rival_asset_weight(other_id),
                        "available": predicted_available,
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
        if super_mode and use_mi:
            # MI owns an independent 20-CPI pool in every city. Once the route
            # is active, fund every opened market rather than only a shortlist.
            mi_selected = {
                index for index in selected if agent_plan[index][1] > 0
            }
        else:
            mi_selected = set(mi_priority[:mi_city_limit]) if use_mi else set()

        # Choose the CPI indices first. The affordable production count is then
        # derived from the strategy guide's complete group cost; investment is
        # never used as a blind cash sink after production has been calculated.
        ma_index_target = (
            ma_threshold
            * max(ma_round_buffer, 1.02 + plan_index * 0.16)
            * variation
            * ma_strength
        )
        normal_mix = NORMAL_INVESTMENT_MIXES[(profile + plan_index * 5) % len(NORMAL_INVESTMENT_MIXES)]
        normal_phase = (1.00, 1.08, 1.20, 1.38, 1.58, 1.82, 2.08)[plan_index]
        if not super_mode:
            # Early rounds buy just enough of an opened CPI pool to compete;
            # later rounds widen naturally. Profiles rotate, so the field has
            # MA-, QI-, MI- and balanced plans without an expensive optimiser.
            ma_index_target = ma_threshold * max(
                1.02,
                float(normal_mix["ma"]) * normal_phase * rng.uniform(0.96, 1.05),
            )
            if distress_liquidation:
                ma_index_target = ma_threshold * rng.uniform(1.02, 1.10)
        if super_mode:
            rival_ma = _upper_typical([item["ma"] for index in selected for item in rival_metrics[index]])
            ma_index_target = min(
                5000.0,
                max(ma_index_target, ma_threshold * super_aggression, rival_ma * super_aggression),
            )
        qi_line = max(float(markets[index]["max_price"]) / 50.0 for index in selected)
        qi_index_target = qi_line * max(qi_safe_multiplier, qi_strength)
        if not super_mode:
            qi_index_target = qi_line * max(
                qi_safe_multiplier,
                float(normal_mix["qi"]) * normal_phase * rng.uniform(0.95, 1.06),
            )
            if distress_liquidation:
                use_qi = False
                qi_index_target = 0.0
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
            if not super_mode:
                target_mi = threshold * max(
                    1.02,
                    float(normal_mix["mi"]) * normal_phase * rng.uniform(0.95, 1.06),
                )
                if distress_liquidation:
                    target_mi = 0.0
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

        if distress_liquidation:
            # Low-price CPI is the recovery tool. Do not compound the problem
            # by opening QI/MI pools or research while operating cash is tight.
            use_qi = False
            use_mi = False
            mi_selected = set()
            marketing_targets = {index: 0.0 for index in marketing_targets}

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
            current_workers = employee_count(conn, company_id, "worker")
            current_engineers = employee_count(conn, company_id, "engineer")
            worker_delta = _staff_delta(conn, company_id, "worker", round_no, worker_effective / worker_planning_multiplier)
            engineer_delta = _staff_delta(conn, company_id, "engineer", round_no, engineer_effective / engineer_planning_multiplier)
            if super_mode and production <= 0 and old_products > 0 and ma_index_target >= ma_threshold:
                # Inventory-only MA play: retain exactly one employee so the
                # whole MA investment becomes the MA index denominator.
                if current_workers > 0:
                    worker_delta = max(worker_delta, 1 - current_workers)
                    engineer_delta = -current_engineers
                elif current_engineers > 0:
                    worker_delta = -current_workers
                    engineer_delta = max(engineer_delta, 1 - current_engineers)
                else:
                    worker_delta = 1
                    engineer_delta = 0
            workers = max(0, current_workers + worker_delta)
            engineers = max(0, current_engineers + engineer_delta)
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
                low_price_unlocked = (
                    previous_price >= cap * 0.75
                    or late_game_low_price
                    or index in saturated_low_price_indices
                )
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
                ordinary_strategic_low_price = bool(
                    not super_mode
                    and index in selected
                    and agents_after > 0
                    and (late_game_low_price or index in saturated_low_price_indices)
                )
                if (distress_liquidation or ordinary_strategic_low_price) and index in selected and agents_after > 0:
                    # Flexible liquidation price: move from the last market
                    # average toward cost as inventory/cash pressure rises.
                    # Never deliberately sell below the real per-unit cost.
                    cost_floor = max(price_min, direct_unit_cost * 1.03)
                    average_ceiling = min(cap, max(price_min, previous_price))
                    if distress_liquidation:
                        pressure_depth = (
                            0.48
                            + min(0.30, prior_surplus_ratio * 0.70)
                            + min(0.14, max(0.0, 0.90 - cash_ratio) * 0.40)
                            + rng.uniform(-0.06, 0.07)
                        )
                        pressure_depth = min(0.94, max(0.42, pressure_depth))
                    else:
                        # Late/saturated markets undercut more gently than an
                        # emergency liquidation, preserving positive margin.
                        pressure_depth = (
                            0.22
                            + (0.10 if official_round >= total_rounds else 0.0)
                            + (0.12 if index in saturated_low_price_indices else 0.0)
                            + min(0.16, prior_surplus_ratio * 0.45)
                            + rng.uniform(-0.05, 0.07)
                        )
                        pressure_depth = min(0.72, max(0.16, pressure_depth))
                    reference = (
                        average_ceiling - (average_ceiling - cost_floor) * pressure_depth
                        if average_ceiling >= cost_floor else cost_floor
                    )
                elif super_mode and price_ratio_override is not None:
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
                        "company_id": int(rival.get("company_id", -(rival_number + 1))),
                        "ma_index": rival["ma"],
                        "qi_index": rival["qi"],
                        "mi_investment": rival["mi_effective"] / (1.0 + rival_agents * 0.10),
                        "price": rival["price"], "agents": rival_agents,
                        "attack_weight": float(rival.get("attack_weight", 0.0)),
                        "available": float(rival.get("available", 0.0)),
                    })
                    weighted_prices.append((rival["price"], max(1.0, rival.get("available", 1.0))))
                entries.append({
                    "company_id": company_id, "ma_index": ma_index, "qi_index": qi_index,
                    "mi_investment": float(row["marketing"]), "price": float(row["price"]),
                    "agents": int(row["agents_after"]),
                })
                weighted_prices.append((float(row["price"]), max(1.0, old_products + production)))
                market = markets[index]
                size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                average_price = investment_average_prices(
                    entries,
                    [weight for _, weight in weighted_prices],
                    fallback=previous_prices[index],
                    market_size=size,
                    max_price=float(market["max_price"]),
                    ma_large_threshold=ma_threshold,
                )
                own_cpi = allocate_city_cpi_for_company(
                    entries, market_size=size, max_price=float(market["max_price"]),
                    ma_large_threshold=ma_threshold, average_price=average_price,
                    market_average_price=previous_prices[index],
                    price_power=price_power,
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
            # Keep the organiser's original 1–8000 / 1–3000 search range for
            # the default KDS, but express it as live threshold multiples so a
            # different event scales correctly.
            ma_ceiling = max(1.0, ma_threshold * (8000.0 / 1300.0))
            qi_ceiling = max(1.0, qi_line * 6.0)
            mi_ratio_ceiling = 6.0 if use_mi else 0.0
            if competitive_mode:
                ma_ceiling = max(ma_ceiling, ma_threshold * 10.0)
                qi_ceiling = max(qi_ceiling, qi_line * 10.0)
                if use_mi:
                    rival_mi_ratios = []
                    for index in selected:
                        benefit = 1.0 + agent_plan[index][1] * 0.10
                        base_threshold = mi_thresholds.get(index, 0.0) * benefit
                        if base_threshold <= 0.0:
                            continue
                        rival_mi_ratios.extend(
                            float(item.get("mi_effective", 0.0)) / base_threshold
                            for item in rival_metrics[index]
                            if float(item.get("attack_weight", 0.0)) > 0.0
                        )
                    mi_ratio_ceiling = max(
                        mi_ratio_ceiling,
                        min(24.0, max(rival_mi_ratios, default=0.0) * 1.30),
                    )
            field_ma = min(ma_ceiling, max(1.0, ma_index_target))
            field_qi = min(qi_ceiling, max(1.0, qi_index_target))
            field_mi_ratio = max(
                (
                    marketing_targets.get(index, 0.0) / mi_thresholds[index]
                    for index in selected
                    if index in mi_selected and mi_thresholds.get(index, 0.0) > 0
                ),
                default=0.0,
            )
            field_mi_ratio = min(mi_ratio_ceiling, max(0.0, field_mi_ratio))
            leader_ma = field_ma
            leader_qi = field_qi
            leader_mi_ratio = field_mi_ratio
            price_ratio_candidates = {
                min(0.98, max(0.75, high_price_ratio)), 0.95, 0.88, 0.81, 0.75,
            }
            if previous_leader:
                leader_ma = min(ma_ceiling, max(1.0, float(previous_leader["ma"])))
                leader_qi = min(qi_ceiling, max(1.0, float(previous_leader["qi"])))
                for index in selected:
                    leader_city = previous_leader["cities"].get(str(markets[index]["city"]))
                    if not leader_city:
                        continue
                    if use_mi and mi_thresholds.get(index, 0.0) > 0:
                        leader_mi_ratio = max(
                            leader_mi_ratio,
                            min(
                                mi_ratio_ceiling,
                                max(0.0, float(leader_city["marketing"]) / mi_thresholds[index]),
                            ),
                        )
                    cap = min(price_max, float(markets[index]["max_price"]))
                    if cap > 0:
                        price_ratio_candidates.add(min(
                            0.98,
                            max(price_min / cap, float(leader_city["price"]) / cap),
                        ))
            if late_game_low_price or saturated_low_price_indices or any(
                previous_prices[index] >= min(price_max, float(markets[index]["max_price"])) * 0.75
                for index in selected
            ) or all_super_field or any(
                current_pressure.get(index, 0.0) >= 0.60 for index in selected
            ):
                # Once the low-price condition opens, search continuously down
                # to the configured floor and include small undercuts of every
                # visible rival price. MA/QI=1 and MI=0 form the pure-price path.
                minimum_ratio = max(
                    price_min / max(1.0, min(price_max, float(markets[index]["max_price"])))
                    for index in selected
                )
                # Low-price candidates use normalized price gaps transformed
                # to the live KDS exponent.  This gives comparable CPI-weight
                # steps across an 8th-power regional game and a 20th-power
                # national game without blindly jumping thousands of yuan.
                representative_ratios = [
                    previous_prices[index]
                    / max(1.0, min(price_max, float(markets[index]["max_price"])))
                    for index in selected
                ]
                previous_ratio = sum(representative_ratios) / max(1, len(representative_ratios))
                for base_depth in (0.08, 0.16, 0.28, 0.42, 0.58, 0.74, 0.88, 0.97):
                    depth = _kds_price_depth(base_depth, price_power)
                    candidate_ratio = previous_ratio - (previous_ratio - minimum_ratio) * depth
                    price_ratio_candidates.add(round(min(0.98, max(minimum_ratio, candidate_ratio)), 5))
                price_ratio_candidates.add(minimum_ratio)
                for index in selected:
                    cap = min(price_max, float(markets[index]["max_price"]))
                    if cap <= 0 or not (
                        previous_prices[index] >= cap * 0.75
                        or late_game_low_price
                        or index in saturated_low_price_indices
                        or all_super_field
                        or current_pressure.get(index, 0.0) >= 0.60
                    ):
                        continue
                    rival_prices = [
                        float(rival["price"])
                        for rival in rival_metrics[index]
                        if float(rival["price"]) > 0
                    ]
                    if rival_prices:
                        candidate_price = _price_for_weight_advantage(
                            previous_prices[index],
                            min(rival_prices),
                            price_min,
                            price_power,
                        )
                        price_ratio_candidates.add(round(
                            min(0.98, max(minimum_ratio, candidate_price / cap)),
                            5,
                        ))

            # Rival decisions and city KDS stay fixed during this Bot's search.
            # Build them once, while retaining the same summation order and
            # every candidate/CPI calculation used by the original search.
            active_market_count = max(1, sum(1 for index in selected if agent_plan[index][1] > 0))
            candidate_city_data: list[tuple[Any, ...]] = []
            forecast_interest = (
                max(0.0, float(bot["debt"] or 0) + float(loan_change))
                * float(home_market["interest_rate"] or 0)
            )
            component_labor = component_need * worker_need * worker_hours / 504.0 * worker_salary * 3
            product_labor = engineer_need * engineer_hours / 504.0 * engineer_salary * 3
            for index in selected:
                agents_after = agent_plan[index][1]
                if agents_after <= 0:
                    continue
                market = markets[index]
                cap = min(price_max, float(market["max_price"]))
                low_price_unlocked = (
                    previous_prices[index] >= cap * 0.75
                    or late_game_low_price
                    or index in saturated_low_price_indices
                    or all_super_field
                    or current_pressure.get(index, 0.0) >= 0.60
                )
                direct_unit_cost = (
                    component_need * float(home_market["component_material"]) * material_factor
                    + float(home_market["product_material"]) * material_factor
                    + component_labor + product_labor
                    + (transport_cost if str(market["city"]) != home else 0.0)
                )
                entries: list[dict[str, float | int]] = []
                rival_weights: list[float] = []
                rival_weighted_prices: list[float] = []
                rival_asset_weighted_supply: list[float] = []
                for rival_number, rival in enumerate(rival_metrics[index]):
                    rival_agents = max(1.0, rival.get("agents", 1.0))
                    entries.append({
                        "company_id": int(rival.get("company_id", -(rival_number + 1))),
                        "ma_index": rival["ma"],
                        "qi_index": rival["qi"],
                        "mi_investment": rival["mi_effective"] / (1.0 + rival_agents * 0.10),
                        "price": rival["price"], "agents": rival_agents,
                        "attack_weight": float(rival.get("attack_weight", 0.0)),
                        "available": float(rival.get("available", 0.0)),
                    })
                    weight = max(1.0, rival.get("available", 1.0))
                    rival_weights.append(weight)
                    rival_weighted_prices.append(rival["price"] * weight)
                    rival_asset_weighted_supply.append(
                        weight * float(rival.get("strategic_weight", 1.0))
                    )
                own_entry = {
                    "company_id": company_id, "ma_index": 0.0, "qi_index": 0.0,
                    "mi_investment": 0.0, "price": 0.0, "agents": agents_after,
                }
                entries.append(own_entry)
                size = float(market["population"]) * float(market["penetration"]) * growth ** max(0, official_round - 1)
                cpi_evaluator = prepare_city_cpi_for_company(
                    entries, target_company_id=company_id, market_size=size,
                    max_price=float(market["max_price"]), ma_large_threshold=ma_threshold,
                    price_power=price_power,
                    market_average_price=previous_prices[index],
                )
                # Price CPI is exceptionally fragile when the KDS uses a high
                # exponent: one later Bot can take almost the whole 40-point
                # pool with a tiny undercut.  Candidate scoring therefore keeps
                # a KDS-scaled shadow competitor instead of valuing a temporary
                # price monopoly as guaranteed revenue.  This is only a Bot
                # risk model; settlement continues to use the exact field.
                shadow_price_competitors = min(3.0, max(0.75, price_power / 12.0))
                aggregate_rival_weight = (
                    sum(rival_asset_weighted_supply) / sum(rival_weights)
                    if rival_weights and sum(rival_weights) > 0 else 1.0
                )
                candidate_city_data.append((
                    index, cap, low_price_unlocked, direct_unit_cost,
                    rival_weights, rival_weighted_prices, entries, size, cpi_evaluator,
                    shadow_price_competitors, aggregate_rival_weight,
                ))

            def evaluate_candidate(
                candidate_ma: float,
                candidate_qi: float,
                candidate_mi_ratio: float,
                candidate_price_ratio: float,
                force_full_output: bool = False,
            ) -> dict[str, Any]:
                pure_price_route = bool(
                    candidate_ma <= 1.00000001
                    and candidate_qi <= 1.00000001
                    and candidate_mi_ratio <= 1e-12
                )
                resolved_mi_ratio = candidate_mi_ratio
                if super_mode and use_mi and not pure_price_route:
                    # MA/QI naturally compound as the field invests more. MI is
                    # city-specific, so enforce the same progression separately
                    # in every opened city. Only a true pure-price route may use
                    # zero MI; there is no half-open MI strategy.
                    mi_phase_floor = min(
                        mi_ratio_ceiling,
                        1.02
                        + max(0, plan_index - 2) * 0.55
                        + (sequence_strength - 1.0) * 2.0,
                    )
                    resolved_mi_ratio = max(resolved_mi_ratio, mi_phase_floor)
                candidate_marketing = {
                    index: (
                        mi_thresholds.get(index, 0.0) * resolved_mi_ratio
                        if index in mi_selected and agent_plan[index][1] > 0 else 0.0
                    )
                    for index in selected
                }
                candidate_group_cost = (
                    base_group_cost
                    + candidate_ma * (group["workers"] + group["engineers"])
                    + candidate_qi * group["products"]
                )
                # QI also applies to old stock. Omitting this fixed part made
                # inventory-heavy low-price plans look cheaper than they are.
                candidate_fixed = (
                    agent_cost
                    + sum(candidate_marketing.values())
                    + candidate_qi * old_products * 1.2
                )
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
                        "score": (-1, -math.inf, 0.0, -math.inf, candidate_price_ratio),
                        "ma": candidate_ma, "qi": candidate_qi,
                        "marketing": candidate_marketing, "groups": candidate_groups,
                        "price_ratio": candidate_price_ratio, "coverage": 0.0,
                        "predicted_profit": -math.inf, "risk_profit": -math.inf,
                        "predicted_sold": 0.0, "sell_ratio": 0.0,
                        "marketing_total": sum(candidate_marketing.values()),
                        "rival_damage": 0.0, "full_output": force_full_output,
                        "starting_assets": own_strategy_assets,
                        "ending_assets": -math.inf,
                    }

                city_capacities: list[tuple[int, float, float]] = []
                rival_damage = 0.0
                for (
                    index, cap, low_price_unlocked, direct_unit_cost,
                    rival_weights, rival_weighted_prices, average_entries, size, cpi_evaluator,
                    shadow_price_competitors, aggregate_rival_weight,
                ) in candidate_city_data:
                    effective_ratio = (
                        candidate_price_ratio
                        if low_price_unlocked or candidate_price_ratio >= 0.75
                        else min(0.98, max(0.75, high_price_ratio))
                    )
                    candidate_price = cap * effective_ratio
                    candidate_price = min(cap, max(price_min, candidate_price, direct_unit_cost * 1.03))
                    own_weight = max(1.0, candidate_available / active_market_count)
                    average_entries = [dict(entry) for entry in average_entries]
                    average_entries[-1].update({
                        "ma_index": candidate_ma,
                        "qi_index": candidate_qi,
                        "mi_investment": candidate_marketing[index],
                        "price": candidate_price,
                        "agents": agent_plan[index][1],
                    })
                    average_price = investment_average_prices(
                        average_entries,
                        [*rival_weights, own_weight],
                        fallback=previous_prices[index],
                        market_size=size,
                        max_price=cap,
                        ma_large_threshold=ma_threshold,
                    )
                    own_cpi = cpi_evaluator.evaluate(
                        ma_index=candidate_ma, qi_index=candidate_qi,
                        mi_investment=candidate_marketing[index],
                        price=candidate_price, average_price=average_price,
                    )
                    market_average = float(previous_prices[index])
                    if candidate_price < market_average:
                        own_price_weight = (market_average - candidate_price) ** price_power
                        known_price_weight = sum(cpi_evaluator.price_weights)
                        known_price_cpi = (
                            PRICE_CPI_TOTAL * own_price_weight
                            / max(own_price_weight + known_price_weight, 1e-300)
                        )
                        resilient_price_cpi = (
                            PRICE_CPI_TOTAL * own_price_weight
                            / max(
                                known_price_weight
                                + own_price_weight * (1.0 + shadow_price_competitors * 1.35),
                                1e-300,
                            )
                        )
                        own_cpi = max(0.0, own_cpi - known_price_cpi + resilient_price_cpi)
                    own_capacity = size * own_cpi / 100.0
                    city_capacities.append((index, own_capacity, candidate_price))
                    rival_available = sum(rival_weights)
                    rival_average_price = (
                        sum(rival_weighted_prices) / rival_available
                        if rival_available > 0 else candidate_price
                    )
                    # In a crowded market, every unit of capacity captured by
                    # this candidate removes revenue that the preset rivals
                    # could otherwise serve. A profitable undercut also reduces
                    # their pricing room. This is a search objective only; the
                    # engine's CPI and settlement formulas remain untouched.
                    own_city_supply = min(
                        own_capacity,
                        float(candidate_available) / active_market_count,
                    )
                    displaced = min(
                        own_city_supply,
                        max(0.0, rival_available + own_city_supply - size),
                    )
                    congestion = min(
                        1.0,
                        max(
                            0.0,
                            (rival_available + own_city_supply) / max(1.0, size) - 0.75,
                        ) / 0.25,
                    )
                    undercut_ratio = max(
                        0.0,
                        (rival_average_price - candidate_price)
                        / max(1.0, rival_average_price),
                    )
                    pressured = (
                        min(own_city_supply, rival_available)
                        * undercut_ratio
                        * 0.25
                        * congestion
                    )
                    rival_damage += (
                        (displaced + pressured)
                        * max(1.0, rival_average_price)
                        * aggregate_rival_weight
                    )

                total_capacity = sum(capacity for _, capacity, _ in city_capacities)
                # High investment does not require maximum production. Keep a
                # small demand buffer, but never pay for complete groups whose
                # products are not forecast to sell. This is the high-CPI,
                # controlled-output strategy used in saturated/high-price play.
                capacity_safety = (
                    0.94 if last_round
                    else 0.98 if official_round >= max(1, total_rounds - 1)
                    else 1.03
                )
                profitable_group_limit = max(
                    0,
                    int(math.ceil(
                        max(0.0, total_capacity * capacity_safety - old_products)
                        / max(1.0, group["products"])
                    )),
                )
                early_expansion = bool(
                    official_round <= max(2, math.ceil(total_rounds * 0.50))
                    and not all_markets_full
                    and market_pressure < 0.60
                    and prior_surplus_ratio < 0.20
                )
                if (
                    not force_full_output
                    and not early_expansion
                    and profitable_group_limit < candidate_groups
                ):
                    candidate_groups = profitable_group_limit
                    candidate_production = int(math.floor(candidate_groups * group["products"]))
                    candidate_available = old_products + candidate_production
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
                # With inventory but no new production, a pure MA strategy
                # keeps one employee; model that one-person MA spend here.
                control_ma_cost = candidate_ma if candidate_groups == 0 and old_products > 0 else 0.0
                predicted_cost = (
                    candidate_fixed
                    + candidate_groups * candidate_group_cost
                    + control_ma_cost
                    + predicted_transport
                )
                predicted_revenue = predicted_sold * predicted_price
                predicted_pre_tax = predicted_revenue - predicted_cost - forecast_interest
                predicted_tax = max(0.0, predicted_pre_tax * tax_rate)
                predicted_profit = predicted_pre_tax - predicted_tax
                # A conservative revenue haircut protects the late-game
                # strategy from a later undercut or small CPI forecast error.
                # Safe profit wins first; near break-even market filling is
                # allowed only when no safely profitable candidate exists.
                risk_pre_tax = predicted_revenue * 0.92 - predicted_cost - forecast_interest
                risk_profit = risk_pre_tax - max(0.0, risk_pre_tax * tax_rate)
                non_loss = int(predicted_profit >= 0)
                # Profit is the primary objective. Sell-through and CPI/stock
                # fit break ties between similarly profitable strategies, so a
                # pure low-price plan is selected only when it truly earns more.
                return {
                    "score": (
                        non_loss,
                        predicted_profit,
                        risk_profit,
                        sell_ratio,
                        -abs(cpi_coverage - 1.0),
                        candidate_price_ratio,
                    ),
                    "ma": candidate_ma, "qi": candidate_qi,
                    "marketing": candidate_marketing, "groups": candidate_groups,
                    "price_ratio": candidate_price_ratio,
                    "coverage": cpi_coverage,
                    "predicted_profit": predicted_profit,
                    "risk_profit": risk_profit,
                    "predicted_sold": predicted_sold,
                    "sell_ratio": sell_ratio,
                    "marketing_total": sum(candidate_marketing.values()),
                    "rival_damage": rival_damage,
                    "full_output": force_full_output,
                    "starting_assets": own_strategy_assets,
                    "ending_assets": own_strategy_assets + predicted_profit,
                }

            # Use field-informed upper bounds plus deliberately high legal
            # bounds. For each strategy family, binary-search the point where
            # CPI capacity reaches stock. Every visited point still competes on
            # predicted profit, so the search does not blindly maximise CPI.
            strategy_upper_bounds = {
                (ma_ceiling, qi_ceiling, mi_ratio_ceiling),
                (ma_ceiling, 1.0, 0.0),
                (1.0, qi_ceiling, 0.0),
                (1.0, 1.0, mi_ratio_ceiling),
                (ma_ceiling, qi_ceiling, 0.0),
                (ma_ceiling, 1.0, mi_ratio_ceiling),
                (1.0, qi_ceiling, mi_ratio_ceiling),
                (ma_ceiling, field_qi, field_mi_ratio),
                (field_ma, qi_ceiling, field_mi_ratio),
                (field_ma, field_qi, mi_ratio_ceiling),
                (ma_ceiling, qi_ceiling, field_mi_ratio),
                (ma_ceiling, field_qi, mi_ratio_ceiling),
                (field_ma, qi_ceiling, mi_ratio_ceiling),
                (field_ma, field_qi, field_mi_ratio),
                (leader_ma, leader_qi, leader_mi_ratio),
            }
            best_candidate: dict[str, Any] | None = None
            candidate_cache: dict[tuple[float, float, float, float, bool], dict[str, Any]] = {}

            def test_candidate(
                candidate_ma: float,
                candidate_qi: float,
                candidate_mi_ratio: float,
                candidate_price_ratio: float,
                force_full_output: bool = False,
            ) -> dict[str, Any]:
                nonlocal best_candidate
                key = (
                    round(candidate_ma, 8), round(candidate_qi, 8),
                    round(candidate_mi_ratio, 8), round(candidate_price_ratio, 8),
                    bool(force_full_output),
                )
                candidate = candidate_cache.get(key)
                if candidate is None:
                    candidate = evaluate_candidate(
                        candidate_ma,
                        candidate_qi,
                        candidate_mi_ratio,
                        candidate_price_ratio,
                        force_full_output,
                    )
                    candidate_cache[key] = candidate
                if best_candidate is None or candidate["score"] > best_candidate["score"]:
                    best_candidate = candidate
                return candidate

            for candidate_price_ratio in sorted(price_ratio_candidates, reverse=True):
                # Pure-price path is always retained.
                test_candidate(1.0, 1.0, 0.0, candidate_price_ratio)
                first_full_low_price = bool(
                    all_super_field
                    and bot_position == 0
                    and candidate_price_ratio < 0.75
                )
                if first_full_low_price:
                    # The first sequential Bot has no real current-round Super
                    # decisions to copy. Explicitly compare a cash-max output
                    # low-price route against its controlled-output candidates.
                    test_candidate(
                        1.0, 1.0, 0.0, candidate_price_ratio,
                        force_full_output=True,
                    )
                for upper_ma, upper_qi, upper_mi_ratio in strategy_upper_bounds:
                    low_scale = 0.0
                    high_scale = 1.0
                    test_candidate(upper_ma, upper_qi, upper_mi_ratio, candidate_price_ratio)
                    if first_full_low_price:
                        test_candidate(
                            upper_ma, upper_qi, upper_mi_ratio,
                            candidate_price_ratio,
                            force_full_output=True,
                        )
                    for _ in range(7):
                        scale = (low_scale + high_scale) / 2.0
                        candidate = test_candidate(
                            1.0 + (upper_ma - 1.0) * scale,
                            1.0 + (upper_qi - 1.0) * scale,
                            upper_mi_ratio * scale,
                            candidate_price_ratio,
                        )
                        if float(candidate["coverage"]) >= 1.0:
                            high_scale = scale
                        else:
                            low_scale = scale

            if competitive_mode and candidate_cache:
                # Exact targeted pressure is more expensive than the own-CPI
                # search, so run it only on a diverse shortlist after the
                # binary searches finish. Baseline CPI is cached per city;
                # each shortlisted plan is then compared against that same
                # pre-entry field to measure target CPI loss and new surplus.
                candidate_values = list(candidate_cache.values())
                shortlisted: dict[int, dict[str, Any]] = {}

                def include_shortlist(rows: list[dict[str, Any]], limit: int = 24) -> None:
                    for row in rows[:limit]:
                        shortlisted[id(row)] = row

                include_shortlist(sorted(
                    candidate_values,
                    key=lambda row: float(row.get("predicted_profit", -math.inf))
                    + float(row.get("rival_damage", 0.0)),
                    reverse=True,
                ), 32)
                include_shortlist(sorted(
                    candidate_values,
                    key=lambda row: float(row.get("marketing_total", 0.0)),
                    reverse=True,
                ))
                include_shortlist(sorted(
                    candidate_values,
                    key=lambda row: float(row.get("price_ratio", 1.0)),
                ))
                include_shortlist(sorted(
                    candidate_values,
                    key=lambda row: float(row.get("ma", 0.0)) + float(row.get("qi", 0.0)),
                    reverse=True,
                ))
                baseline_by_city: dict[int, dict[int, dict[str, Any]]] = {}

                for candidate in shortlisted.values():
                    candidate_available = old_products + int(candidate["groups"]) * int(group["products"])
                    targeted_damage = 0.0
                    target_cpi_drop = 0.0
                    target_surplus = 0.0
                    for (
                        index, cap, low_price_unlocked, direct_unit_cost,
                        rival_weights, _rival_weighted_prices, base_entries_with_own,
                        size, _cpi_evaluator, _shadow_price_competitors,
                        _aggregate_rival_weight,
                    ) in candidate_city_data:
                        base_entries = [dict(entry) for entry in base_entries_with_own[:-1]]
                        target_entries = [
                            entry for entry in base_entries
                            if float(entry.get("attack_weight", 0.0)) > 0.0
                        ]
                        if not target_entries:
                            continue
                        baseline = baseline_by_city.get(index)
                        if baseline is None:
                            baseline_averages = investment_average_prices(
                                base_entries,
                                rival_weights,
                                fallback=previous_prices[index],
                                market_size=size,
                                max_price=float(markets[index]["max_price"]),
                                ma_large_threshold=ma_threshold,
                            )
                            baseline_rows = allocate_city_cpi(
                                base_entries,
                                market_size=size,
                                max_price=float(markets[index]["max_price"]),
                                ma_large_threshold=ma_threshold,
                                price_power=price_power,
                                average_price=baseline_averages,
                                market_average_price=previous_prices[index],
                            )
                            baseline = {
                                int(row["company_id"]): row for row in baseline_rows
                            }
                            baseline_by_city[index] = baseline

                        effective_ratio = (
                            float(candidate["price_ratio"])
                            if low_price_unlocked or float(candidate["price_ratio"]) >= 0.75
                            else min(0.98, max(0.75, high_price_ratio))
                        )
                        candidate_price = min(
                            cap,
                            max(price_min, cap * effective_ratio, direct_unit_cost * 1.03),
                        )
                        own_entry = dict(base_entries_with_own[-1])
                        own_entry.update({
                            "ma_index": float(candidate["ma"]),
                            "qi_index": float(candidate["qi"]),
                            "mi_investment": float(candidate["marketing"].get(index, 0.0)),
                            "price": candidate_price,
                            "agents": agent_plan[index][1],
                        })
                        after_entries = [*base_entries, own_entry]
                        own_weight = max(1.0, float(candidate_available) / active_market_count)
                        after_averages = investment_average_prices(
                            after_entries,
                            [*rival_weights, own_weight],
                            fallback=previous_prices[index],
                            market_size=size,
                            max_price=float(markets[index]["max_price"]),
                            ma_large_threshold=ma_threshold,
                        )
                        after_rows = allocate_city_cpi(
                            after_entries,
                            market_size=size,
                            max_price=float(markets[index]["max_price"]),
                            ma_large_threshold=ma_threshold,
                            price_power=price_power,
                            average_price=after_averages,
                            market_average_price=previous_prices[index],
                        )
                        after = {int(row["company_id"]): row for row in after_rows}
                        for target in target_entries:
                            target_id = int(target["company_id"])
                            before_row = baseline.get(target_id)
                            after_row = after.get(target_id)
                            if not before_row or not after_row:
                                continue
                            weight = float(target.get("attack_weight", 0.0))
                            before_cpi = float(before_row["total_cpi"])
                            after_cpi = float(after_row["total_cpi"])
                            cpi_drop = max(0.0, before_cpi - after_cpi)
                            available = max(0.0, float(target.get("available", 0.0)))
                            before_sales = min(available, size * before_cpi / 100.0)
                            after_sales = min(available, size * after_cpi / 100.0)
                            lost_sales = max(0.0, before_sales - after_sales)
                            new_surplus = max(0.0, available - after_sales) - max(
                                0.0, available - before_sales,
                            )
                            target_price = max(1.0, float(target.get("price", 0.0)))
                            targeted_damage += (
                                lost_sales * target_price
                                + max(0.0, new_surplus) * target_price * 0.20
                            ) * weight
                            target_cpi_drop += cpi_drop * weight
                            target_surplus += max(0.0, new_surplus) * weight
                    candidate["targeted_damage"] = targeted_damage
                    candidate["target_cpi_drop"] = target_cpi_drop
                    candidate["target_surplus"] = target_surplus

            # Select by relative advantage across every evaluated plan. Profiles
            # still generate different candidate grids and rival responses, but
            # they cannot override the economic result after the search.
            empirical_candidate = _select_empirical_super_candidate(
                list(candidate_cache.values()),
                profile,
                tactical_price_allowed=bool(
                    late_game_low_price
                    or saturated_low_price_indices
                    or prior_surplus_ratio >= 0.50
                    or all_super_field
                    or market_pressure >= 0.60
                ),
                profit_target=profit_target,
                competitive_mode=competitive_mode,
                sacrifice_allowed=bool(
                    last_round
                    and competitive_mode
                    and next(
                        (
                            rival_id for rival_id in reversed(ranked_super_ids)
                            if rival_id in competitive_super_ids
                        ),
                        None,
                    ) == company_id
                ),
            )
            if empirical_candidate is not None:
                best_candidate = empirical_candidate

            if best_candidate is not None:
                chosen_predicted_profit = float(best_candidate.get("predicted_profit", 0.0))
                ma_index_target = float(best_candidate["ma"])
                qi_index_target = float(best_candidate["qi"])
                marketing_targets = dict(best_candidate["marketing"])
                # Preserve the profit-selected production level. The former
                # post-search expansion silently replaced it with the maximum
                # cash-affordable output and forced low-price liquidation.
                chosen_groups = max(0, int(best_candidate["groups"]))
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
            and not distress_liquidation
            and projected_cash - research_needed >= operating_reserve
            and (
                not super_mode
                or (
                    official_round >= 4
                    and cash_budget >= research_goal * 4.0
                    and projected_cash - research_needed >= cash_budget * 1.05
                    and (
                        chosen_predicted_profit is None
                        or chosen_predicted_profit - research_needed > profit_target
                    )
                )
            )
        )
        research = research_needed if research_ready else 0.0

        # Replace only this Bot, and only after its complete candidate search
        # has succeeded. If analysis stops before this point, its old decision
        # and every previously saved Bot remain untouched.
        if super_mode and existing_decision:
            conn.execute(
                "DELETE FROM city_decisions WHERE company_id=? AND round_no=?",
                (company_id, round_no),
            )
            conn.execute(
                "DELETE FROM decisions WHERE company_id=? AND round_no=?",
                (company_id, round_no),
            )
        conn.execute(
            "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,"
            "engineer_salary,management_investment,production_volume,quality_investment,research_investment,submitted_at,is_draft) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, round_no, loan_change, budget["worker_delta"], worker_salary, budget["engineer_delta"],
             engineer_salary, budget["management"], production, budget["quality"], research, now_iso(), int(super_mode)),
        )
        for row in city_rows:
            conn.execute(
                "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                "VALUES(?,?,?,?,?,?,0)",
                (company_id, round_no, row["city"], row["agent_delta"], row["marketing"], row["price"]),
            )
        # Release the SQLite writer lock between expensive Super Bot searches.
        # A rerun resumes after this saved Bot instead of rebuilding it.
        if super_mode:
            conn.commit()
            frozen_super_ids.add(company_id)
        submitted += 1
        if progress_callback:
            progress_callback(submitted, max(1, progress_total), str(bot["code"]))
    if super_mode and submitted and not defer_rebalance:
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
    replace_existing: bool = False,
    *,
    target_ids: set[int] | None = None,
    defer_rebalance: bool = False,
) -> int:
    """Super bots wait until every non-super team has submitted."""
    missing = one(
        conn,
        "SELECT COUNT(*) AS n FROM companies c WHERE c.is_super_bot=0 AND NOT EXISTS "
        "(SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=? "
        "AND d.submitted_at IS NOT NULL AND d.is_draft=0)",
        (round_no,),
    )
    if missing and int(missing["n"]) > 0:
        raise ValueError("仍有真人玩家或普通 Bot 未提交，超级 Bot 暂不能读取本轮数据。")
    # Existing decisions remain in place during analysis. A normal call resumes
    # only missing Bots; explicit reanalysis replaces each Bot individually
    # after its new decision is ready.
    conn.commit()
    return _submit_bots(
        conn,
        round_no,
        True,
        progress_callback,
        replace_existing=replace_existing,
        target_ids=target_ids,
        defer_rebalance=defer_rebalance,
    )


def submit_super_bot_decisions(
    conn: sqlite3.Connection,
    round_no: int,
    progress_callback: Callable[[int, int, str], None] | None = None,
    replace_existing: bool = False,
    *,
    target_ids: set[int] | None = None,
    defer_rebalance: bool = False,
) -> int:
    """Serialize expensive submissions inside one Streamlit worker process."""
    with _SUPER_BOT_SUBMISSION_LOCK:
        return _submit_super_bot_decisions_locked(
            conn,
            round_no,
            progress_callback,
            replace_existing,
            target_ids=target_ids,
            defer_rebalance=defer_rebalance,
        )


def finalize_super_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Turn a complete analysed Super Bot batch into locked submissions."""
    with _SUPER_BOT_SUBMISSION_LOCK:
        total = int(one(
            conn,
            "SELECT COUNT(*) AS n FROM companies WHERE is_bot=1 AND is_super_bot=1",
        )["n"])
        if total <= 0:
            return 0
        market_total = int(one(conn, "SELECT COUNT(*) AS n FROM market_config")["n"])
        ready = int(one(
            conn,
            "SELECT COUNT(*) AS n FROM companies c JOIN decisions d ON d.company_id=c.id "
            "WHERE c.is_bot=1 AND c.is_super_bot=1 AND d.round_no=? "
            "AND d.submitted_at IS NOT NULL AND (SELECT COUNT(*) FROM city_decisions cd "
            "WHERE cd.company_id=c.id AND cd.round_no=?)=?",
            (round_no, round_no, market_total),
        )["n"])
        if ready != total:
            raise ValueError("超级 Bot 草稿尚未全部分析完成，不能正式提交。")
        conn.execute(
            "UPDATE decisions SET submitted_at=?,is_draft=0 WHERE round_no=? "
            "AND company_id IN (SELECT id FROM companies WHERE is_bot=1 AND is_super_bot=1)",
            (now_iso(), round_no),
        )
        conn.commit()
        return total


def rebalance_super_bot_decisions(conn: sqlite3.Connection, round_no: int) -> int:
    """Finish a split submission after every team's decision has been saved."""
    with _SUPER_BOT_SUBMISSION_LOCK:
        missing = one(
            conn,
            "SELECT COUNT(*) AS n FROM companies c WHERE NOT EXISTS "
            "(SELECT 1 FROM decisions d WHERE d.company_id=c.id AND d.round_no=? "
            "AND d.submitted_at IS NOT NULL)",
            (round_no,),
        )
        if missing and int(missing["n"]) > 0:
            raise ValueError("仍有玩家或 Bot 未提交，暂不能进行联合复算。")
        markets = [dict(row) for row in all_rows(conn, "SELECT * FROM market_config ORDER BY city")]
        if not markets:
            return 0
        _rebalance_super_bot_production(conn, round_no, markets)
        conn.commit()
        count = one(conn, "SELECT COUNT(*) AS n FROM companies WHERE is_bot=1 AND is_super_bot=1")
        return int(count["n"] if count else 0)
