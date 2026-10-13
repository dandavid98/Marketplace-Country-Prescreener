"""Conservative, explainable review lanes and bulk-decision impact previews."""
import sqlite3
from pathlib import Path

from brand_signals import is_generic_brand


LANES = ("all", "walmart", "approved_before", "generic", "missing_brand", "verify_seller")


def lane_flags(row: dict) -> list[str]:
    """A row may belong to multiple lanes. Never infer Walmart status from a name."""
    flags = []
    if row.get("scan_status") == "walmart_sold":
        flags.append("walmart")
    if row.get("remembered") == "approved_before":
        flags.append("approved_before")
    brand = (row.get("listing_brand") or "").strip()
    if not brand:
        flags.append("missing_brand")
    elif is_generic_brand(brand):
        flags.append("generic")
    seller = (row.get("seller_name") or "").strip()
    seller_id = (row.get("seller_id") or "").strip()
    # Flag only clear extraction failures; this is a verification cue, not a verdict.
    if row.get("scan_status") != "walmart_sold" and (
            not seller or seller.lower() == "unknown" or seller_id.lower() in ("", "unknown")
            or seller.lower().startswith("experiment gate")
            or seller.lower().startswith("us store directory")
            or (len(seller) >= 75 and "storefront" not in seller.lower())):
        flags.append("verify_seller")
    return flags


def preview(db_path: Path, ids: list[int], action: str, scope: str | None) -> dict:
    """Count scoped direct rows and other pending repeats affected by item-ID memory.

    Preview is advisory: the confirmation endpoint must revalidate scope and action.
    """
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > 5000:
        raise ValueError("Select between 1 and 5,000 rows per decision")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        direct = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            condition = " and job_id = ?" if scope else ""
            direct.extend(dict(r) for r in conn.execute(
                f"select id, item_id, review_state, scan_status from reviews where id in ({','.join('?' * len(chunk))}){condition}",
                (*chunk, scope) if scope else chunk))
        if len(direct) != len(ids):
            raise ValueError("Some selected rows no longer belong to this scan. Refresh and retry")
        items = {r["item_id"] for r in direct if r["item_id"] not in ("", "Unknown")}
        affected = []
        for start in range(0, len(items), 500):
            chunk = list(items)[start:start + 500]
            affected.extend(dict(r) for r in conn.execute(
                f"select id, item_id, review_state, remembered from reviews "
                f"where item_id in ({','.join('?' * len(chunk))})", chunk))
        selected = set(ids)
        pending_repeats = sum(r["id"] not in selected and r["review_state"] == "pending"
                              for r in affected)
        remembered_repeats = sum(r["id"] not in selected and r["remembered"] in
                                 ("auto_disputed", "approved_before") for r in affected)
    return {"rows": len(direct), "items": len(items),
            "without_item_id": sum(r["item_id"] in ("", "Unknown") for r in direct),
            "pending_repeats": pending_repeats,
            "remembered_repeats": remembered_repeats,
            "walmart_rows": sum(r["scan_status"] == "walmart_sold" for r in direct),
            "action": action}
