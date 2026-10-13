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
            conn.execute("create table scan_history (job_id text primary key, created_at text, finished_at text, status text, criteria_json text)")
            conn.execute("create table reviews (id integer primary key, job_id text, scan_status text, "
                         "review_state text, country_signal text, keyword text, title text, listing_url text, negatives text not null default '', created_at text not null default '')")
            conn.executemany("insert into reviews (id,job_id,scan_status,review_state,country_signal,keyword,title,listing_url,negatives) values (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (1, "scan-a", "match", "pending", "China", "pending", "item-1", "url-1", "tool, refill"),
                (2, "scan-a", "match", "approved", "China", "approved", "item-2", "url-2", "tool, refill"),
                (3, "scan-a", "match", "rejected", "China", "disputed", "item-3", "url-3", "tool, refill"),
                (4, "scan-a", "unknown", "not_queued", "China", "not queued", "item-4", "url-4", "tool, refill"),
                (5, "scan-b", "match", "approved", "China", "other scan", "item-5", "url-5", ""),
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
        self.assertEqual(rows[0][:2], ("Keyword", "Negatives"))
        return rows[1:]

    def test_scopes_and_scan_boundary(self):
        for scope, expected in {
            "all": {"pending", "approved", "disputed", "not queued"},
            "match": {"pending", "approved", "disputed"},
            "approved": {"approved"}, "pending": {"pending"},
            "rejected": {"disputed"}, "not_queued": {"not queued"},
        }.items():
            with self.subTest(scope=scope):
                self.assertEqual({row[0] for row in self.export_keywords("scan-a", scope)}, expected)
        self.assertEqual(len(self.export_keywords()), 5)

    def test_negatives_in_scoped_and_selected_exports(self):
        self.assertEqual(self.export_keywords("scan-a", "approved", {2})[0][1], "tool, refill")
        self.assertIsNone(self.export_keywords("scan-b", "approved")[0][1])

    def test_selected_rows_intersect_scope_and_job(self):
        self.assertEqual([row[0] for row in self.export_keywords("scan-a", "approved", {1, 2, 5})], ["approved"])
        self.assertEqual([row[0] for row in self.export_keywords("scan-a", "match", {3, 4, 5})], ["disputed"])
        self.assertEqual(self.export_keywords("scan-a", "all", set()), [])

    def test_saved_scan_negatives_survive_export(self):
        db = Path(self.folder.name) / "fresh.sqlite3"
        with patch.object(app, "DB_PATH", db):
            app.init_db()
            record = app.ListingRecord(
                job_id="new-scan", keyword="soap", search_url="", listing_url="https://example.com/1",
                seller_page_url="", title="soap", seller_name="seller", seller_id="seller-1",
                ship_from_country="Unknown", target_country="China", country_signal="Unknown",
                evidence_source="", evidence="", scan_status="match", negatives="tool, refill",
            )
            app.save_record(record)
            path = app.export_reviews_xlsx(job_id="new-scan", scope="match")
            self.addCleanup(path.unlink, missing_ok=True)
            workbook = load_workbook(path, read_only=True)
            try:
                self.assertEqual(list(workbook.active.values)[1][:2], ("soap", "tool, refill"))
            finally:
                workbook.close()

    def test_queue_page_has_export_choices(self):
        page = app.queue_page(state="approved", country_signal="all", job="scan-a").body.decode()
        self.assertIn('value="approved" selected', page)
        self.assertIn('value="match"', page)
        self.assertIn('id="export-selected"', page)
        self.assertIn('name="job" value="scan-a"', page)


if __name__ == "__main__":
    unittest.main()
