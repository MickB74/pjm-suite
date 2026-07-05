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

# Open browser after a short delay so Streamlit has time to start
(sleep 3 && open "http://localhost:$PORT") &

PJM_Data_Hub/.venv/bin/streamlit run PJM_Data_Hub/app/Home.py \
    --server.port "$PORT" \
    --server.headless false
