"""Opera E2E against an isolated database, never the real review database."""
import os
import sqlite3
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


URL = os.environ.get('PRESCREENER_CATEGORY_TEST_URL', 'http://127.0.0.1:8017')
DB = os.environ.get('PRESCREENER_CATEGORY_TEST_DB', '/tmp/prescreener-categories-qa-20261013.sqlite3')
OPERA = Path('/Applications/Opera.app/Contents/MacOS/Opera')
CATEGORY = 'Trademark Infringement Media'


@unittest.skipUnless(OPERA.exists() and Path(DB).exists(), 'QA DB and Opera required')
class CategoryBrowserTests(unittest.TestCase):
    def test_approved_rows_bulk_assign_and_remove_on_copy(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(OPERA), headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(URL + '/queue?state=approved&country_signal=all&job=all')
                boxes = page.locator('input[data-select-id]')
                boxes.first.wait_for()
                ids = [int(box.get_attribute('data-select-id')) for box in boxes.all()[:2]]
                with sqlite3.connect(DB) as conn:
                    before = dict(conn.execute('select id, review_category from reviews where id in (?, ?)', ids))
                try:
                    boxes.nth(0).click()
                    boxes.nth(1).click()
                    self.assertTrue(page.locator('#fr-category').is_enabled())
                    page.locator('#fr-category').select_option(CATEGORY)
                    self.assertTrue(page.locator('#fr-category-assign').is_enabled())
                    page.locator('#fr-category-assign').click()
                    page.get_by_text('Categorized 2 rows. Selection kept for your next action.').wait_for()
                    self.assertEqual(page.locator('input[data-select-id]:checked').count(), 2)
                    with sqlite3.connect(DB) as conn:
                        assigned = dict(conn.execute('select id, review_category from reviews where id in (?, ?)', ids))
                    self.assertEqual(set(assigned.values()), {CATEGORY})
                    for row_id in ids:
                        self.assertIn(CATEGORY, page.locator(
                            f'#queue-table tbody tr[data-row-id="{row_id}"]').inner_text())
                    page.locator('#queue-table select[data-filter="review_category"]').select_option(CATEGORY)
                    self.assertGreater(page.locator('#queue-table tbody tr[data-row-id]').count(), 0)
                    page.locator('#queue-table select[data-filter="review_category"]').select_option('')
                    page.locator('#fr-category').select_option('__clear__')
                    page.locator('#fr-category-assign').click()
                    page.get_by_text('Categorized 2 rows. Selection kept for your next action.').wait_for()
                    with sqlite3.connect(DB) as conn:
                        cleared = dict(conn.execute('select id, review_category from reviews where id in (?, ?)', ids))
                    self.assertEqual(set(cleared.values()), {''})
                    self.assertEqual(page.locator('input[data-select-id]:checked').count(), 2)
                    self.assertFalse(errors)
                finally:
                    # Restore the isolated QA copy even if an assertion fails.
                    with sqlite3.connect(DB) as conn:
                        conn.executemany('update reviews set review_category = ? where id = ?',
                                         [(before[row_id], row_id) for row_id in ids])
            finally:
                browser.close()
    def test_categorize_pending_then_approve_without_reselecting(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(OPERA), headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(URL + '/queue?state=pending&country_signal=all&job=all')
                box = page.locator('input[data-select-id]').first
                box.wait_for()
                row_id = int(box.get_attribute('data-select-id'))
                with sqlite3.connect(DB) as conn:
                    original = conn.execute('select review_state, review_category from reviews where id = ?',
                                            (row_id,)).fetchone()
                self.assertEqual(original[0], 'pending')
                try:
                    box.click()
                    page.locator('#fr-category').select_option(CATEGORY)
                    page.locator('#fr-category-assign').click()
                    page.get_by_text('Categorized 1 rows. Selection kept for your next action.').wait_for()
                    self.assertTrue(page.locator(f'input[data-select-id="{row_id}"]').is_checked())
                    self.assertIn(CATEGORY, page.locator(f'tr[data-row-id="{row_id}"]').inner_text())
                    page.locator('[data-bulk="approve"]').click()
                    page.locator('#fr-preview').wait_for(state='visible')
                    page.locator('#fr-confirm').click()
                    page.get_by_text('Approved 1 row', exact=False).wait_for()
                    with sqlite3.connect(DB) as conn:
                        current = conn.execute('select review_state, review_category from reviews where id = ?',
                                               (row_id,)).fetchone()
                    self.assertEqual(current, ('approved', CATEGORY))
                    self.assertFalse(errors)
                finally:
                    with sqlite3.connect(DB) as conn:
                        conn.execute('update reviews set review_state = ?, review_category = ? where id = ?',
                                     (*original, row_id))
                        conn.execute('delete from review_memory where item_id = '
                                     '(select item_id from reviews where id = ?)', (row_id,))
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
