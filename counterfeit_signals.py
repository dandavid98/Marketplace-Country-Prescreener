"""Detect counterfeit-indicating language in seller reviews.

Walmart embeds full customer reviews directly in seller storefront page HTML
as JSON (`"reviewText":"..."`). This module extracts those review bodies and
counts how many distinct reviews mention counterfeit/fake-product language,
so a seller with a pattern of counterfeit complaints can be flagged as a
match regardless of country signal.
"""
from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# How many EXTRA review pages (beyond the first, already-fetched page) to pull
# when counting counterfeit mentions. Walmart shows ~12 reviews per page, and
# sellers can have hundreds, so this is a bounded best-effort sample, not full
# coverage. Kept small so a scan with the counterfeit check on doesn't grind
# to a halt on high-review sellers.
MAX_EXTRA_REVIEW_PAGES = 4

# Word/phrase indicators a reviewer uses when they believe a product is fake.
# Kept as whole-word/phrase patterns to avoid partial-word false hits
# (e.g. "fake" should not match inside an unrelated longer word).
COUNTERFEIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bcounterfeits?\b", re.I),
    re.compile(r"\bcounterfeited\b", re.I),
    re.compile(r"\bfakes?\b", re.I),
    re.compile(r"\bfaked\b", re.I),
    re.compile(r"\bknock[\s\-]?offs?\b", re.I),
    re.compile(r"\bbootlegs?\b", re.I),
    re.compile(r"\breplicas?\b", re.I),
    re.compile(r"\bimitations?\b", re.I),
    re.compile(r"\bdupes?\b", re.I),
    re.compile(r"\bnot\s+authentic\b", re.I),
    re.compile(r"\binauthentic\b", re.I),
    re.compile(r"\bnot\s+genuine\b", re.I),
    re.compile(r"\bnot\s+real\b", re.I),
    re.compile(r"\bnot\s+the\s+real\s+thing\b", re.I),
    re.compile(r"\bnot\s+original\b", re.I),
    re.compile(r"\bfraudulent\b", re.I),
    re.compile(r"\bscam(?:med)?\b", re.I),
]

_REVIEW_TEXT_RE = re.compile(r'"reviewText"\s*:\s*"((?:[^"\\]|\\.)*)"')
_REVIEW_PAGE_RE = re.compile(r'data-automation-id="page-number"[^>]*href="[^"]*reviewsCursor=(\d+)"')


def extract_review_texts(html: str) -> list[str]:
    """Pull decoded review body strings out of embedded reviewText JSON fields."""
    texts: list[str] = []
    for raw in _REVIEW_TEXT_RE.findall(html or ""):
        try:
            decoded = raw.encode().decode("unicode_escape", errors="ignore")
        except Exception:  # pragma: no cover - decoding is best-effort
            decoded = raw
        decoded = decoded.strip()
        if decoded:
            texts.append(decoded)
    return texts


def review_mentions_counterfeit(review_text: str) -> bool:
    return any(pattern.search(review_text) for pattern in COUNTERFEIT_PATTERNS)


def count_counterfeit_reviews(review_texts: list[str]) -> int:
    """Count DISTINCT reviews mentioning counterfeit language (not raw keyword hits)."""
    return sum(1 for text in review_texts if review_mentions_counterfeit(text))


def count_counterfeit_reviews_in_html(html: str) -> int:
    return count_counterfeit_reviews(extract_review_texts(html))


def discover_review_page_cursors(html: str) -> list[str]:
    """Find additional review page cursor values Walmart advertises in its own
    pagination widget on the seller page (e.g. reviewsCursor=2,3,4,6...)."""
    seen: list[str] = []
    for cursor in _REVIEW_PAGE_RE.findall(html or ""):
        if cursor not in seen:
            seen.append(cursor)
    return seen[:MAX_EXTRA_REVIEW_PAGES]


def _url_with_cursor(url: str, cursor: str) -> str:
    parts = urlsplit(url)
    # The /global/seller/<id> URL variant ignores reviewsCursor entirely and
    # always returns page 1 (verified live). The /seller/<id> variant (no
    # "global" segment) actually respects it and returns different reviews.
    path = parts.path.replace("/global/seller/", "/seller/", 1)
    query = dict(parse_qsl(parts.query))
    query["reviewsCursor"] = cursor
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


def count_counterfeit_reviews_across_pages(
    seller_url: str,
    first_page_html: str,
    fetch_page: Callable[[str], str],
) -> int:
    """Count distinct counterfeit-mentioning reviews across the first review
    page plus a bounded number of additional pages the seller page itself
    links to. `fetch_page` is injected so this module stays decoupled from
    the app's HTTP/bot-detection plumbing.

    Best-effort: Walmart's review pagination isn't always deterministic for a
    plain HTTP fetch, so this samples extra pages rather than guaranteeing
    full review coverage.
    """
    all_texts = list(extract_review_texts(first_page_html))
    if not seller_url:
        return count_counterfeit_reviews(all_texts)

    seen_texts = set(all_texts)
    for cursor in discover_review_page_cursors(first_page_html):
        try:
            page_html = fetch_page(_url_with_cursor(seller_url, cursor))
        except Exception:  # pragma: no cover - network is best-effort here
            continue
        for text in extract_review_texts(page_html):
            if text not in seen_texts:
                seen_texts.add(text)
                all_texts.append(text)

    return count_counterfeit_reviews(all_texts)
