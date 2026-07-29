#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(whoami)"
SERVICE_NAME="monitorx.service"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME"

echo "=== MonitorX Systemd Service Installation ==="
echo "User: $CURRENT_USER"
echo "Repository Path: $REPO_DIR"

if [ ! -d "$REPO_DIR/.venv" ]; then
    echo "Virtual environment not found at $REPO_DIR/.venv! Running setup.sh first..."
    bash "$REPO_DIR/setup.sh"
fi

echo "[1/3] Generating systemd unit file at $SERVICE_DEST..."
cat <<EOF | sudo tee "$SERVICE_DEST" > /dev/null
[Unit]
Description=MonitorX System Monitoring & Troubleshooting Dashboard
After=network.target libvirtd.service
Wants=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$REPO_DIR/backend
ExecStart=$REPO_DIR/.venv/bin/python main.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
Environment="HOME=$HOME"
Environment="PATH=$REPO_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

[Install]
WantedBy=multi-user.target
EOF

echo "[2/3] Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "[3/3] Enabling and starting $SERVICE_NAME..."
sudo systemctl enable --now "$SERVICE_NAME"

echo ""
echo "=== MonitorX Service Successfully Installed & Started! ==="
echo "Dashboard is accessible at: http://localhost:8080"
echo ""
echo "Service Commands:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  sudo systemctl stop $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
