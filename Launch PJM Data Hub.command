#!/bin/bash
# Double-click to launch the PJM Data Hub and open it in your browser.
cd "$(dirname "$0")" || exit 1

PORT=8510

if [ ! -d "PJM_Data_Hub/.venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# Free the port if a previous (possibly stuck) server is still holding it.
STUCK_PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null)
if [ -n "$STUCK_PIDS" ]; then
    echo "Port $PORT is in use by PID(s): $STUCK_PIDS — stopping them..."
    echo "$STUCK_PIDS" | xargs kill -9 2>/dev/null
    sleep 1
fi

# Refresh the NYMEX Henry Hub gas strip from Yahoo (this runs on your machine's
# residential IP, so Yahoo is reachable here even though a cloud host it isn't).
# Best-effort and never blocks launch; the app reads the cached CSV it writes.
# Hard-capped at 30s: macOS has no GNU `timeout`, so we run the refresh in the
# background and kill it if it stalls (e.g. Yahoo rate-limiting), then proceed.
echo "Refreshing NYMEX gas strip …"
( cd PJM_Data_Hub && .venv/bin/python -m pjm_core.gas_strip ) &
GAS_PID=$!
( sleep 30; kill -9 "$GAS_PID" 2>/dev/null ) &
GAS_WATCHER=$!
if wait "$GAS_PID" 2>/dev/null; then
    kill -9 "$GAS_WATCHER" 2>/dev/null
else
    kill -9 "$GAS_WATCHER" 2>/dev/null
    echo "  (gas strip refresh skipped/timed out — app will fall back to EIA STEO)"
fi

# Open browser after a short delay so Streamlit has time to start
(sleep 3 && open "http://localhost:$PORT") &

PJM_Data_Hub/.venv/bin/streamlit run PJM_Data_Hub/app/Home.py \
    --server.port "$PORT" \
    --server.headless false
