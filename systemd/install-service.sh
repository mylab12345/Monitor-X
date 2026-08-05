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

# Detect the group that grants read-write access to qemu:///system, so the
# service unit can pick it up via SupplementaryGroups. This must happen before
# the unit is written.
LIBVIRT_GROUP=""
for candidate in libvirt libvirtd kvm; do
    if getent group "$candidate" > /dev/null 2>&1; then
        LIBVIRT_GROUP="$candidate"
        break
    fi
done

echo "[1/4] Generating systemd unit file at $SERVICE_DEST..."
{
cat <<EOF
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
Environment="MONITORX_LIBVIRT_URI=${MONITORX_LIBVIRT_URI:-qemu:///system}"
EOF
# Grant the service read-write libvirt access without sudo. systemd applies
# this at start time, so VM controls work on the very first boot rather than
# only after the operator logs out and back in.
if [ -n "$LIBVIRT_GROUP" ]; then
    echo "SupplementaryGroups=$LIBVIRT_GROUP"
fi
cat <<EOF

[Install]
WantedBy=multi-user.target
EOF
} | sudo tee "$SERVICE_DEST" > /dev/null

# The web process is intentionally unprivileged. Grant only the approved,
# non-interactive service actions required by the dashboard; never grant a shell.
SYSTEMCTL_BIN="$(command -v systemctl)"
SYSCTL_BIN="$(command -v sysctl)"
JOURNALCTL_BIN="$(command -v journalctl)"
DMESG_BIN="$(command -v dmesg || echo /usr/bin/dmesg)"
VIRSH_BIN="$(command -v virsh || true)"
SUDOERS_DEST="/etc/sudoers.d/monitorx-systemctl"
SUDOERS_VM_DEST="/etc/sudoers.d/monitorx-virsh"
echo "[2/4] Installing limited service-control policy at $SUDOERS_DEST..."
cat <<EOF | sudo tee "$SUDOERS_DEST" > /dev/null
# Managed by MonitorX. Required for dashboard Start/Stop/Restart controls.
Cmnd_Alias MONITORX_SYSTEMCTL = $SYSTEMCTL_BIN --no-ask-password start *.service, $SYSTEMCTL_BIN --no-ask-password stop *.service, $SYSTEMCTL_BIN --no-ask-password restart *.service, $SYSTEMCTL_BIN --no-ask-password reload *.service, $SYSTEMCTL_BIN --no-ask-password enable *.service, $SYSTEMCTL_BIN --no-ask-password disable *.service
Cmnd_Alias MONITORX_REMEDIATION = $SYSCTL_BIN -w vm.drop_caches=3, $JOURNALCTL_BIN --vacuum-time=2d, $DMESG_BIN -C, $DMESG_BIN -c, $DMESG_BIN --clear, $JOURNALCTL_BIN --rotate, $JOURNALCTL_BIN --vacuum-time=1s
$CURRENT_USER ALL=(root) NOPASSWD: MONITORX_SYSTEMCTL, MONITORX_REMEDIATION
EOF
sudo chmod 440 "$SUDOERS_DEST"
sudo visudo -cf "$SUDOERS_DEST"

# VM (libvirt) control access.
#
# Preferred path: add the dashboard user to the 'libvirt' group so MonitorX can
# open a read-write connection to qemu:///system directly. No sudo, no shelling
# out, and precise libvirt error reporting.
LIBVIRT_URI="${MONITORX_LIBVIRT_URI:-qemu:///system}"
echo "[2b/4] Configuring libvirt/KVM guest control access..."

# LIBVIRT_GROUP was detected above, before the unit file was written.
if [ -n "$LIBVIRT_GROUP" ]; then
    if id -nG "$CURRENT_USER" | tr ' ' '\n' | grep -qx "$LIBVIRT_GROUP"; then
        echo "  - $CURRENT_USER is already in the '$LIBVIRT_GROUP' group."
    else
        echo "  - Adding $CURRENT_USER to the '$LIBVIRT_GROUP' group (grants read-write libvirt access)."
        sudo usermod -aG "$LIBVIRT_GROUP" "$CURRENT_USER"
        echo "    NOTE: group membership applies to new sessions; the systemd"
        echo "    service picks it up when it is (re)started below."
    fi
else
    echo "  - No libvirt/kvm group found on this host; relying on the sudo policy below."
fi

# Fallback path: a narrowly scoped sudo policy matching the EXACT argv MonitorX
# executes. The command form must stay in sync with _virsh_command() in
# backend/main.py:
#   virsh --quiet --no-pkttyagent --connect <URI> <verb> -- <domain>
#
# Note: '--no-ask-password' (used by earlier releases) is a systemctl flag that
# virsh rejects outright, and 'poweroff' is not a virsh verb -- the forced-stop
# verb is 'destroy'. Both mistakes are corrected here.
# The ':' characters in the URI are escaped for sudoers' parser.
if [ -n "$VIRSH_BIN" ]; then
    echo "  - Installing limited VM-control sudo policy at $SUDOERS_VM_DEST..."
    VIRSH_PREFIX="$VIRSH_BIN --quiet --no-pkttyagent --connect $(printf '%s' "$LIBVIRT_URI" | sed 's/:/\\:/g')"
    {
        echo "# Managed by MonitorX. Required for dashboard Start/Shutdown/Reboot/Poweroff controls on libvirt/KVM guests."
        echo "# Must match _virsh_command() in backend/main.py."
        printf 'Cmnd_Alias MONITORX_VIRSH = '
        first=1
        for verb in start shutdown reboot destroy suspend resume; do
            [ $first -eq 1 ] || printf ', '
            printf '%s %s -- *' "$VIRSH_PREFIX" "$verb"
            first=0
        done
        printf '\n'
        echo "$CURRENT_USER ALL=(root) NOPASSWD: MONITORX_VIRSH"
    } | sudo tee "$SUDOERS_VM_DEST" > /dev/null
    sudo chmod 440 "$SUDOERS_VM_DEST"
    if ! sudo visudo -cf "$SUDOERS_VM_DEST"; then
        echo "  !! Generated sudoers policy is invalid; removing it to avoid breaking sudo."
        sudo rm -f "$SUDOERS_VM_DEST"
        exit 1
    fi
else
    echo "  - virsh not found; skipping VM-control sudo policy."
    echo "    Install it with: sudo apt-get install -y libvirt-clients"
    echo "    then re-run this installer."
fi

# NOTE: $SUDOERS_VM_DEST is rewritten in place above, which replaces the policy
# shipped by older MonitorX versions. That old policy whitelisted the invalid
# 'virsh --no-ask-password ...' form, so it granted nothing usable while still
# making the dashboard report that VM controls were authorized.

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
