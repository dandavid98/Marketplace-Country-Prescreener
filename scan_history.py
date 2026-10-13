"""Persist scan criteria; reconstruct only verifiable fields for older scans."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


SETTING_KEYS = (
    "search_type", "market", "country", "generic_mode", "match_description",
    "fuzzy_strictness", "disguised_search", "max_price", "price_only",
    "counterfeit_min", "limit", "workers", "mode",
)


def init_history(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("""create table if not exists scan_history (
            job_id text primary key,
            created_at text not null,
            finished_at text,
            status text not null,
            criteria_json text not null
        )""")


def save_scan(db_path: Path, job: dict) -> None:
    """Record the submitted criteria before any fetching (including zero-row scans)."""
    criteria = {key: job.get(key) for key in SETTING_KEYS if key in job}
    criteria["keywords"] = list(job["keywords"])
    criteria["negative_terms"] = list(job.get("negative_terms") or [])
    criteria["generic_mode"] = ("only" if job.get("generic_only") else
                                "include" if job.get("generic_include") else "standard")
    criteria["disguised_search"] = bool(job.get("disguised_searches"))
    with sqlite3.connect(db_path) as conn:
        conn.execute("insert into scan_history (job_id, created_at, status, criteria_json) values (?, ?, ?, ?)",
                     (job["id"], job["created_at"], job["status"], json.dumps(criteria)))


def update_status(db_path: Path, job: dict) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("update scan_history set status = ?, finished_at = ? where job_id = ?",
                     (job["status"], job.get("finished_at"), job["id"]))


def latest_scan_id(db_path: Path) -> str | None:
    """Find the latest saved scan, including legacy scans without a history row."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("""select job_id from (
            select job_id, created_at from scan_history
            union all
            select job_id, max(created_at) as created_at from reviews group by job_id
        ) order by created_at desc limit 1""").fetchone()
    return row[0] if row else None


def scan_summary(db_path: Path, job_id: str) -> dict | None:
    """Small persisted summary for an archived scan's non-live page."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        saved = conn.execute("select * from scan_history where job_id = ?", (job_id,)).fetchone()
        counts = conn.execute("""select count(*) as rows, sum(scan_status = 'match') as matches,
                               min(created_at) as created_at from reviews where job_id = ?""", (job_id,)).fetchone()
    if not saved and not counts["rows"]:
        return None
    return {"created_at": saved["created_at"] if saved else counts["created_at"],
            "status": saved["status"] if saved and saved["finished_at"] else None,
            "rows": counts["rows"], "matches": counts["matches"] or 0}


def list_scans(db_path: Path) -> list[dict]:
    """Include legacy result-only jobs; don't infer missing toggles from their rows."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        saved = {row["job_id"]: dict(row) for row in conn.execute("select * from scan_history")}
        legacy = conn.execute("""select job_id, min(created_at) as created_at, count(*) as row_count,
                                     sum(case when scan_status = 'match' then 1 else 0 end) as matches,
                                     min(market) as market, max(market) as last_market,
                                     min(target_country) as country, max(target_country) as last_country
                              from reviews group by job_id""").fetchall()
        summaries = {row["job_id"]: dict(row) for row in legacy}
        jobs = set(saved) | set(summaries)
        # Preserve original term order from the first result of each keyword.
        keywords = {}
        for row in conn.execute("""select job_id, keyword from reviews
                                   group by job_id, keyword order by min(id)"""):
            keywords.setdefault(row["job_id"], []).append(row["keyword"])
        negatives = {}
        for row in conn.execute("""select distinct job_id, negatives from reviews
                                   where negatives <> ''"""):
            negatives.setdefault(row["job_id"], set()).add(row["negatives"])
    result = []
    for job_id in jobs:
        stored = saved.get(job_id)
        summary = summaries.get(job_id, {})
        criteria = json.loads(stored["criteria_json"]) if stored else {
            "keywords": keywords.get(job_id, []),
            "negative_terms": sorted(negatives.get(job_id, [])),
            "market": summary["market"] if summary.get("market") == summary.get("last_market") else None,
            "country": summary["country"] if summary.get("country") == summary.get("last_country") else None,
        }
        result.append({
            "job_id": job_id, "created_at": stored["created_at"] if stored else summary["created_at"],
            "status": stored["status"] if stored else None,
            "row_count": summary.get("row_count", 0), "matches": summary.get("matches", 0),
            "criteria": criteria, "complete": bool(stored),
        })
    return sorted(result, key=lambda entry: entry["created_at"], reverse=True)
