"""Scan history page: criteria, not a second review queue."""
from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote

from fastapi.responses import HTMLResponse

import scan_history
from ui_shell import count_label


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


def _review_counts(job_id: str, counts: dict, state_labels: dict) -> str:
    """One link per review state, so each count opens exactly those rows."""
    links = "".join(
        f'<a class="pill" href="/queue?job={quote(job_id)}&amp;state={state}&amp;country_signal=all">'
        f'{escape(count_label(label, *counts.get(state, (0, 0))))}</a>'
        for state, label in state_labels.items()
    )
    return f'<div class="summary" aria-label="Review decisions">{links}</div>'


def _export_bar(choice_fields: str) -> str:
    """Multi-scan export: dates tick matching scans, checkboxes refine, one XLSX out."""
    return f'''<form id="multi-export" class="card inset multi-export" action="/export-scans.xlsx" method="get">
      <strong>Export several scans into one XLSX</strong>
      <p class="tiny">Pick a date range to tick every scan started in it, or tick scans by hand below. Rows get Scan ID and Scan Date columns.</p>
      <div class="export-controls">
        <label for="range-from">From</label><input type="date" id="range-from">
        <label for="range-to">To</label><input type="date" id="range-to">
        <button type="button" class="button secondary" id="range-clear">Clear ticks</button>
      </div>
      <div class="export-controls">{choice_fields}
        <button type="submit" class="button" id="multi-export-submit" disabled>Download XLSX (0 scans)</button>
        <span class="tiny" id="multi-export-status" aria-live="polite"></span>
      </div></form>'''


def render_history(db_path, html_page, state_labels: dict, export_choice_fields: str) -> HTMLResponse:
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
          <div class="history-heading"><label class="history-pick"><input type="checkbox" name="jobs" value="{job_id}" form="multi-export"
              data-date="{escape(scan["created_at"][:10], quote=True)}" aria-label="Include scan {job_id} in multi-scan export">
            <strong>{escape(_date(scan["created_at"]))}</strong>
            <span class="tiny"> · {escape(status)}</span></label>
            <a class="button secondary" href="/queue?job={quote(scan["job_id"])}&amp;country_signal=all">View results</a></div>
          <p class="tiny">{scan["row_count"]:,} saved rows · {scan["matches"]:,} confirmed matches · Scan {job_id}</p>
          {_review_counts(scan["job_id"], scan["review_counts"], state_labels)}
          <dl class="history-fields">
            <div><dt>Keywords</dt><dd>{escape(", ".join(keywords) if keywords else "None")}</dd></div>
            <div><dt>Negatives</dt><dd>{escape(negatives_text)}</dd></div>
            {fields}
          </dl></article>''')
    content = "".join(cards) or '<p>No scans recorded yet. Start a scan to see its criteria here.</p>'
    body = f'''<div class="card">{_export_bar(export_choice_fields) if scans else ""}
      <p class="tiny">Newest first · {len(scans)} scan(s). Earlier scans show only settings preserved with their results; "Not recorded" does not mean a toggle was off.</p>
      <div class="history-list">{content}</div></div>'''
    return html_page("Scan history", body, active="history", scripts=("history_export.js",),
                     subtitle="Search criteria used for past scans, including keywords, negatives and matching options.")
