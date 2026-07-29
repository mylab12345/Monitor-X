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

echo "[1/4] Generating systemd unit file at $SERVICE_DEST..."
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

# The web process is intentionally unprivileged. Grant only the approved,
# non-interactive service actions required by the dashboard; never grant a shell.
SYSTEMCTL_BIN="$(command -v systemctl)"
SYSCTL_BIN="$(command -v sysctl)"
JOURNALCTL_BIN="$(command -v journalctl)"
VIRSH_BIN="$(command -v virsh || true)"
SUDOERS_DEST="/etc/sudoers.d/monitorx-systemctl"
SUDOERS_VM_DEST="/etc/sudoers.d/monitorx-virsh"
echo "[2/4] Installing limited service-control policy at $SUDOERS_DEST..."
cat <<EOF | sudo tee "$SUDOERS_DEST" > /dev/null
# Managed by MonitorX. Required for dashboard Start/Stop/Restart controls.
Cmnd_Alias MONITORX_SYSTEMCTL = $SYSTEMCTL_BIN --no-ask-password start *.service, $SYSTEMCTL_BIN --no-ask-password stop *.service, $SYSTEMCTL_BIN --no-ask-password restart *.service, $SYSTEMCTL_BIN --no-ask-password reload *.service, $SYSTEMCTL_BIN --no-ask-password enable *.service, $SYSTEMCTL_BIN --no-ask-password disable *.service
Cmnd_Alias MONITORX_REMEDIATION = $SYSCTL_BIN -w vm.drop_caches=3, $JOURNALCTL_BIN --vacuum-time=2d
$CURRENT_USER ALL=(root) NOPASSWD: MONITORX_SYSTEMCTL, MONITORX_REMEDIATION
EOF
sudo chmod 440 "$SUDOERS_DEST"
sudo visudo -cf "$SUDOERS_DEST"

# Optional: VM (libvirt) control policy. Only installed when virsh is present
# so the policy is never broader than what the dashboard needs.
if [ -n "$VIRSH_BIN" ]; then
    echo "[2b/4] Installing limited VM-control policy at $SUDOERS_VM_DEST..."
    cat <<EOF | sudo tee "$SUDOERS_VM_DEST" > /dev/null
# Managed by MonitorX. Required for dashboard Start/Stop/Reboot/Poweroff controls on libvirt/KVM guests.
Cmnd_Alias MONITORX_VIRSH = $VIRSH_BIN --no-ask-password start *, $VIRSH_BIN --no-ask-password shutdown *, $VIRSH_BIN --no-ask-password reboot *, $VIRSH_BIN --no-ask-password poweroff *, $VIRSH_BIN --no-ask-password destroy *, $VIRSH_BIN --no-ask-password suspend *, $VIRSH_BIN --no-ask-password resume *
$CURRENT_USER ALL=(root) NOPASSWD: MONITORX_VIRSH
EOF
    sudo chmod 440 "$SUDOERS_VM_DEST"
    sudo visudo -cf "$SUDOERS_VM_DEST"
else
    echo "[2b/4] virsh not found; skipping VM-control sudo policy (re-run installer after installing libvirt-client)."
fi

echo "[3/4] Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "[4/4] Enabling and starting $SERVICE_NAME..."
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
