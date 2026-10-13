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


    def test_show_more_choices_keep_selection_and_respect_filters(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(OPERA), headless=True)
            try:
                page = browser.new_page()
                page.route("**/api/queue?*", lambda route: route.fulfill(json=[
                    {"id": i, "item_id": str(i), "review_state": "pending",
                     "scan_status": "match", "title": f"Listing {i}",
                     "listing_brand": "Generic" if i <= 125 else "Febreze"} for i in range(1, 261)
                ]))
                page.goto(URL)
                rows = page.locator("#queue-table tbody tr[data-row-id]")
                page.locator("#fr-pagination").get_by_text("Showing 100 of 260").wait_for()
                self.assertEqual(page.locator("#queue-table tfoot .show-more").count(), 0)
                self.assertTrue(page.locator("#fr-pagination").evaluate("el => el.closest('.fr-toolbar') !== null"))
                self.assertLess(page.locator("#fr-pagination").bounding_box()["x"],
                                page.locator(".fr-actions").bounding_box()["x"])
                self.assertEqual(rows.count(), 100)
                self.assertEqual(page.locator(".show-more-count option").all_text_contents(),
                                 ["100", "500", "2,000", "5,000", "10,000"])
                rows.first.locator("input[data-select-id]").click()
                page.locator(".show-more").click()
                self.assertEqual(rows.count(), 200)
                self.assertTrue(rows.first.locator("input[data-select-id]").is_checked())
                page.locator(".show-more-count").select_option("500")
                page.locator(".show-more").click()
                self.assertEqual(rows.count(), 260)  # no phantom rows when fewer than 500 remain
                self.assertIn("Showing 260 of 260", page.locator("#fr-pagination").inner_text())
                self.assertEqual(page.locator(".show-more").count(), 0)
                page.get_by_role("searchbox", name="Filter by Listing Brand").fill("Generic")
                self.assertEqual(rows.count(), 100)  # new filter resets the display limit
                page.locator(".show-more-count").select_option("2000")
                page.locator(".show-more").click()
                self.assertEqual(rows.count(), 125)
                self.assertIn("Showing 125 of 125 matching", page.locator("#fr-pagination").inner_text())
                self.assertTrue(rows.first.locator("input[data-select-id]").is_checked())
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
