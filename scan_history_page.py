"""Scan history page: criteria, not a second review queue."""
from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote

from fastapi.responses import HTMLResponse

import scan_history


LABELS = {
    "search_type": "Search by", "market": "Market", "country": "Target country",
    "generic_mode": "Listing brand matching", "match_description": "Match description",
    "fuzzy_strictness": "Fuzzy matching", "disguised_search": "Search disguised spellings",
    "max_price": "Max price", "price_only": "Match by price only",
    "counterfeit_min": "Counterfeit review threshold", "limit": "Page cap",
    "workers": "Listing workers", "mode": "Scan speed",
}
GENERIC_LABELS = {"only": "Generic brands only", "include": "Standard + generic brands",
                  "standard": "Standard"}


def _display(key: str, value: object) -> str:
    if key == "generic_mode":
        return GENERIC_LABELS.get(str(value), str(value))
    if key == "fuzzy_strictness":
        return "Off" if value is None else f"On (strictness {value})"
    if key == "limit":
        return "No page cap" if value == 0 else str(value)
    if value is None:
        return "Not set"
    if isinstance(value, bool):
        return "On" if value else "Off"
    return str(value)


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %Y · %I:%M %p UTC")
    except ValueError:
        return value


def render_history(db_path, html_page) -> HTMLResponse:
    scans = scan_history.list_scans(db_path)
    cards = []
    for scan in scans:
        criteria = scan["criteria"]
        job_id = escape(scan["job_id"], quote=True)
        keywords = criteria.get("keywords") or []
        negatives = criteria.get("negative_terms") or []
        # Legacy rows may have multiple different negatives strings; don't merge
        # those into one invented scan setting.
        negatives_text = (", ".join(negatives) if scan["complete"] else
                          negatives[0] if len(negatives) == 1 else "Not recorded")
        fields = "".join(
            f'<div><dt>{escape(label)}</dt><dd>{escape(_display(key, criteria[key]))}</dd></div>'
            for key, label in LABELS.items() if key in criteria
        )
        if not scan["complete"]:
            fields += '<div><dt>Other toggles / settings</dt><dd>Not recorded for this older scan</dd></div>'
        status = scan["status"] or "Results saved; scan status not recorded"
        cards.append(f'''<article class="history-entry">
          <div class="history-heading"><div><strong>{escape(_date(scan["created_at"]))}</strong>
            <span class="tiny"> · {escape(status)}</span></div>
            <a class="button secondary" href="/queue?job={quote(scan["job_id"])}&amp;country_signal=all">View results</a></div>
          <p class="tiny">{scan["row_count"]:,} saved rows · {scan["matches"]:,} confirmed matches · Scan {job_id}</p>
          <dl class="history-fields">
            <div><dt>Keywords</dt><dd>{escape(", ".join(keywords) if keywords else "None")}</dd></div>
            <div><dt>Negatives</dt><dd>{escape(negatives_text)}</dd></div>
            {fields}
          </dl></article>''')
    content = "".join(cards) or '<p>No scans recorded yet. Start a scan to see its criteria here.</p>'
    body = f'''<div class="card"><p class="tiny">Newest first · {len(scans)} scan(s). Earlier scans show only settings preserved with their results; "Not recorded" does not mean a toggle was off.</p>
      <div class="history-list">{content}</div></div>'''
    return html_page("Scan history", body, active="history",
                     subtitle="Search criteria used for past scans, including keywords, negatives and matching options.")
