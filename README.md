# MonitorX - System Monitoring & Troubleshooting Dashboard

Real-time, modern monitoring and automated troubleshooting dashboard for Linux servers (CPU, RAM, Storage, Network, GPU, VMs, Systemd Services, and Logs).

![MonitorX UI](https://img.shields.shields.gradient)

## Features

- **Ultra-Modern Glassmorphism UI** — High-tech dark slate interface with neon accent glows, live sparklines, and dark/light mode toggle.
- **Real-Time Canvas Sparklines** — Live 30s trend graphs for CPU, Memory, and Network Bandwidth.
- **Troubleshoot Mode Hub** — Automated 7-point diagnostic scan calculating a **System Health Index (0-100)** score with **1-Click Remediation Fixes**.
- **Live Log Inspector** — Auto-tail streaming for system logs and `dmesg` with level filtering (`ALL`, `🔴 ERROR`, `⚠️ WARN`, `ℹ️ INFO`) and regex search.
- **Network Diagnostic Suite** — Interactive ICMP Ping latency tester, TCP Port connectivity checker, DNS resolver benchmark, and active listening ports table.
- **Resource Bottleneck Finder** — Identifies top CPU, RAM, and thread consumers alongside stuck/zombie process killers.
- **Safe Diagnostic Console** — Approved read-only diagnostic presets (`df -h`, `free -h`, `ss -tulpn`, `systemctl --failed`, `dmesg -T`, `uname -a`) with command history. Arbitrary shell execution is intentionally disabled.
- **Process Manager** — Interactive process table with sorting, multi-select kill, and full process detail inspector (cmdline, open file handles, socket connections).
- **Service & VM Management** — Start, stop, restart, and reload systemd services, with an in-UI authorization status and actionable errors. Running libvirt/KVM guests show live vCPU, RAM, disk I/O, and network throughput metrics.
- **KVM Guest Lifecycle Controls** — `Start`, `Shutdown`, `Reboot`, `Suspend`, `Resume`, and a destructive `Poweroff` for every libvirt domain discovered on the host, with a state-aware action matrix, bulk operations on multiple guests, KPI counters (Total / Running / Stopped / Paused / Other), search/filter/sort, an auto-refresh interval selector, and a built-in audit log of the last 50 control actions.
- **No Authentication Required** — Plug-and-play local/internal server monitoring.

---

## Quick Start

```bash
# 1. Setup Python Virtual Environment and dependencies
./setup.sh

# 2. Launch dashboard directly
./launch.sh
```

Open **http://localhost:8080** in your browser.

---

## Systemd Service Setup (Run on Boot)

To run MonitorX as a background systemd service that starts automatically on boot:

```bash
# Run installer script (creates and enables /etc/systemd/system/monitorx.service)
./systemd/install-service.sh
```

### Dashboard service controls

A dashboard process runs as your regular user, so plain `systemctl restart …` is normally rejected by systemd/polkit. The installer now creates a **limited passwordless sudo policy** only for MonitorX’s `start`, `stop`, `restart`, `reload`, `enable`, and `disable` actions on `.service` units, plus the two explicit remediation commands used by the Troubleshoot Hub (drop page cache and vacuum journals). It does **not** grant shell access.

When `virsh` is detected, the installer additionally writes a separate `/etc/sudoers.d/monitorx-virsh` policy granting the dashboard’s user non-interactive `virsh --no-ask-password` access for `start`, `shutdown`, `reboot`, `poweroff`, `destroy`, `suspend`, and `resume` actions. The **VMs (Libvirt)** tab shows whether this policy is available, disables controls if it is not, and surfaces the exact `virsh` error rather than reporting a false success.

After updating an existing installation, run the installer once more and reload MonitorX:

```bash
./systemd/install-service.sh
# or, if MonitorX is already installed:
sudo systemctl restart monitorx
```

The **Systemd Services** tab shows whether this policy is available, disables controls if it is not, and displays the exact API error rather than reporting a false success.

### Useful Service Commands

```bash
sudo systemctl status monitorx
sudo systemctl restart monitorx
sudo systemctl stop monitorx
journalctl -u monitorx -f
```

---

## Workspace Structure

```
MonitorX/
├── backend/
│   ├── main.py               # FastAPI application with REST & WebSocket APIs
│   └── requirements.txt      # Python dependencies
├── frontend/
│   ├── index.html            # Dashboard HTML structure
│   ├── css/styles.css        # Modern Glassmorphic CSS Theme
│   └── js/app.js             # Real-time WebSocket logic & Canvas charts
├── systemd/
│   ├── monitorx.service      # Systemd service unit template
│   └── install-service.sh    # Automated service installer script
├── launch.sh                 # Launcher script
├── setup.sh                  # Setup script
└── README.md                 # Project documentation
```

---

## Suggested Future Improvements

1. **Persistent Time-Series Storage**:
   - Connect an embedded SQLite, TimescaleDB, or Prometheus exporter to store historical metrics over days/weeks for long-term trend analysis.
2. **Automated Notification Webhooks**:
   - Send instant alerts (Slack, Discord, Telegram, or Email) when the System Health Index drops below a threshold (e.g. `< 70`).
3. **Docker & Container Monitoring**:
   - Add a dedicated Container tab integrating with the Docker daemon API to monitor container CPU/RAM usage, status, and logs.
4. **Custom Dashboard Widgets**:
   - Allow users to pin, drag, and resize metric cards or custom command outputs to create personalized monitoring views.
5. **Service safety workflow**:
   - Add maintenance windows, dependency previews, per-service log tails, and an audit timeline recording who invoked each action and its result.
6. **Role-based access and authentication**:
   - Protect control endpoints with local accounts/SSO and granular roles (view, diagnose, operate) before exposing the dashboard beyond a trusted network.
7. **Alert rules UI**:
   - Let operators create threshold rules with cooldowns, acknowledgement, silencing, and notification routing directly from the dashboard.
8. **Fleet view**:
   - Securely aggregate multiple MonitorX agents into a host inventory with tags, health rollups, and drill-down diagnostics.
