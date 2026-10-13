"""Read-only Opera/Playwright regression for review queue checkbox selection."""
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "http://127.0.0.1:8001/queue?state=pending&country_signal=all&job=latest"
OPERA = Path("/Applications/Opera.app/Contents/MacOS/Opera")


@unittest.skipUnless(OPERA.exists(), "Opera is needed for the browser regression")
class QueueSelectionTests(unittest.TestCase):
    def test_click_label_shift_range_keyboard_and_select_all(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(OPERA), headless=True)
            try:
                page = browser.new_page()
            # No real review records or decisions are changed by this test.
                page.route("**/api/queue?*", lambda route: route.fulfill(json=[
                    {"id": i, "item_id": str(i), "review_state": "pending",
                     "scan_status": "match", "title": f"Listing {i}",
                     "listing_brand": "Generic" if i == 1 else "Febreze"} for i in range(1, 6)
                ]))
                page.goto(URL)
                boxes = page.locator("input[data-select-id]")
                boxes.first.wait_for()
                self.assertEqual(boxes.count(), 5)
                headings = page.locator("#queue-table thead tr:first-child th")
                labels = [heading.locator("button").inner_text().replace("↕", "").replace("↑", "").replace("↓", "").strip()
                          if heading.locator("button").count() else heading.inner_text().strip()
                          for heading in headings.all()]
                self.assertEqual(labels[labels.index("Title") + 1:labels.index("Seller")], ["Listing Brand"])
                self.assertEqual(page.locator("#queue-table tbody tr:first-child td").nth(labels.index("Listing Brand")).inner_text(), "Generic")
                page.get_by_role("searchbox", name="Filter by Listing Brand").fill("Generic")
                self.assertIn("1 of 5 rows", page.locator("#fr-rowcount").inner_text())
                page.get_by_role("searchbox", name="Filter by Listing Brand").fill("")

                boxes.nth(0).click()
                self.assertTrue(boxes.nth(0).is_checked())
                self.assertIn("1 selected", page.locator("#fr-selected").inner_text())
                page.locator(".fr-box").nth(0).click(position={"x": 2, "y": 2})
                self.assertFalse(boxes.nth(0).is_checked())

                boxes.nth(0).click()
                boxes.nth(3).click(modifiers=["Shift"])
                self.assertEqual([boxes.nth(i).is_checked() for i in range(5)],
                                 [True, True, True, True, False])
                boxes.nth(1).click()
                self.assertFalse(boxes.nth(1).is_checked())
                boxes.nth(3).click(modifiers=["Shift"])
                self.assertEqual([boxes.nth(i).is_checked() for i in range(5)],
                                 [True, False, False, False, False])
                boxes.nth(0).focus()
                page.keyboard.press("Space")
                self.assertFalse(boxes.nth(0).is_checked())
                page.locator(".fr-all").click()
                self.assertTrue(all(boxes.nth(i).is_checked() for i in range(5)))
                page.locator(".fr-all").click()
                self.assertTrue(all(not boxes.nth(i).is_checked() for i in range(5)))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
