from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import unittest

from sim.defaults import DEFAULT_MARKETS, DEFAULT_SETTINGS
from sim.kds_pdf import build_public_kds_pdf

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


MARKET_KEYS = (
    "city", "home_enabled", "max_loan", "min_loan", "interest_rate",
    "worker_initial_salary", "engineer_initial_salary", "component_material",
    "product_material", "component_storage", "product_storage", "population",
    "penetration", "initial_avg_price", "max_price", "transport_cost",
    "worker_training_cost", "engineer_training_cost",
)


class PublicKDSPDFTest(unittest.TestCase):
    def setUp(self):
        self.settings = dict(DEFAULT_SETTINGS)
        self.markets = [dict(zip(MARKET_KEYS, values)) for values in DEFAULT_MARKETS]

    def test_admin_only_values_cannot_change_the_public_pdf(self):
        original_settings = deepcopy(self.settings)
        original_markets = deepcopy(self.markets)
        expected = build_public_kds_pdf(self.settings, self.markets)
        private_settings = dict(self.settings)
        private_settings.update({
            "research_25": 876_543_211, "research_75": 987_654_322,
            "research_buffer": 918_273_645, "research_probability_cap": 0.123456789,
            "research_hidden_threshold_multiplier": 98.7654321,
            "test_patent_repeat_boost": 87.654321,
            "qi_safe_multiplier": 76.54321, "cpi_ma_large_threshold": 765_432_111,
            "cpi_price_power": 73, "cpi_algorithm_version": "PRIVATE_CPI_SENTINEL",
            "admin_token": "PRIVATE_PASSWORD_SENTINEL",
        })
        private_markets = deepcopy(self.markets)
        for market in private_markets:
            market.update({
                "home_enabled": 0, "min_loan": 765_432_121,
                "max_price": 765_432_122, "private_notes": "PRIVATE_CITY_SENTINEL",
                "transport_cost": 765_432_123, "worker_training_cost": 765_432_124,
                "engineer_training_cost": 765_432_125,
            })
        self.assertTrue(expected.startswith(b"%PDF-"))
        self.assertEqual(build_public_kds_pdf(private_settings, private_markets), expected)
        self.assertEqual(self.settings, original_settings)
        self.assertEqual(self.markets, original_markets)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF text verification")
    def test_saved_public_changes_are_included_in_the_next_download(self):
        self.settings.update({"initial_cash": 19_876_543, "component_workers": 9, "patent_factor": 0.63})
        self.markets[0].update({
            "city": "公开测试城", "max_loan": 8_765_432, "penetration": 0.0375,
            "worker_initial_salary": 4_321, "engineer_initial_salary": 6_789,
            "population": 2_345_678,
        })
        pdf = build_public_kds_pdf(self.settings, self.markets)
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        for expected in (
            "公开测试城", "19,876,543", "8,765,432", "3.75%", "9 名无经验工人",
            "0.63", "4,321", "6,789", "2,345,678",
        ):
            self.assertIn(expected, text)
        for private_label in ("价格差幂次", "隐藏门槛", "真实成功概率", "概率上限", "cpi_price_power", "research_75"):
            self.assertNotIn(private_label, text)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for multipage verification")
    def test_many_cities_keep_every_row_and_repeat_table_headers(self):
        cities = [dict(self.markets[0], city=f"测试城市{index:03d}") for index in range(70)]
        pdf = build_public_kds_pdf(self.settings, cities)
        pages = [page.extract_text() for page in PdfReader(BytesIO(pdf)).pages]
        text = "\n".join(pages)
        self.assertGreaterEqual(len(pages), 5)
        for city in cities:
            self.assertEqual(text.count(city["city"]), 2)
        for page in pages:
            if "测试城市" not in page:
                continue
            self.assertIn("城市参数", page)
            if "资金与人员" in page:
                self.assertIn("第一轮最高贷款", page)
                self.assertIn("工程师初始月薪", page)
            if "材料、仓储与市场" in page:
                self.assertIn("零件材料单价", page)
                self.assertIn("初始渗透率", page)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF text verification")
    def test_fractional_production_requirements_and_training_fees_keep_precision(self):
        self.settings.update({
            "component_hours": 1.5,
            "product_hours": 7.125,
            "worker_training_cost": 123.45,
            "engineer_training_cost": 12.345678,
        })
        pdf = build_public_kds_pdf(self.settings, self.markets)
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        for expected in ("1.5 小时", "7.125 小时", "RMB 123.45", "RMB 12.345678"):
            self.assertIn(expected, text)
        self.assertIn("RMB 15,000,000", text)
        self.assertNotIn("15,000,000.000000", text)


if __name__ == "__main__":
    unittest.main()
