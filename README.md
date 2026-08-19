# Marketplace Country Prescreener

Local FastAPI tools for prescreening/searching Marketplace listing keywords
against a workbook of compliance monitoring keywords.

## Files

- `marketplace_prescreener.py` — main FastAPI app (SQLite-backed prescreening workflow).
- `search_helper.py` — lightweight FastAPI app that turns workbook keywords into
  ready-to-open Walmart search links.
- `Consumables_Master.xlsx` — source workbook of keywords per subcategory sheet.
- `Consumables_Starter_File.rtf` — reference/starter notes.

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
