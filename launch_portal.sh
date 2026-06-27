#!/bin/bash
# Launch the PJM Data Hub Streamlit app.
# Usage: ./launch_portal.sh [port]

set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HUB_DIR="$REPO_DIR/PJM_Data_Hub"
PORT="${1:-8510}"

if [ ! -d "$HUB_DIR/.venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

echo "Starting PJM Data Hub on http://localhost:$PORT"
cd "$HUB_DIR"
.venv/bin/streamlit run app/Home.py --server.port "$PORT" --server.headless false
