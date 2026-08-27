from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse, parse_qs, urlencode, urlunparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from openpyxl import Workbook

from counterfeit_signals import count_counterfeit_reviews_in_html, count_counterfeit_reviews_across_pages

APP_TITLE = "Marketplace Country Prescreener"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_COUNTRY = "China"
COUNTRY_OPTIONS = ["China", "US", "Any"]
COUNTRY_OPTION_LABELS = {"China": "China", "US": "US", "Any": "Any (no country filter)"}
DEFAULT_LIMIT = 0


@dataclass(frozen=True)
class Market:
    """A Walmart-banner storefront the scanner can point at.

    `domain` is the storefront host, `locale_prefix` is the path segment
    Walmart puts before `/ip/` and `/search` on that storefront (empty for
    walmart.com, "en" for walmart.ca, etc.), and `currency_suffix` is a
    cosmetic label appended to parsed prices so a $19.99 CAD listing isn't
    mistaken for USD.
    """

    code: str
    label: str
    domain: str
    locale_prefix: str = ""
    currency_suffix: str = ""

    @property
    def root_domain(self) -> str:
        return self.domain[4:] if self.domain.startswith("www.") else self.domain

    @property
    def path_prefix(self) -> str:
        return f"/{self.locale_prefix}" if self.locale_prefix else ""

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"


# NOTE on MX/CL domains: verified live via curl during setup (2026-08-25) for
# US and CA only. This sandbox's DNS resolver blocks .mx/.cl TLD lookups
# entirely (also blocked amazon.com.mx, a domain that definitely exists), so
# walmart.com.mx / lider.cl could not be confirmed from here. Both are the
# correct storefronts per public knowledge, but test them for real once this
# is running on a normal network before trusting MX/CL results.
MARKETS: dict[str, Market] = {
    "US": Market("US", "United States \u2014 walmart.com", "www.walmart.com"),
    "CA": Market("CA", "Canada \u2014 walmart.ca", "www.walmart.ca", locale_prefix="en", currency_suffix=" CAD"),
    "MX": Market("MX", "Mexico \u2014 walmart.com.mx (verify DNS resolves before use)", "www.walmart.com.mx", currency_suffix=" MXN"),
    "CL": Market("CL", "Chile \u2014 lider.cl (verify DNS resolves before use)", "www.lider.cl", currency_suffix=" CLP"),
}
DEFAULT_MARKET = "US"


def get_market(code: str | None) -> Market:
    return MARKETS.get((code or "").upper(), MARKETS[DEFAULT_MARKET])


SEARCH_TIMEOUT = 15
MAX_PAGE_SAFETY = 250
LISTING_WORKERS = 5
KEYWORD_WORKERS = 3
MAX_LISTING_WORKERS = 12
DEFAULT_STOP_AFTER_FIRST_MATCH = False
DEFAULT_MODE = "balanced"
DEFAULT_SEARCH_TYPE = "keyword"
DB_PATH = Path(__file__).with_name("prescreener.sqlite3")


class BotChallengeDetected(RuntimeError):
    pass


BOT_CHALLENGE_PATTERNS = [
    re.compile(r"not a robot", re.I),
    re.compile(r"are you human", re.I),
    re.compile(r"verify you are human", re.I),
    re.compile(r"robot check", re.I),
    re.compile(r"unusual traffic", re.I),
    re.compile(r"access denied", re.I),
    re.compile(r"pardon our interruption", re.I),
    re.compile(r"security check", re.I),
]


app = FastAPI(title=APP_TITLE)
lock = threading.RLock()
JOBS: dict[str, dict] = {}


@dataclass
class ListingRecord:
    job_id: str
    keyword: str
    search_url: str
    listing_url: str
    seller_page_url: str
    title: str
    seller_name: str
    seller_id: str
    ship_from_country: str
    target_country: str
    country_signal: str
    evidence_source: str
    evidence: str
    scan_status: str
    review_state: str = "pending"
    created_at: str = ""
    item_id: str = ""
    image_url: str = ""
    price_text: str = ""
    price_value: float | None = None
    description: str = ""
    counterfeit_review_count: int = 0
    market: str = DEFAULT_MARKET
    listing_brand: str = ""
    id: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def walmart_search_url(keyword: str, market: Market | None = None) -> str:
    market = market or MARKETS[DEFAULT_MARKET]
    return f"{market.base_url}{market.path_prefix}/search?q={quote_plus(keyword)}"


def is_bot_challenge(html: str) -> bool:
    text = normalize_space(strip_tags(html)).lower()
    title = page_title(html, fallback="").lower()
    combined = f"{title} {text}"
    return any(pattern.search(combined) for pattern in BOT_CHALLENGE_PATTERNS)


def fetch_html(url: str, timeout: int = SEARCH_TIMEOUT) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="replace")
    if is_bot_challenge(html):
        raise BotChallengeDetected(f"Bot challenge detected at {url}")
    return html


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def page_title(html: str, fallback: str = "") -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return normalize_space(strip_tags(m.group(1))) if m else fallback


def extract_listing_urls(search_html: str, market: Market | None = None) -> list[str]:
    market = market or MARKETS[DEFAULT_MARKET]
    prefix = re.escape(market.path_prefix)
    domain = re.escape(market.domain)
    relative_pattern = rf"{prefix}/ip/[^\"'<>\s]+" if prefix else r"/ip/[^\"'<>\s]+"
    patterns = [rf"https://{domain}{prefix}/ip/[^\"'<>\s]+", relative_pattern]
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, search_html):
            url = raw if raw.startswith("http") else urljoin(market.base_url, raw)
            url = url.split("?")[0]
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def extract_next_search_url(search_html: str, current_url: str) -> str:
    max_page_match = re.search(r'"paginationV2":\{"maxPage":(\d+)', search_html)
    max_page = int(max_page_match.group(1)) if max_page_match else None
    parsed = urlparse(current_url)
    params = parse_qs(parsed.query)
    current_page = 1
    if "page" in params and params["page"]:
        try:
            current_page = max(1, int(params["page"][0]))
        except ValueError:
            current_page = 1
    if max_page is not None and current_page >= max_page:
        return ""
    params["page"] = [str(current_page + 1)]
    next_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=next_query))


def extract_seller_page_url(product_html: str, listing_url: str) -> str:
    """Find the seller/store link on a product page.

    Domain-checked against `listing_url`'s own host (not a hardcoded
    "walmart.com") so this works for any configured Market -- walmart.ca,
    walmart.com.mx, lider.cl, etc. -- without needing the Market object
    threaded in here too.
    """
    patterns = [
        r'href=["\']([^"\']*(?:/seller/|seller\?|seller/|/store/|store/)[^"\']*)["\']',
        r'href=["\']([^"\']*seller[^"\']*)["\']',
    ]
    listing_domain = urlparse(listing_url).netloc
    domain_root = listing_domain[4:] if listing_domain.startswith("www.") else listing_domain
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, product_html, flags=re.I):
            url = raw if raw.startswith("http") else urljoin(listing_url, raw)
            url = url.split("#")[0].split("?")[0]
            if url not in seen and domain_root and domain_root in url and "/ip/" not in url and "/search" not in url:
                seen.add(url)
                candidates.append(url)
    return candidates[0] if candidates else ""


def extract_seller_id(seller_page_url: str) -> str:
    if not seller_page_url:
        return "Unknown"
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(seller_page_url)
    params = parse_qs(parsed.query)
    for key in ("seller_id", "sellerId", "storeId", "merchantId", "id"):
        values = params.get(key)
        if values and values[0]:
            return normalize_space(values[0])
    segments = [seg for seg in parsed.path.split("/") if seg]
    for idx, seg in enumerate(segments):
        if seg.lower() in {"seller", "store", "merchant"} and idx + 1 < len(segments):
            candidate = segments[idx + 1]
            if re.fullmatch(r"\d{3,}", candidate):
                return candidate
    for seg in reversed(segments):
        if re.fullmatch(r"\d{3,}", seg):
            return seg
    m = re.search(r"(?:seller|store|merchant)[^\d]{0,20}(\d{3,})", seller_page_url, re.I)
    if m:
        return m.group(1)
    return "Unknown"


def extract_embedded_seller_id(product_html: str) -> str:
    """Fallback seller-id lookup for storefronts that don't render a clickable
    seller-page link (confirmed on walmart.ca product pages, which show a
    plain "Sold by X" text with no href, unlike walmart.com's linked seller
    storefronts). Walmart's product page still embeds the raw sellerId in a
    JSON payload regardless of storefront, so this pulls it from there.
    """
    m = re.search(r'"sellerId"\s*:\s*"([^"]+)"', product_html)
    return normalize_space(m.group(1)) if m else "Unknown"


def extract_item_id(listing_url: str) -> str:
    m = re.search(r"/ip/[^/]+/(\d+)", listing_url)
    if m:
        return m.group(1)
    tail = listing_url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else "Unknown"


def extract_image_url(product_html: str) -> str:
    m = re.search(r'property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', product_html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', product_html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'https://i\d*\.walmartimages\.[a-z.]+/[^"\'\\\s]+', product_html)
    if m:
        return m.group(0)
    return ""


def extract_description(product_html: str) -> str:
    m = re.search(r'property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']', product_html, re.I)
    if m:
        return normalize_space(strip_tags(m.group(1)))
    m = re.search(r'name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', product_html, re.I)
    if m:
        return normalize_space(strip_tags(m.group(1)))
    m = re.search(r'"shortDescription"\s*:\s*"((?:[^"\\]|\\.)*)"', product_html)
    if m:
        text = m.group(1).encode().decode("unicode_escape", errors="ignore")
        return normalize_space(strip_tags(text))
    return ""


def extract_price_text(product_html: str) -> str:
    m = re.search(r'"currentPrice"\s*:\s*\{[^}]*?"price"\s*:\s*([0-9]+(?:\.[0-9]+)?)', product_html)
    if m:
        return f"${float(m.group(1)):.2f}"
    m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([0-9]+(?:\.[0-9]+)?)["\']', product_html, re.I)
    if m:
        return f"${float(m.group(1)):.2f}"
    m = re.search(r"\$\s?([0-9]{1,4}\.[0-9]{2})", product_html)
    if m:
        return f"${m.group(1)}"
    return "Unknown"


def extract_listing_brand(product_html: str) -> str:
    """Pull the seller-supplied "Listing Brand" attribute off a product page.

    Confirmed live (2026-08-27) that Walmart embeds this as a plain
    `"brand":"..."` JSON field on both search-result tiles and the product
    page itself, on both walmart.com and walmart.ca -- same field, same
    shape, no market-specific handling needed.
    """
    m = re.search(r'"brand"\s*:\s*"([^"]*)"', product_html)
    return normalize_space(m.group(1)) if m and m.group(1).strip() else ""


def parse_price_value(price_text: str) -> float | None:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", price_text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def keyword_matches_text(keyword: str, text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", keyword.lower()) if len(w) > 1]
    if not words:
        return True
    return all(w in text_lower for w in words)


def keyword_matches_listing(keyword: str, title: str, description: str, match_description: bool) -> bool:
    """Match a keyword against the title, optionally also checking the description.

    With match_description off, only the title is checked (original behavior).
    With it on, a listing counts as a match if EITHER the title OR the description
    contains all the keyword's significant words.
    """
    if keyword_matches_text(keyword, title):
        return True
    if match_description and keyword_matches_text(keyword, description):
        return True
    return False


def brand_matches(target_brand: str, listing_brand: str) -> bool:
    """Exact, case-insensitive match against the listing's own Brand attribute.

    Deliberately NOT a text-contains check against title/description -- live
    testing showed listing brand codes (e.g. a private bulk-seller brand like
    "WLBXH") almost never appear in the product title itself, so trusting the
    embedded brand field is the only reliable signal for this search type.
    """
    if not target_brand.strip():
        return True
    return listing_brand.strip().lower() == target_brand.strip().lower()


def term_matches_listing(
    search_type: str,
    term: str,
    title: str,
    description: str,
    listing_brand: str,
    match_description: bool,
) -> bool:
    """Dispatch to the right match rule for the job's search type.

    Keeps `keyword_matches_listing` untouched (still used standalone by
    keyword-mode jobs); brand-mode jobs get their own exact-match rule via
    `brand_matches` instead of a text-contains check.
    """
    if search_type == "brand":
        return brand_matches(term, listing_brand)
    return keyword_matches_listing(term, title, description, match_description)


COUNTRY_HINTS: dict[str, list[re.Pattern[str]]] = {
    "China": [
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)\s*[:\-]?\s*[^<>{}\n]{0,180}\b(?:china|cn)\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,180}\bchina\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,180}\bcn\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,220}\b(?:guangzhou|shenzhen|hangzhou|dongguan|foshan|yiwu|ningbo|suzhou|wenzhou|xiamen|qingdao|tianjin|beijing|shanghai|zhuhai|guangdong|zhejiang|jiangsu|shandong)\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,220}\b(?:GD|ZJ|JS|SH|BJ|CQ|SC|HN|HB|SD|FJ|JX|AH|GX|LN|TJ|HE|HL|JL)\s+\d{6}\b", re.I),
    ],
    "US": [
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|based in)[^<>{}\n]{0,180}\b(?:united states|usa|us)\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|based in)[^<>{}\n]{0,220}\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b[^<>{}\n]{0,40}\bUS\b", re.I),
    ],
}


def first_match(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            if m.lastindex:
                return normalize_space(m.group(1))
            return normalize_space(m.group(0))
    return "Unknown"


def strip_review_text(html: str) -> str:
    """Remove embedded customer review text/title JSON fields before signal scanning.

    Walmart embeds full customer reviews directly in product/seller page HTML.
    Reviews are personal opinions/guesses ("I suspect it came from China"), not
    authoritative seller data, so they must not be able to trigger a country
    signal match the way real "Seller Location: ..." text would.
    """
    html = re.sub(r'"reviewText"\s*:\s*"(?:[^"\\]|\\.)*"', '"reviewText":""', html)
    html = re.sub(r'"reviewTitle"\s*:\s*"(?:[^"\\]|\\.)*"', '"reviewTitle":""', html)
    # Walmart also renders the review body as plain visible text (not just JSON),
    # consistently wrapped in this span structure. Strip that rendered copy too.
    html = re.sub(r'(<span class="tl-m db-m"><b></b>).*?(</span>)', r'\1\2', html, flags=re.S)
    return html


def extract_signals(html: str, target_country: str) -> tuple[str, str, str, str]:
    html = strip_review_text(html)
    text = normalize_space(strip_tags(html))
    china_evidence = first_match(text, COUNTRY_HINTS.get("China", []))
    us_evidence = first_match(text, COUNTRY_HINTS.get("US", []))

    if target_country == "China":
        if china_evidence != "Unknown":
            country_signal = "China"
        elif us_evidence != "Unknown":
            country_signal = "US"
        else:
            country_signal = "Unknown"
    elif target_country == "US":
        if us_evidence != "Unknown":
            country_signal = "US"
        elif china_evidence != "Unknown":
            country_signal = "China"
        else:
            country_signal = "Unknown"
    elif target_country == "Any":
        # No country filter requested -- still surface whichever signal (if
        # any) is present on the page for visibility, just don't gate on it.
        if china_evidence != "Unknown":
            country_signal = "China"
        elif us_evidence != "Unknown":
            country_signal = "US"
        else:
            country_signal = "Unknown"
    else:
        target_patterns = COUNTRY_HINTS.get(target_country, [re.compile(re.escape(target_country), re.I)])
        target_evidence = first_match(text, target_patterns)
        country_signal = target_country if target_evidence != "Unknown" else "Unknown"

    seller_name = first_match(
        text,
        [
            re.compile(r"seller name[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"sold by[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"visit the\s+([A-Za-z0-9&'\-., ]{2,80})\s+store", re.I),
            re.compile(r"seller:\s+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"by\s+([A-Za-z0-9&'\-., ]{2,80})", re.I),
        ],
    )

    ship_from = first_match(
        text,
        [
            re.compile(r"ships? from[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"shipping from[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"seller(?:\s+location)?[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"seller address[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"address[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"location[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"country of origin[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"based in[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
            re.compile(r"from[:\s]+([A-Za-z0-9&'\-., ]{2,80})", re.I),
        ],
    )

    if ship_from == "Unknown":
        if country_signal == "China" and china_evidence != "Unknown":
            ship_from = china_evidence
        elif country_signal == "US" and us_evidence != "Unknown":
            ship_from = us_evidence

    if country_signal == "China" and china_evidence != "Unknown":
        evidence = china_evidence
    elif country_signal == "US" and us_evidence != "Unknown":
        evidence = us_evidence
    elif ship_from != "Unknown":
        evidence = ship_from
    else:
        evidence = seller_name if seller_name != "Unknown" else "No explicit seller-country signal found on page"
    return country_signal, seller_name, ship_from, evidence


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists reviews (
                id integer primary key autoincrement,
                job_id text not null,
                keyword text not null,
                search_url text not null,
                listing_url text not null,
                seller_page_url text not null default '',
                title text not null,
                seller_name text not null,
                seller_id text not null default "Unknown",
                ship_from_country text not null,
                target_country text not null,
                country_signal text not null,
                evidence_source text not null default '',
                evidence text not null,
                scan_status text not null,
                review_state text not null,
                created_at text not null
            )
            """
        )
        cols = {row[1] for row in conn.execute("pragma table_info(reviews)").fetchall()}
        additions = {
            'seller_page_url': "alter table reviews add column seller_page_url text not null default ''",
            'seller_id': "alter table reviews add column seller_id text not null default 'Unknown'",
            'evidence_source': "alter table reviews add column evidence_source text not null default ''",
            'item_id': "alter table reviews add column item_id text not null default ''",
            'image_url': "alter table reviews add column image_url text not null default ''",
            'price_text': "alter table reviews add column price_text text not null default ''",
            'price_value': "alter table reviews add column price_value real",
            'description': "alter table reviews add column description text not null default ''",
            'counterfeit_review_count': "alter table reviews add column counterfeit_review_count integer not null default 0",
            'market': f"alter table reviews add column market text not null default '{DEFAULT_MARKET}'",
            'listing_brand': "alter table reviews add column listing_brand text not null default ''",
        }
        for col, sql in additions.items():
            if col not in cols:
                conn.execute(sql)
        conn.commit()


init_db()


def save_record(record: ListingRecord) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            insert into reviews (
                job_id, keyword, search_url, listing_url, seller_page_url, title, seller_name, seller_id,
                ship_from_country, target_country, country_signal, evidence_source, evidence,
                scan_status, review_state, created_at, item_id, image_url, price_text, price_value, description, counterfeit_review_count, market, listing_brand
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.job_id,
                record.keyword,
                record.search_url,
                record.listing_url,
                record.seller_page_url,
                record.title,
                record.seller_name,
                record.seller_id,
                record.ship_from_country,
                record.target_country,
                record.country_signal,
                record.evidence_source,
                record.evidence,
                record.scan_status,
                record.review_state,
                record.created_at,
                record.item_id,
                record.image_url,
                record.price_text,
                record.price_value,
                record.description,
                record.counterfeit_review_count,
                record.market,
                record.listing_brand,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_reviews(review_state: str | None = None, country_signal: str | None = None) -> list[dict]:
    sql = "select * from reviews"
    clauses: list[str] = []
    params: list[str] = []
    if review_state and review_state != "all":
        clauses.append("review_state = ?")
        params.append(review_state)
    if country_signal and country_signal != "all":
        clauses.append("country_signal = ?")
        params.append(country_signal)
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by id desc"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def update_review_state(record_id: int, review_state: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("update reviews set review_state = ? where id = ?", (review_state, record_id))
        conn.commit()


def export_reviews_xlsx() -> Path:
    rows = get_reviews("all")
    wb = Workbook()
    ws = wb.active
    ws.title = "Queue"
    headers = [
        "ID", "Job ID", "Keyword", "Listing Brand", "Market", "Search URL", "Listing URL", "Seller Page URL", "Title", "Seller Name", "Seller ID",
        "Ship From Country", "Target Country", "Country Signal", "Evidence Source", "Evidence", "Scan Status",
        "Review State", "Created At",
    ]
    ws.append(headers)
    for row in rows:
        ws.append([
            row.get("id", ""),
            row.get("job_id", ""),
            row.get("keyword", ""),
            row.get("listing_brand", ""),
            row.get("market", DEFAULT_MARKET),
            row.get("search_url", ""),
            row.get("listing_url", ""),
            row.get("seller_page_url", ""),
            row.get("title", ""),
            row.get("seller_name", ""),
            row.get("seller_id", "Unknown"),
            row.get("ship_from_country", ""),
            row.get("target_country", ""),
            row.get("country_signal", ""),
            row.get("evidence_source", ""),
            row.get("evidence", ""),
            row.get("scan_status", ""),
            row.get("review_state", ""),
            row.get("created_at", ""),
        ])
    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)
    tmp = Path(tempfile.mkstemp(suffix=".xlsx", prefix="prescreener_")[1])
    wb.save(tmp)
    return tmp


def split_keywords(raw: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n,]+", raw or "") if part.strip()]


def create_job(keywords: list[str], country: str, limit: int, workers: int, mode: str, max_price: float | None = None, price_only: bool = False, match_description: bool = False, counterfeit_min: int | None = None, market: str = DEFAULT_MARKET, search_type: str = DEFAULT_SEARCH_TYPE) -> str:
    job_id = uuid.uuid4().hex[:10]
    market_obj = get_market(market)
    search_type = search_type if search_type in ("keyword", "brand") else DEFAULT_SEARCH_TYPE
    term_label = "listing brand" if search_type == "brand" else "keyword"
    job = {
        "id": job_id,
        "keywords": keywords,
        "country": country,
        "market": market_obj.code,
        "search_type": search_type,
        "limit": limit,
        "workers": workers,
        "mode": mode,
        "max_price": max_price,
        "price_only": bool(price_only and max_price is not None),
        "match_description": match_description,
        "counterfeit_min": counterfeit_min,
        "status": "running",
        "message": "Queued",
        "current": 0,
        "total": max(len(keywords), 1),
        "logs": [f"Queued scan for {len(keywords)} {term_label}(s) targeting {country} on {market_obj.label}."] ,
        "results": [],
        "scanned": [],
        "stop_requested": False,
        "created_at": utc_now(),
        "finished_at": None,
    }
    with lock:
        JOBS[job_id] = job
    return job_id


def append_log(job: dict, message: str) -> None:
    with lock:
        job["logs"].append(f"[{utc_now().split('T')[1][:8]}] {message}")


def scan_listing(keyword: str, listing_url: str, target_country: str, match_description: bool = False, check_counterfeit: bool = False, market: Market | None = None, search_type: str = DEFAULT_SEARCH_TYPE) -> tuple[str, str, str, str, str, str, str, str, str, str, str, str, str, str, int, str]:
    item_id = extract_item_id(listing_url)
    try:
        product_html = fetch_html(listing_url)
    except BotChallengeDetected as exc:
        return "Unknown", "", "", "", "Unknown", "Unknown", "Unknown", str(exc), "blocked", "", "", item_id, "", "Unknown", 0, ""
    except HTTPError as exc:
        return "Unknown", "", "", "", "Unknown", "Unknown", "Unknown", f"HTTP {exc.code}", "error", "", "", item_id, "", "Unknown", 0, ""
    except URLError as exc:
        return "Unknown", "", "", "", "Unknown", "Unknown", "Unknown", f"Network error: {exc.reason}", "error", "", "", item_id, "", "Unknown", 0, ""
    except Exception as exc:  # pragma: no cover
        return "Unknown", "", "", "", "Unknown", "Unknown", "Unknown", f"Unexpected error: {exc}", "error", "", "", item_id, "", "Unknown", 0, ""

    image_url = extract_image_url(product_html)
    price_text = extract_price_text(product_html)
    if price_text != "Unknown" and market and market.currency_suffix:
        price_text = f"{price_text}{market.currency_suffix}"
    title = page_title(product_html, fallback=listing_url.rsplit("/", 1)[-1])
    description = extract_description(product_html)
    listing_brand = extract_listing_brand(product_html)
    seller_page_url = extract_seller_page_url(product_html, listing_url)
    seller_page_title = ""
    seller_html = ""
    evidence_source = "product page"

    if seller_page_url:
        try:
            seller_html = fetch_html(seller_page_url)
            seller_page_title = page_title(seller_html, fallback=seller_page_url.rsplit("/", 1)[-1])
            evidence_source = "seller page"
        except BotChallengeDetected as exc:
            evidence_source = f"seller page blocked: {exc}"
        except Exception as exc:
            evidence_source = f"seller page fetch failed: {exc}"

    scan_html = seller_html or product_html
    country_signal, seller_name, ship_from, evidence = extract_signals(scan_html, target_country)
    seller_id = extract_seller_id(seller_page_url)
    if seller_id == "Unknown":
        seller_id = extract_embedded_seller_id(product_html)
    if check_counterfeit and seller_page_url and seller_html:
        counterfeit_review_count = count_counterfeit_reviews_across_pages(seller_page_url, seller_html, fetch_html)
    else:
        counterfeit_review_count = count_counterfeit_reviews_in_html(seller_html or product_html)


    if country_signal == "Unknown" and seller_html:
        fallback_country, fallback_seller, fallback_ship, fallback_evidence = extract_signals(product_html, target_country)
        if fallback_country != "Unknown":
            country_signal, seller_name, ship_from, evidence = fallback_country, fallback_seller, fallback_ship, fallback_evidence
            evidence_source = "product page fallback"

    if seller_name == "Unknown" and seller_page_title:
        seller_name = seller_page_title
    keyword_ok = term_matches_listing(search_type, keyword, title, description, listing_brand, match_description)
    # "Any" target country means no country filter -- every listing is
    # country-eligible regardless of what extract_signals found, so status
    # depends purely on keyword_ok (or brand_ok). Otherwise, require an
    # explicit match against the chosen target country, same as before.
    country_ok = True if target_country == "Any" else (country_signal == target_country)
    if country_ok:
        evidence = f"{evidence_source}: {evidence}"
        if not keyword_ok:
            if search_type == "brand":
                evidence = f"{evidence} (listing brand {listing_brand or 'Unknown'!r} does not match target brand {keyword!r}, treating as non-match)"
            else:
                where = "title or description" if match_description else "title"
                evidence = f"{evidence} (keyword {keyword!r} not found in {where}, treating as non-match)"
    status = "match" if (country_ok and keyword_ok) else "unknown"
    return country_signal, title, description, seller_page_url, seller_name, seller_id, ship_from, evidence_source, evidence, status, seller_page_title, item_id, image_url, price_text, counterfeit_review_count, listing_brand


def scan_listing_job(keyword: str, listing_url: str, target_country: str, search_url: str, match_description: bool = False, check_counterfeit: bool = False, market: Market | None = None, search_type: str = DEFAULT_SEARCH_TYPE) -> tuple[ListingRecord, str]:
    market = market or MARKETS[DEFAULT_MARKET]
    country_signal, title, description, seller_page_url, seller_name, seller_id, ship_from, evidence_source, evidence, status, seller_page_title, item_id, image_url, price_text, counterfeit_review_count, listing_brand = scan_listing(keyword, listing_url, target_country, match_description, check_counterfeit, market, search_type)
    record = ListingRecord(
        job_id="",
        keyword=keyword,
        search_url=search_url,
        listing_url=listing_url,
        seller_page_url=seller_page_url,
        title=title or seller_page_title or listing_url,
        description=description,
        seller_name=seller_name,
        seller_id=seller_id,
        ship_from_country=ship_from,
        target_country=target_country,
        country_signal=country_signal,
        evidence_source=evidence_source,
        evidence=evidence,
        scan_status=status,
        review_state="pending" if status == "match" else "not_queued",
        created_at=utc_now(),
        item_id=item_id,
        image_url=image_url,
        price_text=price_text,
        price_value=parse_price_value(price_text),
        counterfeit_review_count=counterfeit_review_count,
        market=market.code,
        listing_brand=listing_brand,
    )
    return record, status


def run_job(job_id: str) -> None:
    with lock:
        job = JOBS[job_id]
    keywords = job["keywords"]
    country = job["country"]
    market = get_market(job.get("market"))
    search_type = job.get("search_type", DEFAULT_SEARCH_TYPE)
    page_limit = job["limit"]
    mode = job.get("mode", DEFAULT_MODE)
    max_price = job.get("max_price")
    price_only = bool(job.get("price_only") and max_price is not None)
    match_description = bool(job.get("match_description"))
    counterfeit_min = job.get("counterfeit_min")
    worker_cap = max(1, min(int(job.get("workers", LISTING_WORKERS)), MAX_LISTING_WORKERS))
    if mode == "fast":
        worker_cap = max(worker_cap, 8)
    elif mode == "thorough":
        worker_cap = min(worker_cap, 3)

    seen_urls: set[str] = set()
    discovered = 0

    def process_keyword(position: int, keyword: str) -> None:
        nonlocal discovered
        with lock:
            if job.get("stop_requested"):
                return
            job["message"] = f"Searching {market.label} for {keyword!r} ({position}/{len(keywords)})"
        append_log(job, job["message"])

        current_search_url = walmart_search_url(keyword, market)
        visited_search_pages: set[str] = set()
        pages_crawled = 0
        keyword_listing_count = 0

        while current_search_url and current_search_url not in visited_search_pages:
            with lock:
                if job.get("stop_requested"):
                    job["status"] = "stopped"
                    job["message"] = "Scan stopped by user."
                    job["finished_at"] = utc_now()
                    append_log(job, "User requested stop. Ending scan early.")
                    return
            if page_limit > 0 and pages_crawled >= page_limit:
                append_log(job, f"Page cap reached for {keyword!r}: {page_limit} page(s).")
                break
            if pages_crawled >= MAX_PAGE_SAFETY:
                append_log(job, f"Safety cap reached for {keyword!r}: {MAX_PAGE_SAFETY} pages.")
                break

            visited_search_pages.add(current_search_url)
            pages_crawled += 1
            append_log(job, f"Fetching search page {pages_crawled} for {keyword!r}: {current_search_url}")

            try:
                search_html = fetch_html(current_search_url)
            except BotChallengeDetected as exc:
                append_log(job, f"Search page blocked for {keyword!r} on page {pages_crawled}: {exc}")
                break
            except Exception as exc:
                append_log(job, f"Search failed for {keyword!r} on page {pages_crawled}: {exc}")
                break

            page_listing_urls = extract_listing_urls(search_html, market)
            next_search_url = extract_next_search_url(search_html, current_search_url)
            append_log(job, f"Page {pages_crawled} yielded {len(page_listing_urls)} candidate listing(s).")

            with lock:
                new_urls: list[str] = []
                for u in page_listing_urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        new_urls.append(u)
            if not new_urls:
                append_log(job, f"No new listings found on page {pages_crawled} for {keyword!r}.")
            else:
                worker_count = min(worker_cap, len(new_urls))
                append_log(job, f"Scanning {len(new_urls)} new listing(s) with {worker_count} worker(s).")
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    futures = {
                        pool.submit(scan_listing_job, keyword, listing_url, country, current_search_url, match_description, counterfeit_min is not None, market, search_type): listing_url
                        for listing_url in new_urls
                    }
                    for future in as_completed(futures):
                        listing_url = futures[future]
                        try:
                            record, status = future.result()
                        except Exception as exc:
                            append_log(job, f"Listing scan failed for {listing_url}: {exc}")
                            continue
                        with lock:
                            discovered += 1
                            keyword_listing_count += 1
                            job["message"] = f"Finished scan {discovered} for {keyword!r} on page {pages_crawled}"
                        record.job_id = job_id
                        keyword_ok = term_matches_listing(search_type, keyword, record.title, record.description, record.listing_brand, match_description)
                        if status in ("match", "unknown") and max_price is not None:
                            price_ok = record.price_value is not None and record.price_value <= max_price
                            if price_only:
                                status = "match" if (price_ok and keyword_ok) else "unknown"
                            elif status == "match" and not price_ok:
                                status = "unknown"
                            record.scan_status = status
                            record.review_state = "pending" if status == "match" else "not_queued"
                        if status != "match" and keyword_ok and counterfeit_min is not None and record.counterfeit_review_count >= counterfeit_min:
                            status = "match"
                            record.scan_status = status
                            record.review_state = "pending"
                        record.id = save_record(record)
                        with lock:
                            job["scanned"].append(asdict(record))
                            if status == "match":
                                job["results"].append(asdict(record))
                        if status == "match":
                            append_log(job, f"Match found: {record.title or listing_url}")

            if not next_search_url or next_search_url in visited_search_pages:
                append_log(job, f"No more search pages for {keyword!r}.")
                break
            current_search_url = next_search_url

        append_log(job, f"Finished keyword {keyword!r}: crawled {pages_crawled} page(s), inspected {keyword_listing_count} new listing(s), queued {len(job['results'])} match(es) so far.")
        with lock:
            job["current"] = min(job["total"], job.get("current", 0) + 1)

    try:
        keyword_worker_count = min(KEYWORD_WORKERS, len(keywords)) if keywords else 1
        append_log(job, f"Running up to {keyword_worker_count} keyword search(es) in parallel.")
        with ThreadPoolExecutor(max_workers=keyword_worker_count) as pool:
            futures = {
                pool.submit(process_keyword, i, keyword): keyword
                for i, keyword in enumerate(keywords, start=1)
            }
            for future in as_completed(futures):
                keyword = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    append_log(job, f"Keyword task failed for {keyword!r}: {exc}")

        with lock:
            if job.get("stop_requested"):
                job["status"] = "stopped"
                job["message"] = f"Stopped. {len(job['results'])} match(es) queued for review."
            else:
                job["status"] = "done"
                job["message"] = f"Finished. {len(job['results'])} match(es) queued for review."
            job["current"] = job["total"]
            job["finished_at"] = utc_now()
        append_log(job, job["message"])
    except Exception as exc:  # pragma: no cover
        with lock:
            job["status"] = "error"
            job["message"] = f"Job failed: {exc}"
            job["finished_at"] = utc_now()
        append_log(job, job["message"])


RESIZABLE_TABLE_SCRIPT = """
function initResizableTables() {
  var tables = document.querySelectorAll('table[data-resizable]');
  for (var t = 0; t < tables.length; t++) {
    var table = tables[t];
    if (table.dataset.resizeReady === 'true') continue;
    table.dataset.resizeReady = 'true';
    table.style.tableLayout = 'fixed';
    var headers = table.querySelectorAll('thead th');
    for (var i = 0; i < headers.length; i++) {
      (function (th) {
        var handle = document.createElement('span');
        handle.className = 'col-resize-handle';
        if (getComputedStyle(th).position === 'static') {
          th.style.position = 'relative';
        }
        th.appendChild(handle);
        var startX = 0;
        var startWidth = 0;
        function onMove(e) {
          var delta = e.clientX - startX;
          var newWidth = Math.max(50, startWidth + delta);
          th.style.width = newWidth + 'px';
        }
        function onUp() {
          document.removeEventListener('pointermove', onMove);
          document.removeEventListener('pointerup', onUp);
          handle.classList.remove('active');
        }
        handle.addEventListener('pointerdown', function (e) {
          e.preventDefault();
          startX = e.clientX;
          startWidth = th.getBoundingClientRect().width;
          handle.classList.add('active');
          document.addEventListener('pointermove', onMove);
          document.addEventListener('pointerup', onUp);
        });
      })(headers[i]);
    }
  }
}
"""

THUMB_PREVIEW_SCRIPT = """
function initThumbPreview() {
  var preview = document.getElementById('thumb-preview');
  if (!preview) {
    preview = document.createElement('div');
    preview.id = 'thumb-preview';
    preview.className = 'thumb-preview';
    var img = document.createElement('img');
    preview.appendChild(img);
    document.body.appendChild(preview);
  }
  var previewImg = preview.querySelector('img');
  function findThumbLink(target) {
    while (target && target !== document.body) {
      if (target.classList && target.classList.contains('thumb-link')) return target;
      target = target.parentNode;
    }
    return null;
  }
  document.addEventListener('mouseover', function (e) {
    var link = findThumbLink(e.target);
    if (!link) return;
    var src = link.getAttribute('data-preview-src');
    if (!src) return;
    previewImg.src = src;
    preview.style.display = 'block';
  });
  document.addEventListener('mousemove', function (e) {
    if (preview.style.display !== 'block') return;
    var x = e.clientX + 15;
    var y = e.clientY + 15;
    var maxX = window.innerWidth - 320;
    var maxY = window.innerHeight - 320;
    if (x > maxX) x = maxX;
    if (y > maxY) y = maxY;
    preview.style.left = x + 'px';
    preview.style.top = y + 'px';
  });
  document.addEventListener('mouseout', function (e) {
    var link = findThumbLink(e.target);
    if (!link) return;
    var toLink = findThumbLink(e.relatedTarget);
    if (toLink) return;
    preview.style.display = 'none';
  });
}
"""

SEARCH_FORM_SCRIPT = """
function updateSearchTypeLabels() {
  var isBrand = document.querySelector('input[name="search_type"]:checked').value === 'brand';
  document.getElementById('keywords-label').textContent = isBrand ? 'Listing Brand(s)' : 'Keywords';
  document.getElementById('keywords').placeholder = isBrand
    ? 'Example:\nWLBXH\nSOMEOTHERBRAND'
    : 'Example:\ndish soap\nlaundry detergent\npaper towels';
  document.getElementById('keywords-hint').textContent = isBrand
    ? 'One listing brand per line (exact match, case-insensitive). Every listing tagged with that brand is pulled, regardless of what the title says.'
    : 'One per line (or comma-separated). Each keyword is searched, then listings are matched by keyword-in-title.';
}

function savePrescreenerPref(key, value) {
  localStorage.setItem('prescreener_' + key, value);
}

function restorePrescreenerPrefs() {
  var market = localStorage.getItem('prescreener_market');
  var marketSelect = document.getElementById('market');
  if (market && marketSelect) marketSelect.value = market;

  var searchType = localStorage.getItem('prescreener_search_type');
  if (searchType) {
    var radio = document.querySelector('input[name="search_type"][value="' + searchType + '"]');
    if (radio) radio.checked = true;
  }

  var country = localStorage.getItem('prescreener_country');
  var countrySelect = document.getElementById('country');
  if (country && countrySelect) countrySelect.value = country;

  updateSearchTypeLabels();
}

document.addEventListener('DOMContentLoaded', restorePrescreenerPrefs);
"""


def html_page(title: str, body: str, extra_script: str = "") -> HTMLResponse:    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{
      --bg: #f7f8fa; --panel: #ffffff; --border: #d8dee9; --text: #102133; --muted: #5e6b7a; --accent: #1f4e78;
      --ok: #e2f0d9; --warn: #fff2cc; --bad: #f4cccc;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }}
    header {{ background: linear-gradient(135deg, var(--accent), #2c6aa0); color: white; padding: 20px 24px; }}
    header h1 {{ margin: 0 0 6px 0; font-size: 22px; }}
    header p {{ margin: 0; color: rgba(255,255,255,.92); }}
    main {{ padding: 20px; max-width: 1450px; margin: 0 auto; }}
    .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 1px 2px rgba(0,0,0,.04); padding: 16px; margin-bottom: 16px; }}
    .grid {{ display: grid; grid-template-columns: 1.5fr .6fr .4fr; gap: 12px; }}
    label {{ display:block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }}
    textarea, input, select {{ width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; font-size: 14px; }}
    textarea {{ min-height: 120px; resize: vertical; }}
    button, a.button {{ padding: 10px 14px; border: 0; border-radius: 10px; background: var(--accent); color: white; font-weight: 700; cursor: pointer; text-decoration: none; display:inline-block; }}
    .secondary {{ background: #6b7280; }}
    .muted {{ color: var(--muted); font-size: 13px; line-height: 1.5; }}
    .summary {{ display:flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }}
    .pill {{ background: #eef3f8; border: 1px solid var(--border); border-radius: 999px; padding: 6px 10px; font-size: 13px; }}
    .status {{ padding: 10px 12px; border-radius: 10px; background: #eef3f8; border: 1px solid var(--border); margin: 10px 0; }}
    .bar {{ height: 10px; background: #e9eef5; border-radius: 999px; overflow:hidden; margin: 10px 0 12px; }}
    .bar > div {{ height:100%; width: 22%; background: linear-gradient(90deg, #1f4e78, #2c6aa0); border-radius: 999px; animation: slide 1.1s linear infinite; }}
    @keyframes slide {{ 0% {{ transform: translateX(-140%); }} 100% {{ transform: translateX(580%); }} }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
    th, td {{ padding: 10px 8px; border-bottom: 1px solid var(--border); vertical-align: top; text-align: left; }}
    th {{ color: var(--muted); font-size: 13px; }}
    td a {{ color: #0b63ce; text-decoration: none; }}
    td a:hover {{ text-decoration: underline; }}
    .badge {{ display:inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border); }}
    .match {{ background: var(--ok); }} .unknown {{ background: var(--warn); }} .error {{ background: var(--bad); }}
    .counterfeit-flag {{ background: #fecaca; border-color: #dc2626; color: #7f1d1d; font-weight: 700; }}
    .logbox {{ background: #0f172a; color: #dbeafe; padding: 12px; border-radius: 10px; max-height: 240px; overflow:auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45; }}
    .row-actions {{ display:flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .top-actions {{ display:flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }}
    .toolbar {{ display:flex; gap: 8px; flex-wrap: wrap; align-items:end; }}
    .table-actions {{ display:flex; gap: 8px; flex-wrap: wrap; }}
    .group-row td {{ background: #f8fbff; }}
    .keyword-row td {{ background: #fcfdff; }}
    .group-item {{ transition: opacity 0.15s ease; }}
    .group-toggle {{ padding: 6px 10px; font-size: 12px; margin-right: 10px; }}
    .tiny {{ font-size: 12px; color: var(--muted); }}
    .table-scroll {{ overflow-x: auto; overflow-y: auto; max-height: 430px; border: 1px solid var(--border); border-radius: 8px; }}
    .table-scroll table {{ margin-top: 0; }}
    .table-scroll table[data-resizable] thead th {{ position: sticky; top: 0; background: var(--panel); z-index: 1; box-shadow: 0 1px 0 var(--border); }}
    table[data-resizable] {{ table-layout: fixed; }}
    table[data-resizable] th {{ position: relative; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding-right: 16px; }}
    .col-resize-handle {{ position: absolute; top: 0; right: 0; width: 8px; height: 100%; cursor: col-resize; }}
    .col-resize-handle:hover, .col-resize-handle.active {{ background: rgba(31,78,120,.35); }}
    .thumb-link img.thumb-image {{ width: 56px; height: 56px; object-fit: contain; border: 1px solid var(--border); border-radius: 6px; background: #fff; padding: 2px; cursor: zoom-in; }}
    .thumb-preview {{ display: none; position: fixed; z-index: 999; width: 300px; height: 300px; background: #fff; border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.2); padding: 8px; pointer-events: none; }}
    .thumb-preview img {{ width: 100%; height: 100%; object-fit: contain; }}
    details summary {{ cursor: pointer; font-weight: 600; }}
    details summary::-webkit-details-marker {{ color: var(--accent); }}
    .settings-accordion {{ }}
    .settings-toggle {{ list-style: none; display: inline-block; width: auto; }}
    .settings-toggle::-webkit-details-marker {{ display: none; }}
    .settings-toggle::marker {{ content: ''; }}
    .settings-toggle::before {{ content: ' '; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
{body}
<script>{extra_script}</script>
</body>
</html>"""
    )


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    body = f"""
<header>
  <h1>{html_escape(APP_TITLE)}</h1>
  <p>Best-effort listing prescreening for seller-country signals. If the country is not explicit, the tool says unknown. No fortune-telling.</p>
</header>
<main>
  <div class="card">
    <form action="/scan" method="get">
      <div class="grid" style="grid-template-columns: 1fr;">
        <div>
          <label>Search by</label>
          <div style="display:flex; gap:20px; margin-bottom:8px;">
            <label style="display:flex; align-items:center; gap:6px; margin-bottom:0; font-weight:400;">
              <input type="radio" name="search_type" value="keyword" checked onchange="savePrescreenerPref('search_type', this.value); updateSearchTypeLabels();" style="width:auto;"> Keyword
            </label>
            <label style="display:flex; align-items:center; gap:6px; margin-bottom:0; font-weight:400;">
              <input type="radio" name="search_type" value="brand" onchange="savePrescreenerPref('search_type', this.value); updateSearchTypeLabels();" style="width:auto;"> Listing Brand
            </label>
          </div>
          <label for="keywords" id="keywords-label">Keywords</label>
          <textarea id="keywords" name="keywords" placeholder="Example:\ndish soap\nlaundry detergent\npaper towels"></textarea>
          <p class="tiny" id="keywords-hint">One per line (or comma-separated). Each keyword is searched, then listings are matched by keyword-in-title.</p>
        </div>
      </div>
        <div class="grid" style="margin-top:12px;">
          <div>
            <label for="market">Market (which Walmart storefront to scan) &mdash; remembered between visits</label>
            <select id="market" name="market" onchange="savePrescreenerPref('market', this.value)">
              {''.join(f'<option value="{html_escape(m.code)}"{" selected" if m.code == DEFAULT_MARKET else ""}>{html_escape(m.label)}</option>' for m in MARKETS.values())}
            </select>
          </div>
        </div>
      <details class="settings-accordion" style="margin-top:12px;">
        <summary class="button secondary settings-toggle">Scan settings</summary>
        <div class="grid" style="margin-top:12px;">
          <div>
            <label for="country">Target country</label>
            <select id="country" name="country" onchange="savePrescreenerPref('country', this.value)">
              {''.join(f'<option value="{html_escape(option)}"{" selected" if option == DEFAULT_COUNTRY else ""}>{html_escape(COUNTRY_OPTION_LABELS.get(option, option))}</option>' for option in COUNTRY_OPTIONS)}
            </select>
          </div>
          <div>
            <label for="limit">Max search pages per keyword (0 = all)</label>
            <input id="limit" name="limit" type="number" min="0" max="500" value="{DEFAULT_LIMIT}">
          </div>
        </div>
        <div class="grid" style="margin-top:12px; grid-template-columns: .8fr .8fr 1.4fr;">
          <div>
            <label for="mode">Scan mode</label>
            <select id="mode" name="mode">
              <option value="fast">Fast</option>
              <option value="balanced" selected>Balanced</option>
              <option value="thorough">Thorough</option>
            </select>
          </div>
          <div>
            <label for="workers">Listing workers</label>
            <input id="workers" name="workers" type="number" min="1" max="12" value="{LISTING_WORKERS}">
          </div>
          <div style="align-self:end; padding-top:18px;"></div>
        </div>
        <div class="grid" style="margin-top:12px; grid-template-columns: .8fr 1.2fr;">
          <div>
            <label for="max_price">Max price ($, optional)</label>
            <input id="max_price" name="max_price" type="number" min="0" step="0.01" placeholder="No limit">
          </div>
          <div style="align-self:end;">
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:0;">
              <input id="price_only" name="price_only" type="checkbox" value="true" style="width:auto;">
              Match by price only (ignore country)
            </label>
          </div>
        </div>
        <div class="grid" style="margin-top:12px; grid-template-columns: 1fr;">
          <div>
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:0;">
              <input id="match_description" name="match_description" type="checkbox" value="true" style="width:auto;">
              Also match keyword against listing description (not just title)
            </label>
          </div>
        </div>
        <div class="grid" style="margin-top:12px; grid-template-columns: 1fr 1.5fr;">
          <div>
            <label for="counterfeit_min">Min. counterfeit-mentioning reviews (optional)</label>
            <input id="counterfeit_min" name="counterfeit_min" type="number" min="0" step="1" placeholder="Off">
          </div>
          <div style="align-self:end;">
            <span class="tiny">If set, a seller with at least this many reviews mentioning fake/counterfeit/knockoff language counts as a match too &mdash; in China, US, or Unknown, regardless of country signal.</span>
          </div>
        </div>
      </details>
      <div class="row-actions" style="margin-top:12px;">
        <button type="submit">Scan listings</button>
        <a class="button secondary" href="/queue">Review queue</a>
        <a class="button secondary" href="/export.xlsx">Export queue</a>
      </div>
    </form>
    <p class="muted" style="margin-top:12px;">
      What it does: searches the selected <strong>Market</strong> storefront for each keyword, crawls through search result pages until there are no more pages (or your page cap is reached), opens each listing, and flags pages that explicitly mention the target country.
      Pick <strong>Market</strong> to point the scan at walmart.com, walmart.ca, walmart.com.mx, or lider.cl &mdash; everything else (target country, price, keyword matching) behaves the same regardless of market.
      If Walmart does not show a seller-country clue, the listing stays unflagged or unknown. If a robot check or captcha pops up, the scan logs it and moves on.
      Open <strong>Scan settings</strong> to set a max price. With "Match by price only" checked, listings under the price count as matches regardless of country; unchecked, price is an extra filter on top of the country match.
      By default the keyword must appear in the listing title; check "Also match against description" to count a listing as a match if the keyword shows up in EITHER the title or the description.
      Set a <strong>counterfeit review threshold</strong> to also flag sellers whose reviews frequently mention fake/counterfeit/knockoff products &mdash; this works across China, US, and Unknown alike, as long as the keyword still matches.
      Switch <strong>Search by</strong> to "Listing Brand" to instead pull every listing tagged with a specific seller-supplied Brand attribute (e.g. a private bulk-seller brand code) &mdash; brand mode trusts the listing's own Brand field exactly, it does not require the brand text to appear in the title.
      Set <strong>Target country</strong> to "Any (no country filter)" to pull every listing matching your keyword/brand regardless of country signal &mdash; useful for a pure brand sweep where you just want everything under that brand, not just the China/US-flagged subset.
      Your <strong>Market</strong>, <strong>Target country</strong>, and <strong>Search by</strong> choices are remembered in this browser between visits.
    </p>
  </div>
</main>
"""
    return html_page(APP_TITLE, body, SEARCH_FORM_SCRIPT)


@app.get("/scan", response_class=HTMLResponse)
def scan(
    keywords: str = Query(default=""),
    country: str = Query(default=DEFAULT_COUNTRY),
    market: str = Query(default=DEFAULT_MARKET),
    search_type: str = Query(default=DEFAULT_SEARCH_TYPE),
    limit: int = Query(default=DEFAULT_LIMIT, ge=0, le=500),
    workers: int = Query(default=LISTING_WORKERS, ge=1, le=MAX_LISTING_WORKERS),
    mode: str = Query(default=DEFAULT_MODE),
    max_price: str = Query(default=""),
    price_only: bool = Query(default=False),
    match_description: bool = Query(default=False),
    counterfeit_min: str = Query(default=""),
) -> HTMLResponse:
    terms = split_keywords(keywords)
    if not terms:
        return home()
    parsed_max_price: float | None = None
    if max_price.strip():
        try:
            parsed_max_price = max(0.0, float(max_price.strip()))
        except ValueError:
            parsed_max_price = None
    parsed_counterfeit_min: int | None = None
    if counterfeit_min.strip():
        try:
            parsed_counterfeit_min = max(0, int(float(counterfeit_min.strip())))
        except ValueError:
            parsed_counterfeit_min = None
    job_id = create_job(terms, country, limit, workers, mode, parsed_max_price, price_only, match_description, parsed_counterfeit_min, market, search_type)
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return RedirectResponse(url=f"/job/{job_id}", status_code=302)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str) -> HTMLResponse:
    body = f"""
<header>
  <h1>{html_escape(APP_TITLE)}</h1>
  <p>Live scan progress — this page updates itself while the job runs.</p>
</header>
<main>
  <div class="card">
    <div class="summary">
      <span class="pill" id="job-id">Job: {html_escape(job_id)}</span>
      <span class="pill" id="job-market">Market: loading...</span>
      <span class="pill" id="job-status">Status: loading...</span>
      <span class="pill" id="job-message">Message: starting...</span>
      <span class="pill" id="china-count">China signal: 0</span>
      <span class="pill" id="us-count">US / explicit non-China signal: 0</span>
      <span class="pill" id="unknown-count">Unknown signal: 0</span>
    </div>
    <div class="status">
      <div><strong>What the app is doing right now</strong></div>
      <div id="job-current" class="tiny">Waiting for job data...</div>
      <div class="bar" id="progress-bar-wrap"><div id="progress-bar"></div></div>
      <div id="job-stats" class="tiny"></div>
    </div>
    <div class="top-actions">
      <form action="/job/{html_escape(job_id)}/stop" method="post" style="display:inline; margin:0;">
        <button type="submit" class="button secondary">Stop Scan</button>
      </form>
      <a class="button" href="/queue">Open review queue</a>
      <a class="button secondary" href="/export.xlsx">Export queue</a>
      <a class="button secondary" href="/">New scan</a>
    </div>
    <div class="card" style="margin:0; background:#f9fbff;">
      <div><strong>Live log</strong></div>
      <div id="job-logs" class="logbox"></div>
    </div>
    <div class="card" style="margin-top:16px; background:#fff;">
      <div><strong>Latest scan rows</strong> <span class="tiny">Most recent scan results, split by country signal</span></div>
      <label style="display:flex; align-items:center; gap:8px; margin-top:8px; font-weight:600;">
        <input id="matches-only-toggle" type="checkbox" checked style="width:auto;">
        Show confirmed matches only <span class="tiny" style="font-weight:400;">(uncheck to see every scanned listing, matched or not)</span>
      </label>
      <div class="tiny" id="latest-china-count" style="margin-top:8px;">China: 0</div>
      <div class="table-scroll">
        <table id="latest-china-table" data-resizable>
          <thead>
            <tr>
              <th>Item ID</th><th>Image</th><th>Price</th><th>Keyword</th><th>Title</th><th>Seller</th><th>Country</th><th>Counterfeit Reviews</th><th>Status</th>
            </tr>
          </thead>
          <tbody id="latest-china-body">
            <tr><td colspan="9" class="muted">Waiting for listing data...</td></tr>
          </tbody>
        </table>
      </div>
      <details style="margin-top:14px;">
        <summary class="tiny" id="latest-us-count">US signal: 0</summary>
        <div class="table-scroll" style="margin-top:8px;">
          <table id="latest-us-table" data-resizable>
            <thead>
              <tr>
                <th>Item ID</th><th>Image</th><th>Price</th><th>Keyword</th><th>Title</th><th>Seller</th><th>Country</th><th>Counterfeit Reviews</th><th>Status</th>
              </tr>
            </thead>
            <tbody id="latest-us-body">
              <tr><td colspan="9" class="muted">Waiting for listing data...</td></tr>
            </tbody>
          </table>
        </div>
      </details>
      <details style="margin-top:10px;">
        <summary class="tiny" id="latest-unknown-count">Unknown signal: 0</summary>
        <div class="table-scroll" style="margin-top:8px;">
          <table id="latest-unknown-table" data-resizable>
            <thead>
              <tr>
                <th>Item ID</th><th>Image</th><th>Price</th><th>Keyword</th><th>Title</th><th>Seller</th><th>Country</th><th>Counterfeit Reviews</th><th>Status</th>
              </tr>
            </thead>
            <tbody id="latest-unknown-body">
              <tr><td colspan="9" class="muted">Waiting for listing data...</td></tr>
            </tbody>
          </table>
        </div>
      </details>
    </div>
    <div class="card" style="margin-top:16px; background:#fff;">
      <div class="toolbar">
        <div class="tiny">All scanned listings are split into China, US, and Unknown sections. Unknown should mean genuinely unclear.</div>
      </div>
      <div class="card" style="margin: 0 0 12px 0; padding: 12px; background:#f9fbff;">
        <div class="grid" style="grid-template-columns: 1.3fr .8fr .8fr; align-items:end;">
          <div>
            <label for="filter-text">Filter text</label>
            <input id="filter-text" type="text" placeholder="Keyword, seller, listing, evidence...">
          </div>
          <div>
            <label for="filter-status">Status</label>
            <select id="filter-status">
              <option value="all">All</option>
              <option value="match">Match</option>
              <option value="unknown">Unknown</option>
              <option value="error">Error</option>
              <option value="blocked">Blocked</option>
            </select>
          </div>
          <div>
            <button type="button" class="button secondary" id="clear-filters">Clear Filters</button>
          </div>
        </div>
      </div>
      <h3 style="margin: 12px 0 6px; display:flex; justify-content:space-between; align-items:center; gap:12px;">
        <span>China signal (all scanned, check the Status column for confirmed matches)</span>
        <button type="button" class="button secondary" id="toggle-china">Hide Results</button>
      </h3>
      <table id="china-table" data-resizable>
        <thead>
          <tr>
            <th>Keyword</th><th>Listing</th><th>Seller Page</th><th>Seller</th><th>Ship From</th><th>Country</th><th>Status</th><th>Evidence</th>
          </tr>
        </thead>
        <tbody id="china-results-body">
          <tr><td colspan="8" class="muted">No China results yet. The scan is warming up.</td></tr>
        </tbody>
      </table>
      <h3 style="margin: 18px 0 6px; display:flex; justify-content:space-between; align-items:center; gap:12px;">
        <span>US / explicit non-China signal</span>
        <button type="button" class="button secondary" id="toggle-us">Show Results</button>
      </h3>
      <table id="us-table" data-resizable style="display:none;">
        <thead>
          <tr>
            <th>Keyword</th><th>Listing</th><th>Seller Page</th><th>Seller</th><th>Ship From</th><th>Country</th><th>Status</th><th>Evidence</th>
          </tr>
        </thead>
        <tbody id="us-results-body">
          <tr><td colspan="8" class="muted">No US results yet. The scan is warming up.</td></tr>
        </tbody>
      </table>
      <h3 style="margin: 18px 0 6px; display:flex; justify-content:space-between; align-items:center; gap:12px;">
        <span>Unknown country signal</span>
        <button type="button" class="button secondary" id="toggle-unknown">Show Results</button>
      </h3>
      <table id="unknown-table" data-resizable style="display:none;">
        <thead>
          <tr>
            <th>Keyword</th><th>Listing</th><th>Seller Page</th><th>Seller</th><th>Ship From</th><th>Country</th><th>Status</th><th>Evidence</th>
          </tr>
        </thead>
        <tbody id="unknown-results-body">
          <tr><td colspan="8" class="muted">No Unknown results yet. The scan is warming up.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>
"""
    extra_script = """
const jobId = __JOB_ID__;
function escapeHtml(text) {
  return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function renderGroupedRows(rows, sectionKey) {
  const sorted = [...rows].sort((a, b) => {
    const sellerA = (a.seller_name || 'Unknown').toLowerCase();
    const sellerB = (b.seller_name || 'Unknown').toLowerCase();
    if (sellerA !== sellerB) return sellerA.localeCompare(sellerB);
    const keywordA = (a.keyword || '').toLowerCase();
    const keywordB = (b.keyword || '').toLowerCase();
    if (keywordA !== keywordB) return keywordA.localeCompare(keywordB);
    return (a.title || a.listing_url || '').localeCompare(b.title || b.listing_url || '');
  });

  const sellerGroups = [];
  for (const row of sorted) {
    const sellerKey = row.seller_name || 'Unknown';
    const lastSeller = sellerGroups[sellerGroups.length - 1];
    if (!lastSeller || lastSeller.key !== sellerKey) {
      sellerGroups.push({ key: sellerKey, rows: [row], keywords: [] });
    } else {
      lastSeller.rows.push(row);
    }
  }

  return sellerGroups.map(sellerGroup => {
    const sellerKey = `${sectionKey}::seller::${sellerGroup.key}`;
    const sellerCollapsed = collapsedGroups.has(sellerKey);
    const sellerId = sellerGroup.rows[0] && sellerGroup.rows[0].seller_page_url
      ? `<a href="${escapeHtml(sellerGroup.rows[0].seller_page_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(sellerGroup.rows[0].seller_id || 'Unknown')}</a>`
      : escapeHtml((sellerGroup.rows[0] && sellerGroup.rows[0].seller_id) || 'Unknown');

    const keywordGroups = [];
    for (const row of sellerGroup.rows) {
      const keywordKey = row.keyword || 'Unknown';
      const lastKeyword = keywordGroups[keywordGroups.length - 1];
      if (!lastKeyword || lastKeyword.key !== keywordKey) {
        keywordGroups.push({ key: keywordKey, rows: [row] });
      } else {
        lastKeyword.rows.push(row);
      }
    }

    const keywordHtml = keywordGroups.map(keywordGroup => {
      const keywordKey = `${sellerKey}::keyword::${keywordGroup.key}`;
      const keywordCollapsed = sellerCollapsed || collapsedGroups.has(keywordKey);
      return `
    <tr class="keyword-row" data-group-key="${escapeHtml(keywordKey)}">
      <td colspan="8">
        <button type="button" class="button secondary group-toggle" data-group-key="${escapeHtml(keywordKey)}">${keywordCollapsed ? 'Expand' : 'Collapse'}</button>
        <strong>${escapeHtml(keywordGroup.key)}</strong> <span class="tiny">(${keywordGroup.rows.length} result${keywordGroup.rows.length === 1 ? '' : 's'})</span>
      </td>
    </tr>
    ${keywordGroup.rows.map(r => `
    <tr class="group-item" data-group-key="${escapeHtml(sellerKey)} ${escapeHtml(keywordKey)}"${keywordCollapsed ? ' style="display:none;"' : ''}>
      <td>${escapeHtml(r.keyword)}</td>
      <td><a href="${escapeHtml(r.listing_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.title || r.listing_url)}</a></td>
      <td>${escapeHtml(r.seller_page_url || 'Unknown')}</td>
      <td>${escapeHtml(r.seller_name || 'Unknown')}</td>
      <td>${escapeHtml(r.ship_from_country || 'Unknown')}</td>
      <td>${escapeHtml(r.country_signal || 'Unknown')}</td>
      <td><span class="badge ${escapeHtml(r.scan_status || 'unknown')}">${escapeHtml(r.scan_status || 'unknown')}</span></td>
      <td>${escapeHtml(r.evidence || '')}</td>
    </tr>`).join('')}`;
    }).join('');

    return `
    <tr class="group-row" data-group-key="${escapeHtml(sellerKey)}">
      <td colspan="8">
        <button type="button" class="button secondary group-toggle" data-group-key="${escapeHtml(sellerKey)}">${sellerCollapsed ? 'Expand' : 'Collapse'}</button>
        <strong>${escapeHtml(sellerGroup.key)}</strong> <span class="tiny">(${sellerGroup.rows.length} result${sellerGroup.rows.length === 1 ? '' : 's'}) Seller ID: ${sellerId}</span>
      </td>
    </tr>
    ${keywordHtml}
  `;
  }).join('');
}

const collapsedGroups = new Set(JSON.parse(localStorage.getItem('collapsedGroups') || '[]'));
function persistCollapsedGroups() {
  localStorage.setItem('collapsedGroups', JSON.stringify([...collapsedGroups]));
}
function wireGroupToggles(scope) {
  scope.querySelectorAll('.group-toggle').forEach(btn => {
    btn.onclick = () => {
      const key = btn.dataset.groupKey;
      if (!key) return;
      if (collapsedGroups.has(key)) collapsedGroups.delete(key);
      else collapsedGroups.add(key);
      persistCollapsedGroups();
      refreshJob();
    };
  });
}
const matchesOnlyToggleEl = document.getElementById('matches-only-toggle');
if (matchesOnlyToggleEl) {
  matchesOnlyToggleEl.addEventListener('change', refreshJob);
}

async function refreshJob() {
  const res = await fetch(`/api/job/${jobId}`);
  const data = await res.json();
  document.getElementById('job-status').textContent = 'Status: ' + data.status;
  document.getElementById('job-market').textContent = 'Market: ' + (data.market || 'US');
  document.getElementById('job-message').textContent = 'Message: ' + data.message;
  document.getElementById('job-current').textContent = data.status === 'stopping' ? 'Stopping scan...' : data.message;
  document.getElementById('job-stats').textContent = `Progress: ${data.current} / ${data.total} | Scanned: ${(data.scanned || []).length} | Matches queued: ${data.results.length}`;
  const logs = document.getElementById('job-logs');
  logs.innerHTML = data.logs.map(line => '<div>' + line.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>').join('');
  logs.scrollTop = logs.scrollHeight;
  const scanned = data.scanned || [];
  const matchesOnlyToggle = document.getElementById('matches-only-toggle');
  const matchesOnly = matchesOnlyToggle ? matchesOnlyToggle.checked : true;
  const chinaRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'China');
  const usRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'US');
  const unknownRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'Unknown');
  document.getElementById('china-count').textContent = `China signal: ${chinaRows.length}`;
  document.getElementById('us-count').textContent = `US / explicit non-China signal: ${usRows.length}`;
  document.getElementById('unknown-count').textContent = `Unknown signal: ${unknownRows.length}`;
  const latestChinaBody = document.getElementById('latest-china-body');
  if (latestChinaBody) {
    function renderLatestRows(rows) {
      const visible = matchesOnly ? rows.filter(r => (r.scan_status || 'unknown') === 'match') : rows;
      return visible.slice(-20).map(r => `
      <tr>
        <td>${escapeHtml(r.item_id || '')}</td>
        <td>${r.image_url ? '<a class="thumb-link" href="' + escapeHtml(r.image_url) + '" target="_blank" rel="noopener noreferrer" data-preview-src="' + escapeHtml(r.image_url) + '"><img class="thumb-image" src="' + escapeHtml(r.image_url) + '" alt="Listing image"></a>' : 'Unknown'}</td>
        <td>${escapeHtml(r.price_text || (r.price_value != null ? '$' + Number(r.price_value).toFixed(2) : 'Unknown'))}</td>
        <td>${escapeHtml(r.keyword || '')}</td>
        <td><a href="${escapeHtml(r.listing_url || '#')}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.title || r.listing_url || '')}</a></td>
        <td>${r.seller_page_url ? `<a href="${escapeHtml(r.seller_page_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.seller_id || 'Unknown')}</a>` : escapeHtml(r.seller_id || 'Unknown')}</td>
        <td>${escapeHtml(r.country_signal || 'Unknown')}</td>
        <td>${r.counterfeit_review_count ? `<span class="badge counterfeit-flag">${escapeHtml(String(r.counterfeit_review_count))}</span>` : '0'}</td>
        <td><span class="badge ${escapeHtml(r.scan_status || 'unknown')}">${escapeHtml(r.scan_status || 'unknown')}</span></td>
      </tr>`).join('') || `<tr><td colspan="9" class="muted">${matchesOnly ? 'No confirmed matches yet.' : 'No scan rows yet.'}</td></tr>`;
    }
    const chinaVisible = matchesOnly ? chinaRows.filter(r => (r.scan_status || 'unknown') === 'match') : chinaRows;
    const usVisible = matchesOnly ? usRows.filter(r => (r.scan_status || 'unknown') === 'match') : usRows;
    const unknownVisible = matchesOnly ? unknownRows.filter(r => (r.scan_status || 'unknown') === 'match') : unknownRows;
    document.getElementById('latest-china-count').textContent = `China: ${chinaVisible.length}${matchesOnly ? ' matches' : ' scanned'}`;
    document.getElementById('latest-us-count').textContent = `US: ${usVisible.length}${matchesOnly ? ' matches' : ' scanned'}`;
    document.getElementById('latest-unknown-count').textContent = `Unknown: ${unknownVisible.length}${matchesOnly ? ' matches' : ' scanned'}`;
    latestChinaBody.innerHTML = renderLatestRows(chinaRows);
    document.getElementById('latest-us-body').innerHTML = renderLatestRows(usRows);
    document.getElementById('latest-unknown-body').innerHTML = renderLatestRows(unknownRows);
  }
  const filterTextEl = document.getElementById('filter-text');
  const filterText = (filterTextEl ? filterTextEl.value : '').trim().toLowerCase();
  const filterStatusEl = document.getElementById('filter-status');
  const filterStatus = filterStatusEl ? filterStatusEl.value : 'all';
  const applyFilters = (rows) => rows.filter(r => {
    const haystack = [r.keyword, r.title, r.listing_url, r.seller_page_url, r.seller_name, r.ship_from_country, r.country_signal, r.evidence, r.scan_status].join(' ').toLowerCase();
    if (filterText && !haystack.includes(filterText)) return false;
    if (filterStatus !== 'all' && (r.scan_status || 'unknown') !== filterStatus) return false;
    return true;
  });
  const chinaTable = document.getElementById('china-table');
  const usTable = document.getElementById('us-table');
  const unknownTable = document.getElementById('unknown-table');
  const chinaBody = document.getElementById('china-results-body');
  const usBody = document.getElementById('us-results-body');
  const unknownBody = document.getElementById('unknown-results-body');
  const toggleChina = document.getElementById('toggle-china');
  const toggleUS = document.getElementById('toggle-us');
  const toggleUnknown = document.getElementById('toggle-unknown');
  const clearFilters = document.getElementById('clear-filters');
  const rerender = () => {
    const filteredChina = applyFilters(chinaRows);
    const filteredUS = applyFilters(usRows);
    const filteredUnknown = applyFilters(unknownRows);
    chinaBody.innerHTML = filteredChina.length ? renderGroupedRows(filteredChina, 'china') : '<tr><td colspan="8" class="muted">No China results match the filters.</td></tr>';
    usBody.innerHTML = filteredUS.length ? renderGroupedRows(filteredUS, 'us') : '<tr><td colspan="8" class="muted">No US results match the filters.</td></tr>';
    unknownBody.innerHTML = filteredUnknown.length ? renderGroupedRows(filteredUnknown, 'unknown') : '<tr><td colspan="8" class="muted">No Unknown results match the filters.</td></tr>';
  };
  toggleUS.onclick = () => {
    const hidden = usTable.style.display === 'none';
    usTable.style.display = hidden ? '' : 'none';
    toggleUS.textContent = hidden ? 'Hide Results' : 'Show Results';
  };
  toggleUnknown.onclick = () => {
    const hidden = unknownTable.style.display === 'none';
    unknownTable.style.display = hidden ? '' : 'none';
    toggleUnknown.textContent = hidden ? 'Hide Results' : 'Show Results';
  };
  clearFilters.onclick = () => {
    document.getElementById('filter-text').value = '';
    document.getElementById('filter-status').value = 'all';
    rerender();
  };
  document.getElementById('filter-text').addEventListener('input', rerender);
  document.getElementById('filter-status').addEventListener('change', rerender);
  rerender();
  wireGroupToggles(chinaBody);
  wireGroupToggles(usBody);
  wireGroupToggles(unknownBody);
  const terminalStatuses = new Set(['done', 'stopped', 'error', 'stopping']);
  if (!terminalStatuses.has(data.status)) {
    setTimeout(refreshJob, 1200);
  }
}
refreshJob();
initResizableTables();
initThumbPreview();
"""
    extra_script = RESIZABLE_TABLE_SCRIPT + THUMB_PREVIEW_SCRIPT + extra_script
    extra_script = extra_script.replace('__JOB_ID__', json.dumps(job_id))
    return html_page(APP_TITLE, body, extra_script)




@app.post("/job/{job_id}/stop")
def stop_job(job_id: str) -> RedirectResponse:
    with lock:
        job = JOBS.get(job_id)
        if job:
            job["stop_requested"] = True
            job["status"] = "stopping"
            job["message"] = "Stopping scan..."
            append_log(job, "Stop requested by user.")
    return RedirectResponse(url=f"/job/{job_id}", status_code=302)


@app.get("/api/job/{job_id}")
def api_job(job_id: str) -> JSONResponse:
    with lock:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"error": "job not found"}, status_code=404)
        payload = {k: v for k, v in job.items() if k != "keywords"}
    return JSONResponse(payload)


@app.get("/queue", response_class=HTMLResponse)
def queue_page(state: str = Query(default="pending"), country_signal: str = Query(default="China")) -> HTMLResponse:
    rows = get_reviews(state, country_signal)
    state_counts = {s: len(get_reviews(s, country_signal)) for s in ["pending", "approved", "rejected", "not_queued"]}
    country_counts = {c: len(get_reviews(state, c)) for c in ["China", "US", "Unknown", "all"]}
    state_links = "".join(
        f'<a class="button secondary" href="/queue?state={s}&country_signal={country_signal}">{s.title()} ({state_counts[s]})</a>' for s in state_counts
    )
    country_links = "".join(
        f'<a class="button secondary" href="/queue?state={state}&country_signal={c}">{c.title()} ({country_counts[c]})</a>' for c in ["China", "US", "Unknown", "all"]
    )
    table_rows = []
    return_to = quote_plus(f"/queue?state={state}&country_signal={country_signal}")
    for row in rows:
        actions = (
            f"<a class='button' href='/review/{row['id']}/approve?return_to={return_to}'>Approve</a>"
            f"<a class='button secondary' href='/review/{row['id']}/reject?return_to={return_to}'>Reject</a>"
        )
        table_rows.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{html_escape(row['keyword'])}</td>"
            f"<td>{html_escape(row.get('listing_brand', ''))}</td>"
            f"<td>{html_escape(row.get('market', DEFAULT_MARKET))}</td>"
            f"<td><a href='{html_escape(row['listing_url'])}' target='_blank' rel='noopener noreferrer'>{html_escape(row['title'])}</a></td>"
            f"<td><a href='{html_escape(row['seller_page_url'])}' target='_blank' rel='noopener noreferrer'>{html_escape(row['seller_page_url']) or 'Unknown'}</a></td>"
            f"<td>{html_escape(row['seller_name'])}</td>"
            f"<td>{html_escape(row['ship_from_country'])}</td>"
            f"<td>{html_escape(row['country_signal'])}</td>"
            f"<td>{html_escape(row['evidence_source'])}</td>"
            f"<td>{html_escape(row['evidence'])}</td>"
            f"<td><span class='badge {row['scan_status']}'>{html_escape(row['scan_status'])}</span></td>"
            f"<td>{html_escape(row['review_state'])}</td>"
            f"<td class='table-actions'>{actions}</td>"
            "</tr>"
        )
    if not table_rows:
        table_rows.append("<tr><td colspan='14' class='muted'>No items in this queue view.</td></tr>")
    body = f"""
<header>
  <h1>{html_escape(APP_TITLE)}</h1>
  <p>Manual review queue. Approve or reject prescreened listings, then export the evidence.</p>
</header>
<main>
  <div class="card">
    <div class="top-actions">
      <a class="button" href="/">New scan</a>
      <a class="button secondary" href="/export.xlsx">Export XLSX</a>
    </div>
    <div class="row-actions">
      <div class="tiny" style="width:100%; margin-bottom:6px;">Review status</div>
      {state_links}
    </div>
    <div class="row-actions">
      <div class="tiny" style="width:100%; margin-bottom:6px;">Country view</div>
      {country_links}
    </div>
    <table data-resizable>
      <thead>
        <tr>
          <th>ID</th><th>Keyword</th><th>Listing Brand</th><th>Market</th><th>Listing</th><th>Seller Page</th><th>Seller</th><th>Ship From</th><th>Country</th><th>Evidence Source</th><th>Evidence</th><th>Status</th><th>Review</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {''.join(table_rows)}
      </tbody>
    </table>
  </div>
</main>
"""
    return html_page(APP_TITLE, body, RESIZABLE_TABLE_SCRIPT + "initResizableTables();")


@app.get("/review/{record_id}/{action}")
def review_action(record_id: int, action: str, return_to: str = "/queue") -> RedirectResponse:
    if action not in {"approve", "reject"}:
        return RedirectResponse(url=return_to, status_code=302)
    update_review_state(record_id, "approved" if action == "approve" else "rejected")
    return RedirectResponse(url=return_to, status_code=302)


@app.get("/export.xlsx")
def export_xlsx() -> FileResponse:
    path = export_reviews_xlsx()
    return FileResponse(
        path,
        filename="prescreener_review_queue.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, reload=False)
