#!/bin/bash
# Double-click to launch the PJM Data Hub and open it in your browser.
cd "$(dirname "$0")" || exit 1

PORT=8510

if [ ! -d "PJM_Data_Hub/.venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# Open browser after a short delay so Streamlit has time to start
(sleep 3 && open "http://localhost:$PORT") &

PJM_Data_Hub/.venv/bin/streamlit run PJM_Data_Hub/app/Home.py \
    --server.port "$PORT" \
    --server.headless false
