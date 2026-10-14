"""History criteria and legacy reconstruction without touching production data."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import marketplace_prescreener as app
import scan_history


class ScanHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        db = Path(self.folder.name) / "history.sqlite3"
        self.db_patch = patch.object(app, "DB_PATH", db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        app.init_db()

    def test_new_scan_persists_all_criteria_without_any_results(self):
        job_id = app.create_job(
            ["Febreze", "Tide"], "China", 4, 3, "fast", max_price=15,
            price_only=True, match_description=True, counterfeit_min=7,
            market="CA", negative_terms=["shirt", "poster"],
            generic_include=True, fuzzy_strictness=85, disguised_search=True,
        )
        self.addCleanup(app.JOBS.pop, job_id, None)
        scans = scan_history.list_scans(app.DB_PATH)
        entry = next(scan for scan in scans if scan["job_id"] == job_id)
        self.assertTrue(entry["complete"])
        self.assertEqual(entry["row_count"], 0)
        self.assertEqual(entry["criteria"]["keywords"], ["Febreze", "Tide"])
        self.assertEqual(entry["criteria"]["negative_terms"], ["shirt", "poster"])
        for key, expected in {"generic_mode": "include", "country": "China", "market": "CA",
                              "fuzzy_strictness": 85, "disguised_search": True,
                              "max_price": 15, "price_only": True, "match_description": True,
                              "counterfeit_min": 7, "limit": 4, "workers": 3, "mode": "fast"}.items():
            self.assertEqual(entry["criteria"][key], expected)
        page = app.history_page().body.decode()
        self.assertIn("Scan history", page)
        self.assertIn("Febreze, Tide", page)
        self.assertIn("shirt, poster", page)
        self.assertIn("Standard + generic brands", page)
        self.assertIn(f"/queue?job={job_id}&amp;country_signal=all", page)
        self.assertIn('href="/history"', page)
        self.assertIn('aria-current="page"', page)
        app.JOBS[job_id]["status"] = "done"
        app.JOBS[job_id]["finished_at"] = "2026-10-13T12:00:00+00:00"
        scan_history.update_status(app.DB_PATH, app.JOBS[job_id])
        self.assertEqual(scan_history.list_scans(app.DB_PATH)[0]["status"], "done")
        self.assertEqual(scan_history.latest_scan_id(app.DB_PATH), job_id)
        archived = app.job_page(job_id).body.decode()
        self.assertIn("Live log", archived)  # current-process jobs still show the live view
        app.JOBS.pop(job_id)
        self.assertIn("View saved results", app.job_page(job_id).body.decode())
        self.assertIn(f'href="/job/{job_id}"', app.home().body.decode())
        self.assertEqual(app.job_page("missing").status_code, 404)

    def test_legacy_rows_show_known_values_without_inventing_toggles(self):
        with sqlite3.connect(app.DB_PATH) as conn:
            conn.execute("""insert into reviews (job_id, keyword, negatives, market,
                          target_country, scan_status, review_state, created_at,
                          search_url, listing_url, title, seller_name, ship_from_country,
                          country_signal, evidence) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         ("legacy", "Febreze", "shirt, poster", "US", "China", "match",
                          "pending", "2026-01-01T00:00:00+00:00", "", "", "", "", "", "China", ""))
        entry = scan_history.list_scans(app.DB_PATH)[0]
        self.assertEqual(scan_history.latest_scan_id(app.DB_PATH), "legacy")
        self.assertIn("View saved results", app.job_page("legacy").body.decode())
        self.assertFalse(entry["complete"])
        self.assertEqual(entry["criteria"]["keywords"], ["Febreze"])
        self.assertEqual(entry["criteria"]["negative_terms"], ["shirt, poster"])
        page = app.history_page().body.decode()
        self.assertIn("shirt, poster", page)
        self.assertIn("Not recorded for this older scan", page)
        self.assertNotIn("Generic brands only", page)

    def test_history_counts_review_decisions_per_scan(self):
        with sqlite3.connect(app.DB_PATH) as conn:
            for state in ("approved", "approved", "rejected", "pending"):
                conn.execute("""insert into reviews (job_id, keyword, negatives, market, target_country, scan_status, review_state, created_at,
                              search_url, listing_url, title, seller_name, ship_from_country,
                              country_signal, evidence) values ('scan-x', 'Tide', '', 'US', 'China', 'match', ?,
                              '2026-01-01T00:00:00+00:00', '', '', '', '', '', 'China', '')""", (state,))
        entry = scan_history.list_scans(app.DB_PATH)[0]
        self.assertEqual(entry["review_counts"], {"approved": 2, "rejected": 1, "pending": 1})
        page = app.history_page().body.decode()
        for text in ("Approved: 2", "Disputed: 1", "Pending: 1", "False Positives: 0",
                     "/queue?job=scan-x&amp;state=rejected&amp;country_signal=all"):
            with self.subTest(text=text):
                self.assertIn(text, page)

    def test_html_escapes_persisted_keywords(self):
        job_id = app.create_job(["<script>"], "China", 1, 1, "balanced")
        self.addCleanup(app.JOBS.pop, job_id)
        page = app.history_page().body.decode()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<dd><script>", page)


if __name__ == "__main__":
    unittest.main()
