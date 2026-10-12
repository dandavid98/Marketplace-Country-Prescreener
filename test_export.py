"""Export categories and row selection without touching the real scan database."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

import marketplace_prescreener as app


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        db = Path(self.folder.name) / "test.sqlite3"
        self.db_patch = patch.object(app, "DB_PATH", db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        with sqlite3.connect(db) as conn:
            conn.execute("create table reviews (id integer primary key, job_id text, scan_status text, "
                         "review_state text, country_signal text, keyword text, title text, listing_url text)")
            conn.executemany("insert into reviews values (?, ?, ?, ?, ?, ?, ?, ?)", [
                (1, "scan-a", "match", "pending", "China", "pending", "item-1", "url-1"),
                (2, "scan-a", "match", "approved", "China", "approved", "item-2", "url-2"),
                (3, "scan-a", "match", "rejected", "China", "disputed", "item-3", "url-3"),
                (4, "scan-a", "unknown", "not_queued", "China", "not queued", "item-4", "url-4"),
                (5, "scan-b", "match", "approved", "China", "other scan", "item-5", "url-5"),
            ])

    def export_keywords(self, job_id=None, scope="all", selected_ids=None):
        path = app.export_reviews_xlsx(job_id=job_id, scope=scope, selected_ids=selected_ids)
        self.addCleanup(path.unlink, missing_ok=True)
        workbook = load_workbook(path, read_only=True)
        try:
            rows = list(workbook.active.values)
        finally:
            workbook.close()
        self.assertIn("Review Status", rows[0])
        return [row[0] for row in rows[1:]]

    def test_scopes_and_scan_boundary(self):
        for scope, expected in {
            "all": {"pending", "approved", "disputed", "not queued"},
            "match": {"pending", "approved", "disputed"},
            "approved": {"approved"}, "pending": {"pending"},
            "rejected": {"disputed"}, "not_queued": {"not queued"},
        }.items():
            with self.subTest(scope=scope):
                self.assertEqual(set(self.export_keywords("scan-a", scope)), expected)
        self.assertEqual(len(self.export_keywords()), 5)

    def test_selected_rows_intersect_scope_and_job(self):
        self.assertEqual(self.export_keywords("scan-a", "approved", {1, 2, 5}), ["approved"])
        self.assertEqual(self.export_keywords("scan-a", "match", {3, 4, 5}), ["disputed"])
        self.assertEqual(self.export_keywords("scan-a", "all", set()), [])

    def test_queue_page_has_export_choices(self):
        page = app.queue_page(state="approved", country_signal="all", job="scan-a").body.decode()
        self.assertIn('value="approved" selected', page)
        self.assertIn('value="match"', page)
        self.assertIn('id="export-selected"', page)
        self.assertIn('name="job" value="scan-a"', page)


if __name__ == "__main__":
    unittest.main()
