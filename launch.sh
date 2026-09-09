#!/usr/bin/env bash
# Launches the Marketplace Country Prescreener and opens it in your browser.
# Safe to run from anywhere (Terminal, double-click via the .command wrapper, etc).

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT=8001
URL="http://127.0.0.1:${PORT}"

# Make sure the venv exists before we try to use it.
if [ ! -d ".venv" ]; then
    echo "No .venv found in $DIR -- creating one and installing requirements..."
    python3 -m venv .venv
    "$DIR/.venv/bin/pip" install -r requirements.txt
fi

open_browser() {
    if command -v open >/dev/null 2>&1; then
        open "$URL"          # macOS
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL"      # Linux
    elif command -v start >/dev/null 2>&1; then
        start "$URL"         # Windows (git-bash)
    fi
}

# If something's already listening on the port, just open the browser and bail.
if lsof -i ":${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Prescreener already running at $URL -- opening browser."
    open_browser
    exit 0
fi

echo "Starting Marketplace Country Prescreener at $URL ..."
echo "(Close this window / press Ctrl+C to stop the server.)"

# Open the browser shortly after the server has had time to bind.
( sleep 1.5 && open_browser ) &

exec "$DIR/.venv/bin/python" marketplace_prescreener.py
