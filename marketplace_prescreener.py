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
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from openpyxl import Workbook

APP_TITLE = "Marketplace Country Prescreener"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_COUNTRY = "China"
DEFAULT_LIMIT = 0
SEARCH_TIMEOUT = 15
MAX_PAGE_SAFETY = 250
LISTING_WORKERS = 5
MAX_LISTING_WORKERS = 12
DEFAULT_STOP_AFTER_FIRST_MATCH = False
DEFAULT_MODE = "balanced"
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
lock = threading.Lock()
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
    id: int | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def walmart_search_url(keyword: str) -> str:
    return f"https://www.walmart.com/search?q={quote_plus(keyword)}"


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


def extract_listing_urls(search_html: str) -> list[str]:
    patterns = [r"https://www\.walmart\.com/ip/[^\"'<>\s]+", r"/ip/[^\"'<>\s]+"]
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, search_html):
            url = raw if raw.startswith("http") else urljoin("https://www.walmart.com", raw)
            url = url.split("?")[0]
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def extract_next_search_url(search_html: str, current_url: str) -> str:
    patterns = [
        r'<a[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
        r'<a[^>]+aria-label=["\'][^"\']*next[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*aria-label=["\'][^"\']*next[^"\']*["\']',
        r'href=["\']([^"\']*(?:[?&](?:page|offset)=\d+)[^"\']*)["\']',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, search_html, flags=re.I)
        for raw in matches:
            next_url = raw if raw.startswith("http") else urljoin(current_url, raw)
            next_url = next_url.split("#")[0]
            if "walmart.com/search" in next_url and next_url != current_url:
                return next_url
    return ""


def extract_seller_page_url(product_html: str, listing_url: str) -> str:
    patterns = [
        r'href=["\']([^"\']*(?:/seller/|seller\?|seller/|/store/|store/)[^"\']*)["\']',
        r'href=["\']([^"\']*seller[^"\']*)["\']',
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for raw in re.findall(pattern, product_html, flags=re.I):
            url = raw if raw.startswith("http") else urljoin(listing_url, raw)
            url = url.split("#")[0].split("?")[0]
            if url not in seen and "walmart.com" in url and "/ip/" not in url and "/search" not in url:
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


COUNTRY_HINTS: dict[str, list[re.Pattern[str]]] = {
    "China": [
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)\s*[:\-]?\s*[^<>{}\n]{0,180}\b(?:china|cn)\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,180}\bchina\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,180}\bcn\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,220}\b(?:guangzhou|shenzhen|hangzhou|dongguan|foshan|yiwu|ningbo|suzhou|wenzhou|xiamen|qingdao|tianjin|beijing|shanghai|zhuhai|guangdong|zhejiang|jiangsu|shandong)\b", re.I),
        re.compile(r"(?:ships?|shipping|ship to|seller(?:\s+location)?|seller address|address|location|country of origin|based in)[^<>{}\n]{0,220}\b(?:GD|ZJ|JS|SH|BJ|CQ|SC|HN|HB|SD|FJ|JX|AH|GX|LN|TJ|HE|HL|JL)\s+\d{6}\b", re.I),
        re.compile(r"\bfrom\s+china\b", re.I),
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


def extract_signals(html: str, target_country: str) -> tuple[str, str, str, str]:
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
                scan_status, review_state, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        "ID", "Job ID", "Keyword", "Search URL", "Listing URL", "Seller Page URL", "Title", "Seller Name", "Seller ID",
        "Ship From Country", "Target Country", "Country Signal", "Evidence Source", "Evidence", "Scan Status",
        "Review State", "Created At",
    ]
    ws.append(headers)
    for row in rows:
        ws.append([
            row.get("id", ""),
            row.get("job_id", ""),
            row.get("keyword", ""),
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


def create_job(keywords: list[str], country: str, limit: int, workers: int, stop_after_first_match: bool, mode: str) -> str:
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "keywords": keywords,
        "country": country,
        "limit": limit,
        "workers": workers,
        "stop_after_first_match": stop_after_first_match,
        "mode": mode,
        "status": "running",
        "message": "Queued",
        "current": 0,
        "total": max(len(keywords), 1),
        "logs": [f"Queued scan for {len(keywords)} keyword(s) targeting {country}."] ,
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
    job["logs"].append(f"[{utc_now().split('T')[1][:8]}] {message}")


def scan_listing(keyword: str, listing_url: str, target_country: str) -> tuple[str, str, str, str, str, str, str, str, str, str]:
    try:
        product_html = fetch_html(listing_url)
    except BotChallengeDetected as exc:
        return "Unknown", "", "", "Unknown", "Unknown", "Unknown", str(exc), "blocked", "", ""
    except HTTPError as exc:
        return "Unknown", "", "", "Unknown", "Unknown", "Unknown", f"HTTP {exc.code}", "error", "", ""
    except URLError as exc:
        return "Unknown", "", "", "Unknown", "Unknown", "Unknown", f"Network error: {exc.reason}", "error", "", ""
    except Exception as exc:  # pragma: no cover
        return "Unknown", "", "", "Unknown", "Unknown", "Unknown", f"Unexpected error: {exc}", "error", "", ""

    title = page_title(product_html, fallback=listing_url.rsplit("/", 1)[-1])
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


    if country_signal == "Unknown" and seller_html:
        fallback_country, fallback_seller, fallback_ship, fallback_evidence = extract_signals(product_html, target_country)
        if fallback_country != "Unknown":
            country_signal, seller_name, ship_from, evidence = fallback_country, fallback_seller, fallback_ship, fallback_evidence
            evidence_source = "product page fallback"

    if seller_name == "Unknown" and seller_page_title:
        seller_name = seller_page_title
    if country_signal == target_country:
        evidence = f"{evidence_source}: {evidence}"
    status = "match" if country_signal == target_country else "unknown"
    return country_signal, title, seller_page_url, seller_name, seller_id, ship_from, evidence_source, evidence, status, seller_page_title


def scan_listing_job(keyword: str, listing_url: str, target_country: str, search_url: str) -> tuple[ListingRecord, str]:
    country_signal, title, seller_page_url, seller_name, seller_id, ship_from, evidence_source, evidence, status, seller_page_title = scan_listing(keyword, listing_url, target_country)
    record = ListingRecord(
        job_id="",
        keyword=keyword,
        search_url=search_url,
        listing_url=listing_url,
        seller_page_url=seller_page_url,
        title=title or seller_page_title or listing_url,
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
    )
    return record, status


def run_job(job_id: str) -> None:
    with lock:
        job = JOBS[job_id]
    try:
        keywords = job["keywords"]
        country = job["country"]
        page_limit = job["limit"]
        mode = job.get("mode", DEFAULT_MODE)
        worker_cap = max(1, min(int(job.get("workers", LISTING_WORKERS)), MAX_LISTING_WORKERS))
        stop_after_first_match = bool(job.get("stop_after_first_match", False))
        if mode == "fast":
            worker_cap = max(worker_cap, 8)
            stop_after_first_match = True
        elif mode == "thorough":
            worker_cap = min(worker_cap, 3)
            stop_after_first_match = False
        seen_urls: set[str] = set()
        discovered = 0

        for i, keyword in enumerate(keywords, start=1):
            with lock:
                if job.get("stop_requested"):
                    job["status"] = "stopped"
                    job["message"] = "Scan stopped by user."
                    job["finished_at"] = utc_now()
                    append_log(job, "User requested stop. Ending scan early.")
                    return
                job["current"] = i - 1
                job["message"] = f"Searching Walmart for {keyword!r} ({i}/{len(keywords)})"
            append_log(job, job["message"])

            current_search_url = walmart_search_url(keyword)
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
                if stop_after_first_match and keyword_listing_count > 0 and any(r.get("keyword") == keyword and r.get("country_signal") == country for r in job["results"]):
                    append_log(job, f"Stopping early for {keyword!r} after first match.")
                    break
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

                page_listing_urls = extract_listing_urls(search_html)
                next_search_url = extract_next_search_url(search_html, current_search_url)
                append_log(job, f"Page {pages_crawled} yielded {len(page_listing_urls)} candidate listing(s).")

                new_urls = [u for u in page_listing_urls if u not in seen_urls]
                for listing_url in new_urls:
                    seen_urls.add(listing_url)
                if not new_urls:
                    append_log(job, f"No new listings found on page {pages_crawled} for {keyword!r}.")
                else:
                    worker_count = min(worker_cap, len(new_urls))
                    append_log(job, f"Scanning {len(new_urls)} new listing(s) with {worker_count} worker(s).")
                    with ThreadPoolExecutor(max_workers=worker_count) as pool:
                        futures = {
                            pool.submit(scan_listing_job, keyword, listing_url, country, current_search_url): listing_url
                            for listing_url in new_urls
                        }
                        for future in as_completed(futures):
                            listing_url = futures[future]
                            try:
                                record, status = future.result()
                            except Exception as exc:
                                append_log(job, f"Listing scan failed for {listing_url}: {exc}")
                                continue
                            discovered += 1
                            keyword_listing_count += 1
                            with lock:
                                job["message"] = f"Finished scan {discovered} for {keyword!r} on page {pages_crawled}"
                            record.job_id = job_id
                            record.id = save_record(record)
                            with lock:
                                job["scanned"].append(asdict(record))
                            if status == "match":
                                with lock:
                                    job["results"].append(asdict(record))
                                append_log(job, f"Match found: {record.title or listing_url}")
                                if stop_after_first_match:
                                    append_log(job, f"Fast mode stopping after first match for {keyword!r}.")
                                    break

                if not next_search_url or next_search_url in visited_search_pages:
                    append_log(job, f"No more search pages for {keyword!r}.")
                    break
                current_search_url = next_search_url

            append_log(job, f"Finished keyword {keyword!r}: crawled {pages_crawled} page(s), inspected {keyword_listing_count} new listing(s), queued {len(job['results'])} match(es) so far.")

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

def html_page(title: str, body: str, extra_script: str = "") -> HTMLResponse:
    return HTMLResponse(
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
    .logbox {{ background: #0f172a; color: #dbeafe; padding: 12px; border-radius: 10px; max-height: 240px; overflow:auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.45; }}
    .row-actions {{ display:flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }}
    .top-actions {{ display:flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }}
    .toolbar {{ display:flex; gap: 8px; flex-wrap: wrap; align-items:end; }}
    .table-actions {{ display:flex; gap: 8px; flex-wrap: wrap; }}
    .group-row td {{ background: #f8fbff; }}
    .group-item {{ transition: opacity 0.15s ease; }}
    .group-toggle {{ padding: 6px 10px; font-size: 12px; margin-right: 10px; }}
    .tiny {{ font-size: 12px; color: var(--muted); }}
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
      <div class="grid">
        <div>
          <label for="keywords">Keywords</label>
          <textarea id="keywords" name="keywords" placeholder="Example:\ndish soap\nlaundry detergent\npaper towels"></textarea>
        </div>
        <div>
          <label for="country">Target country</label>
          <input id="country" name="country" value="{html_escape(DEFAULT_COUNTRY)}">
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
        <div style="align-self:end; padding-top:18px;">
          <label style="display:flex; gap:8px; align-items:center; margin:0; color: var(--text);">
            <input id="stop_after_first_match" name="stop_after_first_match" type="checkbox">
            Stop after first match per keyword
          </label>
        </div>
      </div>
      <div class="row-actions" style="margin-top:12px;">
        <button type="submit">Scan listings</button>
        <a class="button secondary" href="/queue">Review queue</a>
        <a class="button secondary" href="/export.xlsx">Export queue</a>
      </div>
    </form>
    <p class="muted" style="margin-top:12px;">
      What it does: searches Walmart for each keyword, crawls through search result pages until there are no more pages (or your page cap is reached), opens each listing, and flags pages that explicitly mention the target country.
      If Walmart does not show a seller-country clue, the listing stays unflagged or unknown. If a robot check or captcha pops up, the scan logs it and moves on.
    </p>
  </div>
</main>
"""
    return html_page(APP_TITLE, body)


@app.get("/scan", response_class=HTMLResponse)
def scan(
    keywords: str = Query(default=""),
    country: str = Query(default=DEFAULT_COUNTRY),
    limit: int = Query(default=DEFAULT_LIMIT, ge=0, le=500),
    workers: int = Query(default=LISTING_WORKERS, ge=1, le=MAX_LISTING_WORKERS),
    stop_after_first_match: bool = Query(default=DEFAULT_STOP_AFTER_FIRST_MATCH),
    mode: str = Query(default=DEFAULT_MODE),
) -> HTMLResponse:
    terms = split_keywords(keywords)
    if not terms:
        return home()
    job_id = create_job(terms, country, limit, workers, stop_after_first_match, mode)
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
      <span class="pill" id="job-status">Status: loading...</span>
      <span class="pill" id="job-message">Message: starting...</span>
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
        <span>China matches</span>
        <button type="button" class="button secondary" id="toggle-china">Hide Results</button>
      </h3>
      <table id="china-table">
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
        <span>US / explicit non-China matches</span>
        <button type="button" class="button secondary" id="toggle-us">Show Results</button>
      </h3>
      <table id="us-table" style="display:none;">
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
        <span>Unknown country matches</span>
        <button type="button" class="button secondary" id="toggle-unknown">Show Results</button>
      </h3>
      <table id="unknown-table" style="display:none;">
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
    return (a.title || a.listing_url || '').localeCompare(b.title || b.listing_url || '');
  });
  const groups = [];
  for (const row of sorted) {
    const key = row.seller_name || 'Unknown';
    const last = groups[groups.length - 1];
    if (!last || last.key !== key) groups.push({ key, rows: [row] });
    else last.rows.push(row);
  }
  return groups.map(group => {
    const groupKey = `${sectionKey}::${group.key}`;
    const collapsed = collapsedGroups.has(groupKey);
    return `
    <tr class="group-row" data-group-key="${escapeHtml(groupKey)}">
      <td colspan="8">
        <button type="button" class="button secondary group-toggle" data-group-key="${escapeHtml(groupKey)}">${collapsed ? 'Expand' : 'Collapse'}</button>
        <strong>${escapeHtml(group.key)}</strong> <span class="tiny">(${group.rows.length} result${group.rows.length === 1 ? '' : 's'}) Seller ID: ${group.rows[0].seller_page_url ? `<a href="${escapeHtml(group.rows[0].seller_page_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(group.rows[0].seller_id || 'Unknown')}</a>` : escapeHtml(group.rows[0].seller_id || 'Unknown')}</span>
      </td>
    </tr>
    ${group.rows.map(r => `
    <tr class="group-item" data-group-key="${escapeHtml(groupKey)}"${collapsed ? ' style="display:none;"' : ''}>
      <td>${escapeHtml(r.keyword)}</td>
      <td><a href="${escapeHtml(r.listing_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(r.title || r.listing_url)}</a></td>
      <td>${escapeHtml(r.seller_page_url || 'Unknown')}</td>
      <td>${escapeHtml(r.seller_name || 'Unknown')}</td>
      <td>${escapeHtml(r.ship_from_country || 'Unknown')}</td>
      <td>${escapeHtml(r.country_signal || 'Unknown')}</td>
      <td><span class="badge ${escapeHtml(r.scan_status || 'unknown')}">${escapeHtml(r.scan_status || 'unknown')}</span></td>
      <td>${escapeHtml(r.evidence || '')}</td>
    </tr>`).join('')}
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

async function refreshJob() {
  const res = await fetch(`/api/job/${jobId}`);
  const data = await res.json();
  document.getElementById('job-status').textContent = 'Status: ' + data.status;
  document.getElementById('job-message').textContent = 'Message: ' + data.message;
  document.getElementById('job-current').textContent = data.status === 'stopping' ? 'Stopping scan...' : data.message;
  document.getElementById('job-stats').textContent = `Progress: ${data.current} / ${data.total} | Scanned: ${(data.scanned || []).length} | Matches queued: ${data.results.length}`;
  const logs = document.getElementById('job-logs');
  logs.innerHTML = data.logs.map(line => '<div>' + line.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</div>').join('');
  logs.scrollTop = logs.scrollHeight;
  const scanned = data.scanned || [];
  const chinaRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'China');
  const usRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'US');
  const unknownRows = scanned.filter(r => (r.country_signal || 'Unknown') === 'Unknown');
  const filterText = (document.getElementById('filter-text')?.value || '').trim().toLowerCase();
  const filterStatus = document.getElementById('filter-status')?.value || 'all';
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
  toggleChina.onclick = () => {
    const hidden = chinaTable.style.display === 'none';
    chinaTable.style.display = hidden ? '' : 'none';
    toggleChina.textContent = hidden ? 'Hide Results' : 'Show Results';
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
"""
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
        table_rows.append("<tr><td colspan='12' class='muted'>No items in this queue view.</td></tr>")
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
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Keyword</th><th>Listing</th><th>Seller Page</th><th>Seller</th><th>Ship From</th><th>Country</th><th>Evidence Source</th><th>Evidence</th><th>Status</th><th>Review</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {''.join(table_rows)}
      </tbody>
    </table>
  </div>
</main>
"""
    return html_page(APP_TITLE, body)


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
