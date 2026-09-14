from __future__ import annotations

import io
import unittest

from PIL import Image

from sim.report_image import build_round_report_jpg


class LongReportImageTest(unittest.TestCase):
    def test_complete_report_is_one_clear_jpeg_long_image(self) -> None:
        report = {
            "key_metrics": {
                "total_assets": 28_847_412,
                "debt": 2_352_900,
                "net_assets": 26_494_512,
                "sales_revenue": 31_112_250,
                "cost": 9_200_000,
                "net_profit": 16_394_512,
            },
            "finance": {
                "round_begins": 6_500_000,
                "starting_debt": 0,
                "loan_change": 2_300_000,
                "worker_wages": 4_026_750,
                "engineer_wages": 880_930,
                "component_material": 1_493_388,
                "component_storage": 478_650,
                "product_material": 686_065,
                "product_storage": 210_606,
                "agents": 400_000,
                "marketing": 17_776,
                "quality": 3_191,
                "management": 594_644,
                "research": 600_000,
                "market_reports": 400_000,
                "transport": 80_000,
                "interest": 52_900,
                "tax": 5_464_837,
                "project_bonus": 3_600_000,
                "round_ends": 28_847_412,
            },
            "human_resources": {
                "workers": 455,
                "engineers": 51,
                "worker_salary": 2_950,
                "engineer_salary": 5_810,
                "average_worker_salary": 2_919,
                "average_engineer_salary": 5_760,
            },
            "production": {
                "planned": 3_191,
                "produced": 3_191,
                "components": 19_146,
                "components_per_product": 6,
                "sold": 3_191,
                "surplus": 0,
                "ma_index": 1_175.18,
                "qi_index": 1.0,
                "component_productivity": 42.436,
                "product_productivity": 63.55,
                "component_storage_increase": 19_146,
                "product_storage_increase": 3_191,
            },
            "research": {
                "success": False,
                "active_patents_this_round": 0,
                "patents_after": 0,
                "accumulated_after": 600_000,
            },
            "sales": [
                {
                    "city": "上海", "agents": 1, "agent_change": 1,
                    "marketing": 8_888, "price": 9_750,
                    "sold": 1_104, "cpi": 2.04, "market_share": 0.0204,
                },
                {
                    "city": "成都", "agents": 1, "agent_change": 1,
                    "marketing": 8_888, "price": 9_750,
                    "sold": 2_087, "cpi": 7.67, "market_share": 0.0767,
                },
            ],
        }
        market_rows = [
            {
                "code": f"C{index:02d}", "ma_index": 1_200 + index * 30,
                "agents": 1 + index % 3, "marketing": index * 50_000,
                "qi_index": 1 + index * 1.7, "price": 9_900 - index * 90,
                "sold": 800 + index * 37, "market_share": 0.01 + index / 1000,
            }
            for index in range(1, 18)
        ]
        data = build_round_report_jpg(
            {"code": "8", "name": "Hangzhou Entel Foreign Language School"},
            1,
            report,
            1,
            [
                {
                    "city": "上海", "population": 2_700_000,
                    "penetration": 0.02, "market_size": 54_000,
                    "total_volume": 14_951, "average_price": 4_644,
                    "rows": market_rows,
                },
                {
                    "city": "成都", "population": 1_700_000,
                    "penetration": 0.016, "market_size": 27_200,
                    "total_volume": 7_293, "average_price": 4_366,
                    "rows": market_rows[:8],
                },
            ],
        )
        self.assertTrue(data.startswith(b"\xff\xd8"))
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.width, 1180)
        self.assertGreater(image.height, 3_500)
        self.assertLess(image.getbbox()[3], image.height + 1)


if __name__ == "__main__":
    unittest.main()
