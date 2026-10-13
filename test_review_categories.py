"""Approved-only row categories: migration, atomic writes, scope, and decisions."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import marketplace_prescreener as app
import review_categories


class ReviewCategoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / 'categories.sqlite3'
        patcher = patch.object(app, 'DB_PATH', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.init_db()
        with sqlite3.connect(self.db) as conn:
            for rid, job, item, state in [
                (1, 'scan-a', 'A', 'approved'), (2, 'scan-a', 'B', 'approved'),
                (3, 'scan-a', 'C', 'pending'), (4, 'scan-b', 'A', 'approved'),
                (5, 'scan-a', 'D', 'rejected'),
            ]:
                conn.execute('''insert into reviews
                  (id, job_id, item_id, scan_status, review_state, keyword, search_url,
                   listing_url, title, seller_name, ship_from_country, target_country,
                   country_signal, evidence, created_at)
                  values (?, ?, ?, 'match', ?, 'soap', '', '', 'soap', '', '', 'China', 'China', '', '')''',
                  (rid, job, item, state))

    def categories(self):
        with sqlite3.connect(self.db) as conn:
            return dict(conn.execute('select id, review_category from reviews'))

    def test_productwatch_defaults_and_idempotent_migration(self):
        self.assertEqual(len(review_categories.CATEGORIES), 11)
        self.assertIn('Trademark Infringement Media', review_categories.CATEGORIES)
        app.init_db()
        self.assertEqual(self.categories()[1], '')

    def test_selected_approved_rows_only_and_clear(self):
        category = 'Trademark Infringement Media'
        self.assertEqual(review_categories.assign(self.db, [1, 2, 1], category, 'scan-a'), 2)
        self.assertEqual(self.categories(), {1: category, 2: category, 3: '', 4: '', 5: ''})
        self.assertEqual(review_categories.assign(self.db, [1], 'Sex Toys', 'scan-a'), 1)
        self.assertEqual(self.categories()[1], 'Sex Toys')
        self.assertEqual(review_categories.assign(self.db, [1], '', 'scan-a'), 1)
        self.assertEqual(self.categories()[1], '')
        self.assertEqual(app.get_reviews('approved', job_id='scan-a')[0]['review_category'], category)

    def test_atomic_rejection_of_pending_disputed_wrong_scan_and_unknown_category(self):
        category = 'Trademark Infringement Media'
        for ids, name, scope in [([1, 3], category, 'scan-a'),
                                 ([1, 5], category, 'scan-a'),
                                 ([1, 4], category, 'scan-a'),
                                 ([1, 99], category, 'scan-a'),
                                 ([1], 'Made up', 'scan-a'),
                                 ([], category, 'scan-a'),
                                 ([1] * 5001, category, 'scan-a')]:
            with self.subTest(ids=ids[:3], name=name):
                with self.assertRaises(ValueError):
                    review_categories.assign(self.db, ids, name, scope)
                self.assertEqual(set(self.categories().values()), {''})
        with self.assertRaises(ValueError):
            review_categories.assign(self.db, [True], category, 'scan-a')

    def test_endpoint_scope_and_decision_clears_only_relevant_category(self):
        category = 'Trademark Infringement Media'
        self.assertEqual(app.api_review_category([1, 4], category, 'scan-a').status_code, 400)
        self.assertEqual(app.api_review_category([1, 2], category, 'scan-a').status_code, 200)
        self.assertEqual(app.api_review_category([4], 'Adult Content', 'scan-b').status_code, 200)
        app.update_review_states([1], 'rejected')
        self.assertEqual(self.categories()[1], '')
        self.assertEqual(self.categories()[2], category)
        self.assertEqual(self.categories()[4], 'Adult Content')
        self.assertEqual(app.api_review_category([1], category, 'scan-a').status_code, 400)
        app.update_review_states([2], 'pending')
        self.assertEqual(self.categories()[2], '')


if __name__ == '__main__':
    unittest.main()
