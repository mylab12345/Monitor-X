#!/bin/bash
set -e

echo "=== Monitoring Dashboard Setup ==="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo "[1/5] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv libvirt-dev libvirt-daemon-system libvirt-clients qemu-system-x86 2>/dev/null || true

echo "[2/5] Installing Python system packages for GPU/VM monitoring..."
sudo apt-get install -y -qq python3-libvirt 2>/dev/null || true

echo "[3/5] Creating virtual environment..."
python3 -m venv "$SCRIPT_DIR/.venv"
source "$SCRIPT_DIR/.venv/bin/activate"

echo "[4/5] Installing Python packages..."
pip install --upgrade pip -q
pip install fastapi uvicorn websockets psutil jinja2 aiofiles py3nvml --break-system-packages -q

echo "[5/5] Linking libvirt into virtual environment..."
VENVSITE="$SCRIPT_DIR/.venv/lib/python3.12/site-packages"
for f in /usr/lib/python3/dist-packages/libvirt.py /usr/lib/python3/dist-packages/libvirt_lxc.py /usr/lib/python3/dist-packages/libvirtaio.py /usr/lib/python3/dist-packages/libvirtmod*.so; do
    [ -f "$f" ] && sudo ln -sf "$f" "$VENVSITE/" 2>/dev/null || true
done

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "To start the dashboard:"
echo "  ./launch.sh"
echo ""
echo "Or use systemd (requires root for libvirt access):"
echo "  sudo cp systemd/monitoring-dashboard.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now monitoring-dashboard"
echo ""
echo "Dashboard: http://localhost:8080"