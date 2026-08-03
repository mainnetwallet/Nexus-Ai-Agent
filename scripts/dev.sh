#!/usr/bin/env bash
#
# Nexus-Agent -- run backend + frontend with a single command (Linux/Mac).
#
# Usage (from the repo root):
#   chmod +x scripts/dev.sh   # first time only
#   ./scripts/dev.sh
#
# What it does:
#   1. Starts the FastAPI backend (uvicorn) in the background.
#   2. Starts the Vite frontend dev server in the foreground.
#   3. On Ctrl+C (or any exit), stops the backend too, so you're never left
#      with an orphaned backend process running in the background.
#
# Assumes you've already completed the one-time setup from the README (a
# .venv with requirements.txt installed, and `npm install` run inside
# frontend/). This script only launches the two dev servers -- it does not
# install anything.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

mkdir -p logs

echo "Starting backend (uvicorn) in the background (log: logs/backend.log)..."
python -m uvicorn backend.main:app --reload > logs/backend.log 2>&1 &
BACKEND_PID=$!

cleanup() {
    echo ""
    echo "Stopping backend (pid $BACKEND_PID)..."
    kill "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting frontend (Vite) in the foreground -- Ctrl+C stops both..."
cd "$REPO_ROOT/frontend"
npm run dev 2>&1 | sed -u 's/^/[frontend] /'
