#!/bin/bash
# PJM Suite — one-command setup
# Usage:
#   ./setup.sh             build venv + scaffold config + pull DOM Hub prices
#   ./setup.sh --no-data   build venv only, skip the data pull

set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HUB_DIR="$REPO_DIR/PJM_Data_Hub"

echo "=== PJM Suite Setup ==="

# 1. Build virtual environment
cd "$HUB_DIR"
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment …"
    python3 -m venv .venv
fi
echo "Installing requirements …"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "  ✓ Dependencies installed"

# 2. Scaffold config
if [ ! -f "config.json" ]; then
    cp config.example.json config.json
    echo "  ✓ config.json created from example — add your PJM subscription key"
else
    echo "  ✓ config.json already exists"
fi

# 3. Create data directories
.venv/bin/python -c "import sys; sys.path.insert(0,'$HUB_DIR'); from pjm_core import paths; paths.ensure_dirs()"
echo "  ✓ Data directories created"

if [[ "$1" == "--no-data" ]]; then
    echo ""
    echo "=== Setup complete (--no-data: skipped data pull) ==="
    echo "Add your PJM subscription key to PJM_Data_Hub/config.json, then run:"
    echo "  ./launch_portal.sh"
    exit 0
fi

# 4. Check for credentials before pulling data
KEY=$(python3 -c "import json; d=json.load(open('$HUB_DIR/config.json')); print(d.get('subscription_key',''))" 2>/dev/null || echo "")
if [ -z "$KEY" ] || [ "$KEY" = "YOUR_PJM_DATAMINER_SUBSCRIPTION_KEY" ]; then
    echo ""
    echo "  ⚠  No PJM subscription key found."
    echo "     Add your key to PJM_Data_Hub/config.json, then run:"
    echo "     cd PJM_Data_Hub && .venv/bin/python orchestrate.py update hub_prices"
    echo ""
    echo "=== Setup complete (data pull skipped — no credentials) ==="
    exit 0
fi

# 5. Pull DOM Hub prices
echo "Pulling PJM DOM Hub prices …"
cd "$HUB_DIR"
.venv/bin/python orchestrate.py update hub_prices
echo "  ✓ Hub prices updated"

echo ""
echo "=== Setup complete ==="
echo "Launch the app:  ./launch_portal.sh"
