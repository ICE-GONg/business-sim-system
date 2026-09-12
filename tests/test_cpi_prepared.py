from __future__ import annotations

import random
import unittest

from sim.cpi import allocate_city_cpi_for_company, prepare_city_cpi_for_company


class PreparedCPITests(unittest.TestCase):
    def test_reused_evaluator_matches_target_allocator_exactly(self) -> None:
        rng = random.Random(20260912)
        for player_count in (1, 2, 7, 25, 510):
            for target_index in {0, player_count - 1}:
                entries = [
                    {
                        "company_id": index,
                        "ma_index": rng.uniform(0, 13_000),
                        "qi_index": rng.uniform(0, 5_000),
                        "mi_investment": rng.uniform(0, 30_000_000),
                        "price": rng.uniform(3_500, 25_000),
                        "agents": rng.randrange(8),
                    }
                    for index in range(player_count)
                ]
                # Exercise zero thresholds and a recalculated price-pool mean
                # as well as the fixed city means used during Super Bot search.
                for threshold, market_average in ((0, None), (1300, 10_000)):
                    kwargs = {
                        "target_company_id": target_index,
                        "market_size": 88_000,
                        "max_price": 25_000,
                        "ma_large_threshold": threshold,
                        "market_average_price": market_average,
                    }
                    prepared = prepare_city_cpi_for_company(entries, **kwargs)
                    for iteration in range(30):
                        own = {
                            "ma_index": rng.uniform(0, 13_000) if iteration % 3 else 0,
                            "qi_index": rng.uniform(0, 5_000) if iteration % 5 else 0,
                            "mi_investment": rng.uniform(0, 30_000_000) if iteration % 7 else 0,
                            "price": rng.uniform(3_500, 25_000) if iteration % 9 else 0,
                            "agents": rng.randrange(8),
                        }
                        entries[target_index].update(own)
                        average = rng.uniform(0, 25_000) if iteration % 13 else None
                        expected = allocate_city_cpi_for_company(
                            entries, average_price=average, **kwargs,
                        )
                        actual = prepared.evaluate(**own, average_price=average)
                        self.assertEqual(actual, expected)

    def test_gift_cap_and_threshold_boundaries_match_exactly(self) -> None:
        entries = [
            {
                "company_id": index,
                "ma_index": 0.001,
                "qi_index": 0.001,
                "mi_investment": 0.001,
                "price": 10_000,
                "agents": 0,
            }
            for index in range(510)
        ]
        kwargs = {
            "target_company_id": 509,
            "market_size": 80_000,
            "max_price": 25_000,
            "ma_large_threshold": 1_300,
            "market_average_price": 10_000,
        }
        prepared = prepare_city_cpi_for_company(entries, **kwargs)
        for multiple in (0, 0.001, 1 / 500, 1, 4, 10):
            own = {
                "ma_index": 1_300 * multiple,
                "qi_index": 500 * multiple,
                "mi_investment": 8_000_000 / 3 * multiple,
                "price": 10_000,
            }
            entries[-1].update(own)
            self.assertEqual(
                prepared.evaluate(**own, average_price=10_000),
                allocate_city_cpi_for_company(entries, average_price=10_000, **kwargs),
            )

    def test_missing_company_and_empty_city_return_zero(self) -> None:
        prepared = prepare_city_cpi_for_company(
            [], target_company_id=9, market_size=80_000,
            max_price=25_000, ma_large_threshold=1_300,
        )
        self.assertEqual(
            prepared.evaluate(ma_index=1000, qi_index=1000, mi_investment=1000, price=10_000),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
