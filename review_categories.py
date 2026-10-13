"""Review-row categories from ProductWatch quick defaults plus user-supplied labels.

Labels belong to saved review rows, not product taxonomy or item-ID decision memory.
"""
import sqlite3
from pathlib import Path

# ProductWatch frontend/src/stores/quickCategoryStore.ts defaults, supplemented
# with labels supplied by Daniel. Keep his exact spelling, including "Hygi".
CATEGORIES = (
    "Trademark Infringement Media",
    "Perscription Medical Devices",
    "Adult Content",
    "Sex Toys",
    "IP Rights Infringement Apparel",
    "IP Rights Infringement Cosmetics",
    "IP Rights Infringement Food and Beverage",
    "IP Rights Infringement Household and Cleaning",
    "IP Rights Infringement Personal Care and Hygi",
    "IP Rights Infringement Utility Outdoor & Maintenence",
    "IP Rights Infringement Media",
    "Trademark Infringement Apparel",
    "IP Rights Infringement Nutraceuticals",
    "IP Rights Infringement Toys",
    "Counterfeit gemstones and precious metals",
)


def init_categories(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("pragma table_info(reviews)")}
    if "review_category" not in columns:
        conn.execute("alter table reviews add column review_category text not null default ''")


def assign(db_path: Path, ids: list[int], category: str, job_id: str | None = None) -> int:
    """All-or-nothing assignment to non-disputed saved rows in the requested scan scope."""
    if category and category not in CATEGORIES:
        raise ValueError("Unknown review category")
    if not ids or len(ids) > 5000 or any(type(row_id) is not int or row_id <= 0 for row_id in ids):
        raise ValueError("Choose 1 to 5,000 saved rows")
    unique = list(dict.fromkeys(ids))
    with sqlite3.connect(db_path) as conn:
        # BEGIN IMMEDIATE prevents state changes between validation and the update.
        conn.execute("begin immediate")
        for offset in range(0, len(unique), 500):
            part = unique[offset:offset + 500]
            q = ",".join("?" for _ in part)
            where = f"id in ({q}) and review_state in ('pending', 'approved', 'not_queued')"
            params = list(part)
            if job_id:
                where += " and job_id = ?"
                params.append(job_id)
            if conn.execute(f"select count(*) from reviews where {where}", params).fetchone()[0] != len(part):
                raise ValueError("Every selected row must exist, not be disputed, and belong to this scan")
        for offset in range(0, len(unique), 500):
            part = unique[offset:offset + 500]
            conn.execute(f"update reviews set review_category = ? where id in ({','.join('?' for _ in part)})",
                         (category, *part))
    return len(unique)
