from __future__ import annotations

from io import BytesIO
import unittest

from sim.report_pdf import build_round_report_pdf

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


def sample_report() -> dict:
    return {
        "key_metrics": {
            "total_assets": 18_000_000, "debt": 2_000_000, "net_assets": 16_000_000,
            "sales_revenue": 10_000_000, "cost": 6_000_000, "net_profit": 4_000_000,
        },
        "finance": {
            "round_begins": 5_500_000, "starting_debt": 0, "loan_change": 2_000_000,
            "worker_wages": 600_000, "engineer_wages": 300_000, "layoff_cash": 0,
            "layoff_debt": 0, "quit_penalty_cash": 0, "quit_penalty_debt": 0,
            "training": 10_000, "component_material": 800_000, "component_storage": 100_000,
            "product_material": 500_000, "product_storage": 90_000, "agents": 300_000,
            "marketing": 400_000, "quality": 300_000, "management": 200_000,
            "market_reports": 200_000, "research": 0, "transport": 100_000,
            "interest": 40_000, "tax": 800_000, "project_bonus": 500_000,
            "bonus_in_round_begins": True, "round_ends": 14_000_000,
        },
        "human_resources": {
            "workers": 100, "engineers": 30, "worker_salary": 3_000,
            "engineer_salary": 6_000, "average_worker_salary": 2_900,
            "average_engineer_salary": 5_900,
        },
        "production": {
            "planned": 1_000, "produced": 1_000, "components_per_product": 7,
            "components": 7_000, "component_used": 7_000, "sold": 900,
            "surplus": 100, "component_productivity": 70, "product_productivity": 33.333,
            "component_storage_before": 0, "component_storage_after": 7_000,
            "component_storage_increase": 7_000, "product_storage_before": 0,
            "product_storage_after": 1_000, "product_storage_increase": 1_000,
            "ma_index": 1_538.46, "qi_index": 300,
        },
        "research": {
            "active_patents_this_round": 0, "success": False, "patents_after": 0,
            "accumulated_after": 0, "probability": 0.987654321,
            "hidden_threshold": "PRIVATE_RESEARCH_SENTINEL",
        },
        "sales": [
            {"city": "广州", "agents_previous": 0, "agent_change": 1, "agents": 1,
             "agent_change_cost": 300_000, "marketing": 400_000, "cpi": 12.34,
             "sold": 900, "market_share": 0.075, "price": 9_800},
        ],
    }


def sample_markets() -> list[dict]:
    rows = [
        {"code": f"C{index:02d}", "ma_index": index * 100, "agents": 1,
         "marketing": index * 10_000, "qi_index": index * 3, "price": 9_800 - index,
         "sold": 100 + index, "market_share": 0.01 + index / 1_000}
        for index in range(1, 18)
    ]
    return [
        {"city": city, "population": 4_000_000, "penetration": 0.02,
         "market_size": 80_000, "total_volume": 20_000, "average_price": 8_800,
         "rows": rows}
        for city in ("广州", "成都", "武汉")
    ]


@unittest.skipIf(PdfReader is None, "pypdf is needed for report PDF verification")
class OfficialReportPDFTest(unittest.TestCase):
    def build(self) -> tuple[bytes, PdfReader]:
        pdf = build_round_report_pdf(
            {"code": "C01", "name": "Test", "home_city": "广州"},
            3, sample_report(), 2, sample_markets(),
        )
        return pdf, PdfReader(BytesIO(pdf))

    def test_report_is_one_continuous_tall_sheet(self):
        pdf, reader = self.build()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(len(reader.pages), 1)
        page = reader.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 595.276, places=2)
        self.assertGreater(float(page.mediabox.height), 841.89 * 2)

    def test_official_sections_and_market_reports_keep_their_order(self):
        _, reader = self.build()
        text = reader.pages[0].extract_text()
        headings = [
            "Key Metrics", "Finance", "Human Resources", "Production",
            "Research Investment", "Market Report - Guangzhou",
            "Market Report - Chengdu", "Market Report - Wuhan",
        ]
        positions = [text.index(heading) for heading in headings]
        positions.insert(5, text.index("\nSales\n", positions[4]))
        self.assertEqual(positions, sorted(positions))
        self.assertLess(text.index("Tax deduction"), text.index("Project bonus"))

    def test_hidden_research_mechanics_are_not_exported(self):
        _, reader = self.build()
        text = reader.pages[0].extract_text()
        self.assertNotIn("PRIVATE_RESEARCH_SENTINEL", text)
        self.assertNotIn("98.7654321", text)
        self.assertNotIn("probability", text.lower())

    def test_equal_public_input_produces_identical_pdf(self):
        first, _ = self.build()
        second, _ = self.build()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
