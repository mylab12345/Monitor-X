#!/usr/bin/env bash
# Install MonitorX dependencies for Debian/Ubuntu hosts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

printf '%s\n' '=== MonitorX Setup ==='
echo '[1/4] Installing optional system packages for libvirt and VM monitoring...'
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv libvirt-dev libvirt-daemon-system libvirt-clients qemu-system-x86 python3-libvirt || true

echo '[2/4] Creating/updating the virtual environment...'
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip -q

echo '[3/4] Installing pinned Python dependencies...'
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt" -q

echo '[4/4] Making the optional system libvirt bindings visible to the environment...'
VENV_SITE="$($VENV_DIR/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for file in /usr/lib/python3/dist-packages/libvirt.py /usr/lib/python3/dist-packages/libvirt_lxc.py /usr/lib/python3/dist-packages/libvirtaio.py /usr/lib/python3/dist-packages/libvirtmod*.so; do
    [[ -f "$file" ]] && ln -sf "$file" "$VENV_SITE/" 2>/dev/null || true
done

cat <<EOF2

=== Setup complete ===
Start MonitorX: ./launch.sh
Install the systemd service (including dashboard service-control policy):
  ./systemd/install-service.sh
Dashboard: http://localhost:8080
EOF2
