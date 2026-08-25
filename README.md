# Marketplace Country Prescreener

Local FastAPI tools for prescreening/searching Marketplace listing keywords
against a workbook of compliance monitoring keywords.

## Files

- `marketplace_prescreener.py` — main FastAPI app (SQLite-backed prescreening workflow).
- `search_helper.py` — lightweight FastAPI app that turns workbook keywords into
  ready-to-open Walmart search links (US only).
- `Consumables_Master.xlsx` — source workbook of keywords per subcategory sheet.
- `Consumables_Starter_File.rtf` — reference/starter notes.

## Markets

`marketplace_prescreener.py` can point its scans at more than the US storefront.
Pick a **Market** on the home page (or pass `market=` to `/scan`) to choose which
Walmart-banner storefront to crawl:

| Code | Storefront | Notes |
|------|------------|-------|
| `US` | walmart.com | Default. Fully verified. |
| `CA` | walmart.ca | Verified. Uses an `/en/` locale prefix internally; prices tagged "CAD". |
| `MX` | walmart.com.mx | Domain not verified live yet — **confirm it resolves on your network before trusting results.** Prices tagged "MXN". |
| `CL` | lider.cl (Walmart Chile's consumer brand) | Same caveat as MX. Prices tagged "CLP". |

The **target country** dropdown (China/US signal detection) is intentionally
unchanged by market — it always screens for China vs. US ship-from language,
regardless of which storefront you're scanning.

### Known gotchas

- Some storefronts (confirmed on walmart.ca) don't render a clickable seller
  storefront link on the product page — the seller name/id still come from
  the plain "Sold by X" text and an embedded `sellerId` JSON field on the
  product page itself (see `extract_embedded_seller_id`). If a future market
  add-on shows `seller_id: Unknown` a lot, check the raw product HTML for
  where that market puts its seller JSON before assuming it's broken.
- If a market's domain doesn't resolve (DNS failure, VPN split-tunnel, etc.)
  the scan logs a per-keyword error and finishes cleanly with 0 matches —
  it does not crash or hang. Check the job's live log if a market scan comes
  back suspiciously empty.
- Adding a new market is a one-line addition to the `MARKETS` dict at the top
  of `marketplace_prescreener.py` (domain + locale prefix + currency label) —
  no other code changes needed as long as the storefront follows the same
  `/ip/<slug>/<id>` product URL shape Walmart uses everywhere else.

## Setup

This project uses [`uv`](https://github.com/astral-sh/uv) for environment/dependency
management.

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt \
  --index-url https://pypi.ci.artifacts.walmart.com/artifactory/api/pypi/external-pypi/simple \
  --allow-insecure-host pypi.ci.artifacts.walmart.com
```

## Running

```bash
# Search helper (keyword -> Walmart search links)
python search_helper.py
# then open http://127.0.0.1:8000

# Main prescreener app
python marketplace_prescreener.py
```

## Contributing

Standard workflow:

```bash
git checkout -b my-feature
# make changes
git add -A
git commit -m "Describe your change"
git push origin my-feature
# open a PR on GitHub
```
