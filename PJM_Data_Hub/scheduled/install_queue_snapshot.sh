#!/usr/bin/env bash
# Install (or reinstall) the daily PJM interconnection-queue snapshot as a
# macOS launchd agent. Runs orchestrate.py update queue at 06:30 local time
# every day so the queue's change-over-time history accrues even when the
# Streamlit app isn't opened.
#
#   ./scheduled/install_queue_snapshot.sh          # install / reload
#   ./scheduled/install_queue_snapshot.sh remove   # uninstall
set -euo pipefail

LABEL="com.pjmhub.queue-snapshot"
SRC="$(cd "$(dirname "$0")" && pwd)/${LABEL}.plist"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

if [[ "${1:-}" == "remove" ]]; then
    launchctl unload "$DEST" 2>/dev/null || true
    rm -f "$DEST"
    echo "Removed ${LABEL}."
    exit 0
fi

mkdir -p "${HOME}/Library/LaunchAgents"
cp "$SRC" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "Installed ${LABEL} → daily 06:30. It runs:"
echo "  orchestrate.py update queue"
echo "Logs: PJM_Data_Hub/logs/queue_snapshot.log"
echo "Trigger a test run now with:  launchctl start ${LABEL}"
