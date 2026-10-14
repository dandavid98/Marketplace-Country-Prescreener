"""Review decision memory: remember Approve / Dispute per Walmart item ID.

Rules (agreed with Daniel, 2026-10-12):
  * Same listing = same item ID, whoever sells it.
  * A disputed item found again by a scan is saved as disputed ("auto-disputed"),
    so it never reaches the pending queue. It stays visible in the Disputed tab.
  * An approved item found again goes to the "Approved Before" bucket
    (review_state 'repeat', note 'approved_before') instead of pending, and inherits
    the approved row's review category unless it already has one (2026-10-14).
  * Only the newest Approved Before copy of an item stays visible; older copies become
    'superseded' (hidden from tabs, still in whole-scan exports). Nothing is deleted.
  * Decisions also apply to repeats already sitting in the pending queue.
  * "Not set" (and Undo, which uses it) forgets the item and reverses the cascade;
    categories are kept on reversal so nothing you typed is ever wiped.

`reviews.remembered` holds the note: '' | 'auto_disputed' | 'approved_before'.
Rows with no usable item ID are never remembered or matched.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Iterable

AUTO_DISPUTED = "auto_disputed"
APPROVED_BEFORE = "approved_before"
REPEAT_STATE = "repeat"
SUPERSEDED_STATE = "superseded"
_REPEATS = f"('{REPEAT_STATE}', '{SUPERSEDED_STATE}')"
_DECIDED = ("approved", "rejected")
_NO_ID = ("", "Unknown")
# Keep a row's own category; otherwise copy the newest categorised approved copy of the item.
_INHERIT_CATEGORY = ("coalesce(nullif(review_category, ''), (select a.review_category from reviews a "
                     "where a.item_id = reviews.item_id and a.review_state = 'approved' "
                     "and a.review_category <> '' order by a.id desc limit 1), '')")


def init_memory(conn: sqlite3.Connection, parse_item_id: Callable[[str], str]) -> None:
    """Create the memory table/column/indexes and backfill missing item IDs. Idempotent."""
    conn.execute(
        """
        create table if not exists review_memory (
            item_id text primary key,
            decision text not null check (decision in ('approved', 'rejected')),
            decided_at text not null,
            review_id integer
        )
        """
    )
    cols = {row[1] for row in conn.execute("pragma table_info(reviews)")}
    if "remembered" not in cols:
        conn.execute("alter table reviews add column remembered text not null default ''")
    conn.execute("create index if not exists idx_reviews_item_id on reviews(item_id)")
    conn.execute("create index if not exists idx_reviews_state on reviews(review_state, country_signal)")
    # Some August rows were saved without an item ID, but their Walmart URL has it.
    missing = conn.execute("select id, listing_url from reviews where item_id in ('', 'Unknown')").fetchall()
    fixes = [(item, rid) for rid, url in missing if (item := parse_item_id(url or "")) not in _NO_ID]
    if fixes:
        conn.executemany("update reviews set item_id = ? where id = ?", fixes)


def move_repeats_to_bucket(conn: sqlite3.Connection) -> None:
    """Move repeats saved before the Approved Before bucket existed. Idempotent.
    Needs reviews.review_category, so call after review_categories.init_categories."""
    conn.execute(f"update reviews set review_state = '{REPEAT_STATE}', review_category = {_INHERIT_CATEGORY} "
                 f"where review_state = 'pending' and remembered = '{APPROVED_BEFORE}'")
    supersede_older_repeats(conn)


def supersede_older_repeats(conn: sqlite3.Connection, item_ids: list[str] | None = None) -> int:
    """Keep only the newest Approved Before row per item visible; hide (never delete) the rest."""
    sql = (f"update reviews set review_state = '{SUPERSEDED_STATE}' where review_state = '{REPEAT_STATE}' "
           f"and id < (select max(b.id) from reviews b where b.item_id = reviews.item_id "
           f"and b.review_state = '{REPEAT_STATE}')")
    if item_ids is None:
        return conn.execute(sql).rowcount
    return _in_chunks(conn, sql + " and item_id in ({q})", item_ids)


def inherited_category(conn: sqlite3.Connection, item_id: str) -> str:
    row = conn.execute("select review_category from reviews where item_id = ? and review_state = 'approved' "
                       "and review_category <> '' order by id desc limit 1", (item_id,)).fetchone()
    return row[0] if row else ""


def remembered_decision(conn: sqlite3.Connection, item_id: str) -> str | None:
    """'approved' / 'rejected' if this item was decided before, else None."""
    if item_id in _NO_ID:
        return None
    row = conn.execute("select decision from review_memory where item_id = ?", (item_id,)).fetchone()
    return row[0] if row else None


def apply_to_new_row(conn: sqlite3.Connection, item_id: str, review_state: str,
                     evidence: str) -> tuple[str, str, str, str]:
    """For a row a scan is about to queue: returns (review_state, remembered, evidence, category)."""
    if review_state != "pending":
        return review_state, "", evidence, ""
    decision = remembered_decision(conn, item_id)
    if decision == "rejected":
        return "rejected", AUTO_DISPUTED, "Auto-disputed: you disputed this item before. " + evidence, ""
    if decision == "approved":
        return REPEAT_STATE, APPROVED_BEFORE, evidence, inherited_category(conn, item_id)
    return review_state, "", evidence, ""


def record_decisions(conn: sqlite3.Connection, record_ids: Iterable[int], review_state: str, now: str) -> int:
    """Remember (or forget) the items behind `record_ids` and cascade to repeats.

    Call AFTER the rows themselves were updated. Returns how many OTHER rows the
    cascade changed (so the UI knows to reload).
    """
    ids = list(record_ids)
    items: dict[str, int] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for rid, item in conn.execute(
            f"select id, item_id from reviews where id in ({','.join('?' * len(chunk))})", chunk
        ):
            if item not in _NO_ID:
                items.setdefault(item, rid)
    if not items:
        return 0
    # the rows you decided by hand are no longer "remembered" decisions
    _in_chunks(conn, "update reviews set remembered = '' where id in ({q})", ids)
    if review_state in _DECIDED:
        conn.executemany(
            "insert into review_memory (item_id, decision, decided_at, review_id) values (?, ?, ?, ?) "
            "on conflict(item_id) do update set decision = excluded.decision, "
            "decided_at = excluded.decided_at, review_id = excluded.review_id",
            [(item, review_state, now, rid) for item, rid in items.items()],
        )
    else:
        _in_chunks(conn, "delete from review_memory where item_id in ({q})", list(items))
    return _cascade(conn, list(items), review_state)


def _cascade(conn: sqlite3.Connection, item_ids: list[str], review_state: str) -> int:
    changed = 0
    # always undo the previous cascade first, so switching a decision never leaves stale marks
    changed += _in_chunks(conn, f"update reviews set review_state = 'pending', remembered = '' "
                                f"where remembered = '{AUTO_DISPUTED}' and item_id in ({{q}})", item_ids)
    changed += _in_chunks(conn, f"update reviews set review_state = 'pending' "
                                f"where review_state in {_REPEATS} and item_id in ({{q}})", item_ids)
    changed += _in_chunks(conn, f"update reviews set remembered = '' "
                                f"where remembered = '{APPROVED_BEFORE}' and item_id in ({{q}})", item_ids)
    if review_state == "rejected":
        changed += _in_chunks(conn, f"update reviews set review_state = 'rejected', remembered = '{AUTO_DISPUTED}', review_category = '' "
                                    f"where review_state = 'pending' and item_id in ({{q}})", item_ids)
    elif review_state == "approved":
        changed += _in_chunks(conn, f"update reviews set review_state = '{REPEAT_STATE}', remembered = '{APPROVED_BEFORE}', "
                                    f"review_category = {_INHERIT_CATEGORY} "
                                    f"where review_state = 'pending' and item_id in ({{q}})", item_ids)
        supersede_older_repeats(conn, item_ids)
    return changed


def _in_chunks(conn: sqlite3.Connection, sql: str, values: list) -> int:
    """Run `sql` (with a {q} placeholder list) over `values` in SQLite-safe chunks."""
    total = 0
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        total += conn.execute(sql.format(q=",".join("?" * len(chunk))), chunk).rowcount
    return total
