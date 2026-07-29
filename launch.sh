#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

echo "=== Monitoring Dashboard ==="
echo "Starting server on http://localhost:8080"
echo "Press Ctrl+C to stop"
echo ""
cd "$SCRIPT_DIR/backend"
exec "$VENV_PYTHON" main.py