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

VENV_PYTHON="$REPO_DIR/.venv/bin/python3"
venv_usable() {
    # A dangling symlink fails -x/-e, so verify the interpreter by running it.
    "$VENV_PYTHON" -c 'import sys' >/dev/null 2>&1
}

# The venv must be present AND its interpreter must actually run. A missing or
# dangling interpreter makes systemd fail with "status=203/EXEC" on every
# restart attempt, so rebuild the venv here instead of generating a unit that
# can never start.
if [ ! -d "$REPO_DIR/.venv" ] || ! venv_usable; then
    echo "Virtual environment missing or broken at $REPO_DIR/.venv! Running setup.sh first..."
    bash "$REPO_DIR/setup.sh"
fi

if ! venv_usable; then
    echo "ERROR: $VENV_PYTHON is still not usable after running setup.sh." >&2
    echo "       Inspect it with:" >&2
    echo "         ls -la $REPO_DIR/.venv/bin/" >&2
    echo "         readlink -f $VENV_PYTHON" >&2
    exit 1
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

# Keep optional deployment secrets out of the generated unit. If the installer
# is invoked with MONITORX_AUTH_TOKEN, persist it in a root-readable env file;
# the unit references it with EnvironmentFile= above. Existing files are kept
# when the token is not present so upgrades do not silently disable auth.
AUTH_ENV_DEST="/etc/monitorx.env"
if [ -n "${MONITORX_AUTH_TOKEN:-}" ]; then
    printf 'MONITORX_AUTH_TOKEN=%q\n' "$MONITORX_AUTH_TOKEN" | sudo tee "$AUTH_ENV_DEST" > /dev/null
    sudo chmod 600 "$AUTH_ENV_DEST"
fi

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
ExecStart=$REPO_DIR/.venv/bin/python3 $REPO_DIR/backend/main.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
Environment="HOME=$HOME"
Environment="PATH=$REPO_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=-/etc/monitorx.env
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
# Additional maintenance tools used by the Auto-Fix Engine: SSD TRIM and safe
# swap reclaim. fstrim/swapoff/swapon are argv-constrained; the maintenance
# alias is only emitted for tools that exist on this host.
FSTRIM_BIN="$(command -v fstrim || true)"
SWAPOFF_BIN="$(command -v swapoff || true)"
SWAPON_BIN="$(command -v swapon || true)"
MAINT_CMDS=""
if [ -n "$FSTRIM_BIN" ]; then MAINT_CMDS="${MAINT_CMDS:+$MAINT_CMDS, }$FSTRIM_BIN -av"; fi
if [ -n "$SWAPOFF_BIN" ]; then MAINT_CMDS="${MAINT_CMDS:+$MAINT_CMDS, }$SWAPOFF_BIN -a"; fi
if [ -n "$SWAPON_BIN" ]; then MAINT_CMDS="${MAINT_CMDS:+$MAINT_CMDS, }$SWAPON_BIN -a"; fi

SUDOERS_BODY="Cmnd_Alias MONITORX_SYSTEMCTL = $SYSTEMCTL_BIN --no-ask-password start *.service, $SYSTEMCTL_BIN --no-ask-password stop *.service, $SYSTEMCTL_BIN --no-ask-password restart *.service, $SYSTEMCTL_BIN --no-ask-password reload *.service, $SYSTEMCTL_BIN --no-ask-password enable *.service, $SYSTEMCTL_BIN --no-ask-password disable *.service
Cmnd_Alias MONITORX_REMEDIATION = $SYSCTL_BIN -w vm.drop_caches=3, $JOURNALCTL_BIN --vacuum-time=2d, $DMESG_BIN -C, $DMESG_BIN -c, $DMESG_BIN --clear, $JOURNALCTL_BIN --rotate, $JOURNALCTL_BIN --vacuum-time=1s
"
SUDOERS_BODY="${SUDOERS_BODY}Cmnd_Alias MONITORX_SYSCTL = $SYSCTL_BIN -w vm.swappiness=*, $SYSCTL_BIN -w fs.file-max=*
"
if [ -n "$MAINT_CMDS" ]; then
  SUDOERS_BODY="${SUDOERS_BODY}Cmnd_Alias MONITORX_MAINTENANCE = $MAINT_CMDS
"
fi
SUDOERS_BODY="${SUDOERS_BODY}$CURRENT_USER ALL=(root) NOPASSWD: MONITORX_SYSTEMCTL, MONITORX_REMEDIATION, MONITORX_SYSCTL"
if [ -n "$MAINT_CMDS" ]; then
  SUDOERS_BODY="${SUDOERS_BODY}, MONITORX_MAINTENANCE"
fi
SUDOERS_BODY="${SUDOERS_BODY}
"
printf '%s' "$SUDOERS_BODY" | sudo tee "$SUDOERS_DEST" > /dev/null
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
# executes. The command forms must stay in sync with backend/main.py:
#   virsh --quiet [--no-pkttyagent] --connect <URI> <verb> -- <domain>
#     (lifecycle verbs: start shutdown reboot destroy suspend resume)
#   virsh --quiet [--no-pkttyagent] --connect <URI> console -- <domain>
#     (serial console)
#   virsh --quiet [--no-pkttyagent] --connect <URI> setvcpus <domain> ...
#   virsh --quiet [--no-pkttyagent] --connect <URI> setmem <domain> ...
#   virsh --quiet [--no-pkttyagent] --connect <URI> setmaxmem <domain> ...
#     (CPU/RAM resize fallback)
# --no-pkttyagent is present only on libvirt ≥11.4; older hosts reject it,
# so both variants are whitelisted for compatibility.
#
# Note: '--no-ask-password' (used by earlier releases) is a systemctl flag that
# virsh rejects outright, and 'poweroff' is not a virsh verb -- the forced-stop
# verb is 'destroy'. Both mistakes are corrected here.
# The ':' characters in the URI are escaped for sudoers' parser.
if [ -n "$VIRSH_BIN" ]; then
    echo "  - Installing limited VM-control sudo policy at $SUDOERS_VM_DEST..."
    # --no-pkttyagent was added in libvirt 11.4 (2025). Hosts with older virsh
    # reject it as "unsupported option". The dashboard now transparently retries
    # without the flag, so the sudoers policy must whitelist BOTH forms.
    VIRSH_ESCAPED_URI="$(printf '%s' "$LIBVIRT_URI" | sed 's/:/\\:/g')"
    VIRSH_PREFIX="$VIRSH_BIN --quiet --connect $VIRSH_ESCAPED_URI"
    VIRSH_PREFIX_PKT="$VIRSH_BIN --quiet --no-pkttyagent --connect $VIRSH_ESCAPED_URI"
    {
        echo "# Managed by MonitorX. Required for dashboard lifecycle, console, and resize controls on libvirt/KVM guests."
        echo "# Must match _virsh_command()/_build_virsh_modify_command() in backend/main.py."
        echo "# Both --no-pkttyagent and legacy forms are allowed for compat with older libvirt."
        printf 'Cmnd_Alias MONITORX_VIRSH = '
        first=1
        for prefix in "$VIRSH_PREFIX" "$VIRSH_PREFIX_PKT"; do
            for verb in start shutdown reboot destroy suspend resume; do
                [ $first -eq 1 ] || printf ', '
                printf '%s %s -- *' "$prefix" "$verb"
                first=0
            done
            # Serial console (virsh console) and CPU/RAM resize fallbacks.
            printf ', %s console -- *' "$prefix"
            printf ', %s setvcpus *' "$prefix"
            printf ', %s setmem *' "$prefix"
            printf ', %s setmaxmem *' "$prefix"
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
