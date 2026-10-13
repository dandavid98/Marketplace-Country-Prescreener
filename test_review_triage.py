"""Triage classification, safe previews, and confirm gate on an isolated SQLite DB."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import marketplace_prescreener as app
import review_triage


class ReviewTriageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "test.sqlite3"
        db_patch = patch.object(app, "DB_PATH", self.db)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        app.init_db()
        with sqlite3.connect(self.db) as conn:
            for rid, job, item, status, state, brand, seller, remembered in [
                (1, "scan-a", "123", "match", "pending", "Generic", "Store", ""),
                (2, "scan-b", "123", "match", "pending", "", "Unknown", ""),
                (3, "scan-a", "456", "walmart_sold", "not_queued", "Walmart", "Walmart.com", ""),
                (4, "scan-a", "789", "match", "pending", "Febreze", "Store", "approved_before"),
                (5, "scan-a", "", "match", "pending", "", "experiment gate for", ""),
            ]:
                conn.execute("""insert into reviews
                    (id,job_id,item_id,scan_status,review_state,listing_brand,seller_name,remembered,
                     keyword,search_url,listing_url,title,ship_from_country,target_country,country_signal,evidence,created_at)
                    values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid,job,item,status,state,brand,seller,remembered,
                     "soap","","","soap","","China","China","","2026-01-01"))

    def test_lanes_are_explainable_not_guessed(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["id"]: dict(r) for r in conn.execute("select * from reviews")}
        self.assertIn("generic", review_triage.lane_flags(rows[1]))
        self.assertNotIn("generic", review_triage.lane_flags(rows[2]))
        self.assertIn("missing_brand", review_triage.lane_flags(rows[2]))
        self.assertIn("verify_seller", review_triage.lane_flags(rows[2]))
        self.assertEqual(review_triage.lane_flags(rows[3]), ["walmart"])
        self.assertIn("approved_before", review_triage.lane_flags(rows[4]))
        self.assertIn("verify_seller", review_triage.lane_flags(rows[5]))
        self.assertNotIn("walmart", review_triage.lane_flags({"seller_name": "Walmart.com", "scan_status": "match"}))

    def test_preview_counts_repeats_and_enforces_scan_scope(self):
        info = review_triage.preview(self.db, [1, 5], "reject", "scan-a")
        self.assertEqual((info["rows"], info["items"], info["without_item_id"], info["pending_repeats"]),
                         (2, 1, 1, 1))
        self.assertEqual(review_triage.preview(self.db, [3], "reject", "scan-a")["walmart_rows"], 1)
        with self.assertRaisesRegex(ValueError, "no longer belong"):
            review_triage.preview(self.db, [1, 2], "reject", "scan-a")
        with self.assertRaisesRegex(ValueError, "between 1 and 5,000"):
            review_triage.preview(self.db, [], "reject", None)
        with self.assertRaisesRegex(ValueError, "between 1 and 5,000"):
            review_triage.preview(self.db, list(range(5001)), "reject", None)
        self.assertEqual(review_triage.preview(self.db, [1, 1], "reject", None)["rows"], 1)

    def test_no_write_without_confirmation_or_invalid_scope(self):
        self.assertEqual(app.api_review_preview("reject", [1], "scan-a").status_code, 200)
        self.assertEqual(app.api_review_bulk("reject", [1], "scan-a", False).status_code, 400)
        self.assertEqual(app.api_review_bulk("reject", [2], "scan-a", True).status_code, 400)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("select review_state from reviews where id=1").fetchone()[0], "pending")
        result = app.api_review_bulk("reject", [1], "scan-a", True)
        self.assertEqual(result.status_code, 200)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("select review_state from reviews where id=1").fetchone()[0], "rejected")
            self.assertEqual(conn.execute("select review_state from reviews where id=2").fetchone()[0], "rejected")


if __name__ == "__main__":
    unittest.main()
