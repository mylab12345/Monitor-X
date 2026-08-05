# MonitorX - System Monitoring & Troubleshooting Dashboard

Real-time, modern monitoring and automated troubleshooting dashboard for Linux servers (CPU, RAM, Storage, Network, GPU, VMs, Systemd Services, and Logs).

![MonitorX UI](https://img.shields.shields.gradient)

## Features

- **Ultra-Modern Glassmorphism UI** — High-tech dark slate interface with neon accent glows, live sparklines, and dark/light mode toggle.
- **NASA Mission-Control Flight Loop** — A live GO/NO-GO poll board on the Dashboard: nine flight-controller stations (BOOSTER/storage, GUIDO/CPU, TELMU/memory, INCO/network, EECOM/thermal, FIDO/processes, SURGEON/GPU, GC/services, CAPCOM/datalink) each vote GO / CAUTION / NO-GO from real telemetry. The worst vote drives the mission status lamp, and any NO-GO raises a blinking **MASTER ALARM**. Hardware-less stations degrade to STANDBY. Header carries MET, UTC, and DOY (day-of-year) mission clocks, plus a flight-control boot sequence and live telemetry ticker.
- **Real-Time Canvas Sparklines** — Live 30s trend graphs for CPU, Memory, and Network Bandwidth.
- **Troubleshoot Mode Hub** — Automated diagnostic scan (11 check categories) calculating a **System Health Index (0-100)** score with **1-Click Remediation Fixes**.
- **Auto-Fix Engine** — Every issue the scan detects gets a fix button *inside* the hub, plus a **⚡ Fix All Issues** batch runner and a **Review Fix Plan** modal to pick exactly which repairs to apply. Fixes include: clear RAM page cache, vacuum systemd journal, restart failed or individual services, clear the kernel error buffer, kill runaway processes (owner-guarded), reap zombie processes via SIGCHLD, flush the DNS resolver cache, clean stale temp files, and restart exited Docker containers. Every execution is timed, audited, and shown in the **Remediation History** panel — the hub re-scans automatically after each fix and reports which issues it resolved.
- **Live Log Inspector** — Auto-tail streaming for system logs and `dmesg` with level filtering (`ALL`, `🔴 ERROR`, `⚠️ WARN`, `ℹ️ INFO`) and regex search.
- **Network Diagnostic Suite** — Interactive ICMP Ping latency tester, TCP Port connectivity checker, DNS resolver benchmark, and active listening ports table.
- **Resource Bottleneck Finder** — Identifies top CPU, RAM, and thread consumers alongside stuck/zombie process killers.
- **Safe Diagnostic Console** — Approved read-only diagnostic presets (`df -h`, `free -h`, `ss -tulpn`, `systemctl --failed`, `dmesg -T`, `uname -a`) with command history. Arbitrary shell execution is intentionally disabled.
- **Process Manager** — Interactive process table with sorting, multi-select kill, and full process detail inspector (cmdline, open file handles, socket connections). Multi-select kill runs as a single batched request and is **owner-guarded per process**: PIDs owned by another user are individually refused with the reason shown in the summary toast, while authorized PIDs are still terminated — the bulk kill never aborts wholesale on the first unauthorized target.
- **Service & VM Management** — Start, stop, restart, and reload systemd services, with an in-UI authorization status and actionable errors. Running libvirt/KVM guests show live vCPU, RAM, disk I/O, and network throughput metrics.
- **KVM Guest Lifecycle Controls** — `Start`, `Shutdown`, `Reboot`, `Suspend`, `Resume`, and a destructive `Poweroff` for every libvirt domain discovered on the host, with a state-aware action matrix, bulk operations on multiple guests, KPI counters (Total / Running / Stopped / Paused / Other), search/filter/sort, an auto-refresh interval selector, and a built-in audit log of the last 50 control actions. Controls run through the libvirt API when MonitorX has read-write access and transparently fall back to a narrowly scoped `sudo virsh` policy otherwise; a dropped `libvirtd` connection is re-established automatically without restarting the dashboard.
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

A dashboard process runs as your regular user, so plain `systemctl restart …` is normally rejected by systemd/polkit. The installer now creates a **limited passwordless sudo policy** only for MonitorX’s `start`, `stop`, `restart`, `reload`, `enable`, and `disable` actions on `.service` units, plus the explicit remediation commands used by the Troubleshoot Hub (drop page cache, vacuum journals, and clear kernel buffer/logs). It does **not** grant shell access.

### VM (libvirt/KVM) control authorization

VM lifecycle controls use two independent paths, and the dashboard enables the buttons when **either** works:

1. **Native libvirt (preferred).** The installer adds MonitorX’s user to the host’s `libvirt` (or `kvm`) group and sets `SupplementaryGroups=` in the unit file, so the backend opens a **read-write** connection to `qemu:///system` and drives guests through the libvirt API directly — no sudo, no subprocess, and precise error messages.
2. **`sudo virsh` fallback.** When a read-write connection is unavailable, the installer’s `/etc/sudoers.d/monitorx-virsh` policy permits exactly the argv MonitorX runs:

   ```
   virsh --quiet --no-pkttyagent --connect qemu:///system <verb> -- <domain>
   ```

   for the lifecycle verbs `start`, `shutdown`, `reboot`, `destroy`, `suspend`, and `resume` — plus the serial-console form `… console -- <domain>` and the CPU/RAM resize forms `… setvcpus <domain> …`, `… setmem <domain> …`, `… setmaxmem <domain> …`. Nothing else is granted — no shell, no `undefine`, no arbitrary arguments. The `--no-pkttyagent` flag is part of the whitelisted command line and must stay in sync with `_virsh_command()` in `backend/main.py`; sudo matches the full argv, so a mismatch makes every control command fail with “not allowed to execute”.

The **VMs (Libvirt)** tab reports which mode is active, disables controls only when both paths fail, and surfaces the exact libvirt/`virsh` error instead of reporting a false success.

> **Upgrading from an earlier version?** Releases before this fix generated a policy for `virsh --no-ask-password …`. That is a **systemctl** flag which `virsh` rejects outright, so every Start/Shutdown/Reboot silently failed — and `poweroff` isn’t a `virsh` verb at all (the correct one is `destroy`). A later release also shipped a runtime command line that omitted `--no-pkttyagent`, which made sudo reject the exact same commands. Re-run `./systemd/install-service.sh` to replace the stale policy, then restart MonitorX.

After updating an existing installation, run the installer once more and reload MonitorX:

```bash
./systemd/install-service.sh
# or, if MonitorX is already installed:
sudo systemctl restart monitorx
```

The **Systemd Services** tab shows whether this policy is available, disables controls if it is not, and displays the exact API error rather than reporting a false success. Availability is verified by asking sudo to validate the exact `systemctl --no-ask-password <action> <unit>` argv MonitorX runs — not merely whether the account holds *some* sudo privilege — so the tab only reports "available" when the granted commands actually match.

### Useful Service Commands

```bash
sudo systemctl status monitorx
sudo systemctl restart monitorx
sudo systemctl stop monitorx
journalctl -u monitorx -f
```

### Troubleshooting: `status=203/EXEC`

If the service keeps restarting and `systemctl status monitorx` shows `code=exited, status=203/EXEC`, systemd could not **execute** the interpreter named in `ExecStart` — it is not a bug in `main.py`. The usual cause is a missing or broken virtual environment: `setup.sh` may never have been run, or the `.venv` was copied from another machine/path or its base Python was upgraded/removed, leaving `bin/python3` as a dangling symlink.

Diagnose, then repair:

```bash
# 1. Confirm which command fails and why (look for "No such file or directory"
#    or "Permission denied" in the journal).
systemctl cat monitorx
journalctl -u monitorx -n 20 --no-pager

# 2. Inspect the interpreter the unit references.
ls -la /home/<user>/Monitor-X/.venv/bin/python3
readlink -f /home/<user>/Monitor-X/.venv/bin/python3   # points at nothing? => broken venv

# 3. Repair: rebuild the venv, then regenerate and restart the service.
#    (setup.sh now detects a dangling/broken interpreter and rebuilds it;
#    install-service.sh now verifies the venv actually works before installing.)
cd /home/<user>/Monitor-X
./setup.sh
./systemd/install-service.sh
sudo systemctl daemon-reload
sudo systemctl restart monitorx
sudo systemctl status monitorx --no-pager
```

The installer now also generates `ExecStart` with an absolute path to `backend/main.py`, so the unit keeps working even if the service is started from a different working directory.

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
   - Send instant alerts (Slack, Discord, Telegram, or Email) when the System Health Index drops below a threshold (e.g. `< 70`). *Partially done: the Operations Center already ships alert rules + webhooks.*
3. **Docker & Container Monitoring**:
   - Add a dedicated Container tab integrating with the Docker daemon API to monitor container CPU/RAM usage, status, and logs. *Partially done: exited/crashed containers are detected by the health scan and can be restarted from the Auto-Fix Engine.*
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
