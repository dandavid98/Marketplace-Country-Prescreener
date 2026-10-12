"""Compliance rules for standard, generic-only and inclusive keyword scans."""
import unittest
from unittest.mock import patch

import marketplace_prescreener as app


class GenericModeTests(unittest.TestCase):
    def scan(self, brand, country, *, only=False, include=False, keyword="Febreze", sold=False):
        html = "<title>Febreze air freshener</title>"
        with patch.object(app, "fetch_html", return_value=html), \
             patch.object(app, "extract_listing_brand", return_value=brand), \
             patch.object(app, "extract_seller_page_url", return_value=""), \
             patch.object(app, "extract_signals", return_value=(country, "Seller", country, "evidence")), \
             patch.object(app, "is_walmart_sold_and_shipped", return_value=sold):
            return app.scan_listing(keyword, "https://www.walmart.com/ip/123", "China",
                                    generic_only=only, generic_include=include)[9]

    def test_country_and_brand_matrix(self):
        for brand, country, expected in [
            ("Febreze", "US", "unknown"),
            ("Febreze", "China", "match"),
            ("CleaningSupply", "US", "unknown"),
            ("CleaningSupply", "China", "match"),
            ("Generic", "US", "match"),
            ("Generic", "China", "match"),
            ("Unbranded", "Unknown", "match"),
            ("", "US", "unknown"),
        ]:
            with self.subTest(brand=brand, country=country):
                self.assertEqual(self.scan(brand, country, include=True), expected)

    def test_generic_only_still_excludes_real_brands(self):
        self.assertEqual(self.scan("CleaningSupply", "China", only=True), "unknown")
        self.assertEqual(self.scan("Generic", "US", only=True), "match")

    def test_standard_still_requires_country(self):
        self.assertEqual(self.scan("Generic", "US"), "unknown")
        self.assertEqual(self.scan("Febreze", "China"), "match")

    def test_no_keyword_or_walmart_sold_never_matches(self):
        self.assertEqual(self.scan("Generic", "US", include=True, keyword="Tide"), "unknown")
        self.assertEqual(self.scan("Generic", "US", include=True, sold=True), "walmart_sold")

    def test_inclusive_job_and_old_toggle(self):
        inclusive = app.create_job(["Febreze"], "China", 1, 1, "balanced", generic_include=True)
        legacy = app.create_job(["Febreze"], "China", 1, 1, "balanced", generic_only=True)
        try:
            self.assertTrue(app.JOBS[inclusive]["generic_include"])
            self.assertFalse(app.JOBS[inclusive]["generic_only"])
            self.assertTrue(app.JOBS[legacy]["generic_only"])
            self.assertFalse(app.JOBS[legacy]["generic_include"])
            self.assertIn("country_signal=all", app.job_page(inclusive).body.decode())
            page = app.home().body.decode()
            self.assertIn('value="include"', page)
            self.assertIn('value="only"', page)
        finally:
            app.JOBS.pop(inclusive)
            app.JOBS.pop(legacy)


if __name__ == "__main__":
    unittest.main()
