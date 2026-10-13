"""Read-only Opera regression for ProductWatch-style queue rule/JSON editors."""
import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


URL = os.environ.get('PRESCREENER_FILTER_TEST_URL',
                     'http://127.0.0.1:8001/queue?state=pending&country_signal=all&job=latest')
OPERA = Path('/Applications/Opera.app/Contents/MacOS/Opera')
ROWS = [
    {'id': 10, 'item_id': '42', 'job_id': 'a', 'title': 'Generic soap',
     'listing_brand': 'Generic', 'country_signal': 'China', 'price_value': 12,
     'review_state': 'pending', 'scan_status': 'match', 'triage_lanes': ['generic']},
    {'id': 11, 'item_id': '42', 'job_id': 'b', 'title': 'Generic soap copy',
     'listing_brand': 'Generic', 'country_signal': 'US', 'price_value': 20,
     'review_state': 'pending', 'scan_status': 'match', 'triage_lanes': ['generic']},
    {'id': 12, 'item_id': '43', 'job_id': 'a', 'title': 'Febreze spray',
     'listing_brand': 'Febreze', 'country_signal': 'China', 'price_value': 8,
     'review_state': 'pending', 'scan_status': 'match', 'triage_lanes': []},
]


@unittest.skipUnless(OPERA.exists(), 'Opera browser required')
class ReviewFilterTests(unittest.TestCase):
    def setUp(self):
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(executable_path=str(OPERA), headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.errors = []
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.route('**/api/queue?*', lambda route: route.fulfill(json=ROWS))
        self.page.route('**/api/review/bulk', lambda route: self.fail('Filtering must never save decisions'))
        self.page.goto(URL)
        self.page.get_by_text('3 of 3 rows').first.wait_for()

    def open_json(self):
        self.page.locator('#fr-filter-open').click()
        self.page.locator('#fr-filter-json').click()

    def apply_json(self, data):
        import json
        self.open_json()
        self.page.locator('#fr-filter-source').fill(json.dumps(data))
        self.page.locator('#fr-filter-apply').click()

    def test_quick_clear_beside_pagination_preserves_other_filters(self):
        p = self.page
        quick = p.locator('#fr-filter-clear-quick')
        self.assertTrue(quick.is_hidden())
        p.locator('#fr-lanes').select_option('generic')
        p.locator('#fr-group').check()
        self.apply_json({'operator': 'and', 'filters': [
            {'columnId': 'country_signal', 'operator': 'is', 'value': 'US'}]})
        self.assertTrue(quick.is_visible())
        self.assertEqual(p.locator('#fr-filter-count').inner_text(), '(1)')
        self.assertEqual(quick.evaluate('(el) => el.previousElementSibling.id'), 'fr-pagination')
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 1)
        p.locator('input[data-select-id]').check()
        quick.click()
        self.assertTrue(quick.is_hidden())
        self.assertEqual(p.locator('#fr-filter-count').inner_text(), '')
        self.assertEqual(p.locator('#fr-lanes').input_value(), 'generic')
        self.assertTrue(p.locator('#fr-group').is_checked())
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 1)
        self.assertIn('2 rows', p.locator('#fr-lanes option[value="generic"]').inner_text())
        self.assertTrue(p.locator('#fr-selected').is_hidden())
        self.open_json()
        self.assertIn('"filters": []', p.locator('#fr-filter-source').input_value())
        self.assertFalse(self.errors)

    def test_quick_clear_sits_beside_rows_to_add_on_long_queue(self):
        p = self.page
        rows = [dict(ROWS[0], id=i, item_id=str(i), title='Generic soap ' + str(i))
                for i in range(110)]
        p.route('**/api/queue?*', lambda route: route.fulfill(json=rows))
        p.reload()
        p.locator('#fr-pagination .show-more-count').wait_for()
        self.apply_json({'operator': 'and', 'filters': [
            {'columnId': 'listing_brand', 'operator': 'is', 'value': 'Generic'}]})
        self.assertTrue(p.locator('#fr-pagination .show-more-count').is_visible())
        self.assertEqual(p.locator('#fr-filter-clear-quick').evaluate(
            '(el) => el.previousElementSibling.id'), 'fr-pagination')
        self.assertTrue(p.locator('#fr-filter-clear-quick').is_visible())
        p.locator('#fr-filter-clear-quick').click()
        self.assertTrue(p.locator('#fr-filter-clear-quick').is_hidden())
        self.assertTrue(p.locator('#fr-pagination .show-more-count').is_visible())
        self.assertFalse(self.errors)

    def test_quick_clear_when_filter_returns_no_rows(self):
        p = self.page
        self.apply_json({'operator': 'and', 'filters': [
            {'columnId': 'title', 'operator': 'is', 'value': 'impossible'}]})
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 0)
        self.assertTrue(p.locator('#fr-filter-clear-quick').is_visible())
        p.locator('#fr-filter-clear-quick').click()
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 3)
        self.assertTrue(p.locator('#fr-filter-clear-quick').is_hidden())
        self.assertFalse(self.errors)

    def test_nested_rules_json_round_trip_and_grouping(self):
        p = self.page
        p.locator('#fr-filter-open').click()
        p.locator('[data-action="add-group"]').first.click()
        p.locator('[data-path="0"] [data-action="add-rule"]').first.click()
        rule = p.locator('.fr-rule[data-path="0.0"]')
        rule.locator('[data-prop="columnId"]').select_option('listing_brand')
        rule.locator('[data-prop="operator"]').select_option('is')
        rule.locator('[data-prop="value"]').fill('Generic')
        p.locator('#fr-filter-json').click()
        self.assertIn('"columnId": "listing_brand"', p.locator('#fr-filter-source').input_value())
        p.locator('#fr-filter-source').fill('''{"operator":"and","filters":[
          {"operator":"or","filters":[
            {"columnId":"listing_brand","operator":"is","value":"Generic"},
            {"columnId":"title","operator":"includes","value":"spray"}]},
          {"columnId":"country_signal","operator":"is","value":"China"}]}''')
        p.locator('#fr-filter-rules').click()
        self.assertEqual(p.locator('.fr-rule').count(), 3)
        p.locator('#fr-filter-apply').click()
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 2)
        p.locator('#fr-group').check()
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 2)
        p.locator('#fr-lanes').select_option('generic')
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 1)
        p.locator('input[data-select-id]').click()
        self.assertIn('1 selected', p.locator('#fr-selected').inner_text())
        self.assertFalse(self.errors)

    def test_invalid_json_keeps_previous_filter_and_can_clear(self):
        p = self.page
        self.apply_json({'operator': 'and', 'filters': [
            {'columnId': 'price_value', 'operator': 'greaterThan', 'value': 10}]})
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 2)
        self.open_json()
        p.locator('#fr-filter-source').fill('{broken')
        p.locator('#fr-filter-apply').click()
        self.assertTrue(p.locator('#fr-filter-dialog').evaluate('(el) => el.open'))
        self.assertIn('JSON', p.locator('#fr-filter-error').inner_text())
        p.locator('#fr-filter-source').fill('{"operator":"and","filters":[{"columnId":"made_up","operator":"is","value":"x"}]}')
        p.locator('#fr-filter-apply').click()
        self.assertIn('unknown column', p.locator('#fr-filter-error').inner_text())
        p.locator('#fr-filter-close').click()
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 2)
        self.open_json()
        p.locator('#fr-filter-source').fill('{"operator":"and","filters":[{"columnId":"country_signal","operator":"greaterThan","value":1}]}')
        p.locator('#fr-filter-apply').click()
        self.assertIn('only available for Price', p.locator('#fr-filter-error').inner_text())
        p.locator('#fr-filter-close').click()
        p.locator('#fr-filter-open').click()
        p.locator('#fr-filter-clear').click()
        p.locator('#fr-filter-apply').click()
        self.assertEqual(p.locator('#queue-table tbody tr[data-row-id]').count(), 3)
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()
