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

    def pdf_pages(self, markets=None):
        pdf = build_public_kds_pdf(self.settings, self.markets if markets is None else markets)
        return PdfReader(BytesIO(pdf)).pages

    @staticmethod
    def compact(text):
        # Narrow table cells and long city names may wrap without losing content.
        return "".join(text.split())

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

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF layout verification")
    def test_default_kds_fits_one_portrait_a4_page(self):
        pages = self.pdf_pages()
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 595.276, places=2)
        self.assertAlmostEqual(float(page.mediabox.height), 841.890, places=2)
        text = page.extract_text()
        self.assertIn("Markets Details", text)
        self.assertIn("Equations & Ranges & Prices", text)
        self.assertLess(text.index("Markets Details"), text.index("Equations & Ranges & Prices"))
        header_text = text.split(min(market["city"] for market in self.markets))[0]
        # These labels fit on one line; compacting whitespace would hide a
        # regression where the final letter wraps into a second line.
        self.assertEqual(header_text.count("Component"), 2)
        self.assertEqual(header_text.count("Product"), 2)
        for market in self.markets:
            self.assertEqual(text.count(market["city"]), 1)

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
            "公开测试城", "19,876,543", "8,765,432", "3.75%", "9 Inexperienced Workers",
            "0.63", "4,321", "6,789", "2,345,678",
        ):
            self.assertIn(expected, text)
        for private_label in (
            "价格差幂次", "隐藏门槛", "真实成功概率", "概率上限", "cpi_price_power",
            "research_25", "research_75", "probability", "threshold",
        ):
            self.assertNotIn(private_label, text)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF text verification")
    def test_all_twelve_public_city_fields_are_in_the_combined_table(self):
        market = dict(self.markets[0], **{
            "city": "PublicFields公开字段",
            "max_loan": 8_123_456,
            "interest_rate": 0.0437,
            "worker_initial_salary": 3_219,
            "engineer_initial_salary": 6_541,
            "component_material": 287,
            "product_material": 659,
            "component_storage": 23,
            "product_storage": 97,
            "population": 2_345_679,
            "penetration": 0.0268,
            "initial_avg_price": 9_873,
        })
        text = "\n".join(page.extract_text() for page in self.pdf_pages([market]))
        table_text = self.compact(text.split("Equations & Ranges & Prices")[0])
        for expected in (
            "PublicFields公开字段", "¥8,123,456", "4.37%", "¥3,219", "¥6,541",
            "¥287", "¥659", "¥23", "¥97", "2,345,679", "2.68%", "¥9,873",
        ):
            self.assertIn(expected, table_text)
        self.assertEqual(table_text.count(market["city"]), 1)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for multipage verification")
    def test_many_cities_keep_every_row_and_repeat_table_headers(self):
        cities = [dict(self.markets[0], city=f"测试城市{index:03d}") for index in range(70)]
        pdf_pages = self.pdf_pages(cities)
        pages = [page.extract_text() for page in pdf_pages]
        text = "\n".join(pages)
        self.assertGreater(len(pages), 1)
        for city in cities:
            self.assertEqual(self.compact(text).count(city["city"]), 1)
        city_page_count = 0
        for pdf_page, page_text in zip(pdf_pages, pages):
            self.assertLess(float(pdf_page.mediabox.width), float(pdf_page.mediabox.height))
            compact_page = self.compact(page_text)
            if "测试城市" not in compact_page:
                continue
            city_page_count += 1
            for heading in (
                "Markets", "Initial Max Loan", "Interest Rate", "Initial Salary",
                "Material Unit Cost", "Storage Unit Cost", "Population",
                "Initial Penetration", "Initial Avg. Price", "Worker", "Engineer",
            ):
                self.assertIn(self.compact(heading), compact_page)
            # Component and Product subheaders appear under both cost groups.
            header_text = compact_page.split("测试城市")[0]
            self.assertEqual(header_text.count("Component"), 2)
            self.assertEqual(header_text.count("Product"), 2)
        self.assertGreater(city_page_count, 1)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF text verification")
    def test_empty_city_list_still_exports_public_rules(self):
        pages = self.pdf_pages([])
        self.assertEqual(len(pages), 1)
        text = pages[0].extract_text()
        self.assertIn("Markets Details", text)
        self.assertIn("Equations & Ranges & Prices", text)
        self.assertIn("15,000,000", text)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for PDF text verification")
    def test_long_mixed_city_names_keep_the_original_text(self):
        name = "North & 南岸 <港> International Development District 东部新城"
        cities = [dict(self.markets[0], city=name), dict(self.markets[1], city="Latin City")]
        original = deepcopy(cities)
        pages = self.pdf_pages(cities)
        text = self.compact("\n".join(page.extract_text() for page in pages))
        self.assertEqual(text.count(self.compact(name)), 1)
        self.assertEqual(text.count("LatinCity"), 1)
        self.assertIn("Equations&Ranges&Prices", text)
        self.assertEqual(cities, original)

    @unittest.skipIf(PdfReader is None, "pypdf is needed for multipage verification")
    def test_city_row_taller_than_a_page_splits_without_losing_text(self):
        cities = [dict(self.markets[0], city="城" * 600)]
        pages = self.pdf_pages(cities)
        page_texts = [page.extract_text() for page in pages]
        text = "\n".join(page_texts)
        self.assertEqual(text.count("城"), 600)
        city_pages = [page_text for page_text in page_texts if "城" in page_text]
        self.assertGreater(len(city_pages), 1)
        for page_text in city_pages:
            header_text = page_text.split("城")[0]
            for heading in (
                "Markets", "Initial Max Loan", "Interest Rate", "Initial Salary",
                "Material Unit Cost", "Storage Unit Cost", "Population",
                "Initial Penetration", "Initial Avg. Price", "Worker", "Engineer",
            ):
                self.assertIn(self.compact(heading), self.compact(header_text))
            self.assertEqual(header_text.count("Component"), 2)
            self.assertEqual(header_text.count("Product"), 2)
        self.assertIn("Equations & Ranges & Prices", text)

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
        compact_text = self.compact(text)
        for expected in ("1.5 Hours", "7.125 Hours", "¥123.45", "¥12.345678"):
            self.assertIn(self.compact(expected), compact_text)
        self.assertIn("¥15,000,000", compact_text)
        self.assertNotIn("15,000,000.000000", text)


if __name__ == "__main__":
    unittest.main()
