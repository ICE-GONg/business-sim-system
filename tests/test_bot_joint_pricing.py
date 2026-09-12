import math
import unittest

from sim.bot_joint_pricing import JointPriceTarget, solve_joint_prices


class JointBotPricingTests(unittest.TestCase):
    def solve(self, targets, *, power=8, external=(9_000,), **kwargs):
        return solve_joint_prices(
            base_average=10_000,
            external_prices=external,
            targets=targets,
            price_power=power,
            price_min=1_000,
            price_max=25_000,
            **kwargs,
        )

    def test_hits_absolute_target_shares_against_external_competitor(self):
        result = self.solve(
            [
                JointPriceTarget("alpha", 0.20, share_cap=0.40),
                JointPriceTarget("beta", 0.30, share_cap=0.40),
            ]
        )
        self.assertAlmostEqual(result["alpha"].achieved_share, 0.20, places=9)
        self.assertAlmostEqual(result["beta"].achieved_share, 0.30, places=9)

    def test_order_does_not_change_joint_prices(self):
        targets = [
            JointPriceTarget("c", 0.10, share_cap=0.35),
            JointPriceTarget("a", 0.25, share_cap=0.35),
            JointPriceTarget("b", 0.20, share_cap=0.35),
        ]
        forward = self.solve(targets, power=20)
        reverse = self.solve(reversed(targets), power=20)
        for key in ("a", "b", "c"):
            self.assertAlmostEqual(forward[key].price, reverse[key].price, places=10)
            self.assertAlmostEqual(
                forward[key].achieved_share,
                reverse[key].achieved_share,
                places=12,
            )

    def test_zero_external_weight_uses_shallow_anchor(self):
        result = self.solve(
            [JointPriceTarget("a", 0.60), JointPriceTarget("b", 0.40)],
            external=(10_000, 12_000),
            zero_external_anchor_gap=0.02,
        )
        self.assertAlmostEqual(result["a"].achieved_share, 0.60, places=9)
        self.assertAlmostEqual(result["b"].achieved_share, 0.40, places=9)
        self.assertGreaterEqual(min(value.price for value in result.values()), 9_800)
        self.assertLess(max(value.price for value in result.values()), 10_000)

    def test_zero_requests_do_not_manufacture_a_price_bid(self):
        result = self.solve(
            [JointPriceTarget("a", 0.0), JointPriceTarget("b", 0.0)],
            external=(),
        )
        self.assertEqual(result["a"].normalized_weight, 0.0)
        self.assertEqual(result["b"].normalized_weight, 0.0)
        self.assertEqual(result["a"].price, 10_000)
        self.assertEqual(result["b"].price, 10_000)

    def test_cost_floor_and_kds_price_bounds_are_respected(self):
        result = solve_joint_prices(
            base_average=10_000,
            external_prices=[8_000],
            targets=[
                JointPriceTarget("floor", 0.45, cost_floor=9_850, share_cap=0.50),
                JointPriceTarget("free", 0.35, cost_floor=500, share_cap=0.50),
            ],
            price_power=20,
            price_min=3_500,
            price_max=25_000,
        )
        self.assertGreaterEqual(result["floor"].price, 9_850)
        self.assertTrue(result["floor"].constrained)
        for value in result.values():
            self.assertGreaterEqual(value.price, 3_500)
            self.assertLessEqual(value.price, 10_000)

    def test_achieved_share_uses_actual_bounded_weight_denominator(self):
        result = solve_joint_prices(
            base_average=10_000,
            external_prices=[8_000],
            targets=[
                JointPriceTarget("floor", 0.45, cost_floor=9_850, share_cap=0.50),
                JointPriceTarget("free", 0.35, cost_floor=500, share_cap=0.50),
            ],
            price_power=20,
            price_min=3_500,
            price_max=25_000,
        )
        external_gap = (10_000 - 8_000) / 10_000
        external_weight = math.exp(20 * math.log(external_gap))
        denominator = external_weight + math.fsum(
            value.normalized_weight for value in result.values()
        )

        self.assertEqual(result["floor"].price, 9_850)
        for value in result.values():
            self.assertEqual(
                value.achieved_share,
                value.normalized_weight / denominator,
            )

    def test_power_twenty_needs_narrower_inter_bot_price_spread(self):
        targets = [JointPriceTarget("a", 0.10), JointPriceTarget("b", 0.30)]
        p8 = self.solve(targets, power=8)
        p20 = self.solve(targets, power=20)
        spread8 = abs(p8["a"].price - p8["b"].price)
        spread20 = abs(p20["a"].price - p20["b"].price)
        self.assertLess(spread20, spread8)

    def test_waterfill_honours_per_team_share_cap(self):
        result = self.solve(
            [
                JointPriceTarget("large", 0.90, share_cap=0.25),
                JointPriceTarget("small", 0.10, share_cap=0.70),
            ],
            total_share_cap=0.60,
        )
        self.assertAlmostEqual(result["large"].planned_share, 0.25, places=9)
        self.assertAlmostEqual(result["small"].planned_share, 0.35, places=9)
        self.assertLessEqual(result["large"].achieved_share, 0.25 + 1e-9)


if __name__ == "__main__":
    unittest.main()
