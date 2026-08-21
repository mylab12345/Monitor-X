#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional local configuration. Keep secrets in .env (ignored by git); see
# .env.example. Explicit environment variables take precedence.
if [ -f "$SCRIPT_DIR/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#${line%%[![:space:]]*}}"
        [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            printf 'Ignoring invalid variable name in .env: %s\n' "$key" >&2
            continue
        fi
        # Do not overwrite an explicitly exported environment variable.
        if [[ -z "${!key+x}" ]]; then
            value="${value#\"}"; value="${value%\"}"
            export "$key=$value"
        fi
    done < "$SCRIPT_DIR/.env"
fi

export VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

echo "=== Monitoring Dashboard ==="
echo "Starting server on http://${MONITORX_HOST:-127.0.0.1}:${MONITORX_PORT:-8080}"
echo "Press Ctrl+C to stop"
echo ""
cd "$SCRIPT_DIR/backend"
exec "$VENV_PYTHON" main.py