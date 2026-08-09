> **Current implementation note (2026-08-06):** The historical Docker/container and Kubernetes/kubectl integration notes in this memory file are retained for provenance only. Those CLI integrations, endpoints, UI panels, and remediation actions have been removed from the current MonitorX implementation.

# MonitorX - AI Agent Memory File

> **Purpose**: This file gives any AI agent a fast, complete understanding of the MonitorX codebase without scanning every file. Read this FIRST before any task.

---

## Quick Facts

| Fact | Value |
|------|-------|
| **What** | Real-time Linux server monitoring dashboard (self-hosted, no auth) |
| **Version** | v2.5 (NASA Mission-Control theme; GO/NO-GO Flight Control Loop removed) |
| **Port** | 8080 (`0.0.0.0`) |
| **Backend** | Python 3.12 + FastAPI + Uvicorn + WebSockets |
| **Frontend** | Vanilla JS (no framework) + CSS Glassmorphism + HTML5 Canvas sparklines |
| **Database** | None (all ephemeral, computed on-the-fly) |
| **Auth** | None (designed for trusted internal networks) |
| **Process manager** | systemd service (`monitorx.service`) |

---

## Directory Structure

```
MonitorX/
├── backend/
│   ├── main.py              # ~2940 lines — ENTIRE BACKEND (single file)
│   └── requirements.txt     # Points to ../requirements.txt
├── frontend/
│   ├── index.html           # ~745 lines — Full dashboard HTML
│   ├── css/
│   │   ├── styles.css       # ~1580 lines — All styling (glassmorphism dark/light)
│   │   ├── modern-overrides.css # Progressive enhancement layer
│   │   └── nasa-theme.css   # v2.2 — NASA mission-control HUD theme
│   └── js/
│       ├── app.js           # ~2400 lines — All frontend logic
│       └── nasa-enhance.js  # v2.2 — MET/UTC clocks, telemetry ticker, boot seq
├── systemd/
│   ├── monitorx.service     # Systemd unit file
│   └── install-service.sh   # Installer with sudoers policies
├── .opencode/
│   └── MEMORY.md            # THIS FILE — AI agent context
├── .venv/                   # Python virtual environment
├── requirements.txt         # Pinned Python deps
├── setup.sh                 # Installs system packages + creates venv
├── launch.sh                # Starts server on port 8080
└── README.md                # Documentation
```

---

## Tech Stack Details

### Python Dependencies (requirements.txt)
```
fastapi==0.111.0
uvicorn[standard]==0.30.1
websockets==12.0
psutil==5.9.8
jinja2==3.1.4
aiofiles==23.2.1
py3nvml==0.2.7
```

### System Dependencies (optional)
- `python3-libvirt` — VM monitoring/control (KVM/QEMU)
- `nvidia-ml-py` / py3nvml — GPU monitoring
- `docker` CLI — Container monitoring
- `kubectl` — Kubernetes pod monitoring
- `virsh` — VM lifecycle fallback control

### Frontend CDN Dependencies
- xterm.js 5.5.0 — VM console terminal
- @xterm/addon-fit — Terminal auto-fit
- @xterm/addon-web-links — Clickable links in terminal
- Google Fonts (Inter + JetBrains Mono + Orbitron for HUD headings)

---

## Architecture Overview

### Data Flow
```
Browser <--WebSocket (2s)---> FastAPI Backend ---> psutil (CPU/RAM/Disk/Net)
                                                ---> libvirt (VMs)
                                                ---> NVML (GPU)
                                                ---> docker CLI (Containers)
                                                ---> kubectl (Pods)
                                                ---> systemctl (Services)
```

### Single WebSocket Broadcast
- `broadcast_stats()` runs every 2 seconds
- Collects ALL system metrics in one snapshot (`collect_all_stats()`)
- Pushes to all connected WebSocket clients
- Uses `asyncio.Lock` (`stats_lock`) to serialize rate calculations

---

## Backend Architecture (backend/main.py)

### Key Global State
```python
stats_lock = asyncio.Lock()          # Serializes metric snapshots
last_net_io / last_net_time          # Network rate calculation
last_disk_io / last_disk_time        # Disk rate calculation
vm_metric_samples: Dict              # Per-VM cumulative counter snapshots
vm_metrics_lock = asyncio.Lock()     # Guards vm_metric_samples
libvirt_conn = None                  # Read-only libvirt connection
libvirt_rw_conn = None              # Read-write libvirt connection
_vm_ssh_configs: Dict                # Per-VM SSH configs (persisted to disk)
_vm_action_log: List                 # Audit log ring buffer (50 entries)
```

### Libvirt Connection Management
- **Two connections**: read-only (metrics) + read-write (lifecycle control)
- Both use `qemu:///system` URI (configurable via `MONITORX_LIBVIRT_URI`)
- Lazy reconnect on every access (handles libvirtd restarts)
- Thread executor (`_libvirt_executor`) for blocking libvirt calls
- `_run_libvirt(func, timeout)` — runs blocking libvirt in executor with timeout
- `_ensure_libvirt_conn()` / `_ensure_libvirt_rw_conn()` — connection health checks
- `_resolve_domain(vm_id, conn)` — lookup by numeric ID, UUID, or name

### VM Control Flow
1. Try native libvirt API via read-write connection
2. Fall back to `sudo virsh` (scoped via `/etc/sudoers.d/` policy)
3. State validation prevents no-ops (e.g., starting already-running VM)
4. Actions: start, shutdown, reboot, suspend, resume, poweroff (=virsh destroy)
5. Audit log tracks last 50 actions with timestamps

### Docker/Container Monitoring
- Uses `docker ps -a --format '{{json .}}'` CLI (no Python Docker SDK)
- `get_docker_containers()` — list all containers
- `get_docker_container_stats()` — live resource usage (`docker stats --no-stream`)
- `get_docker_container_logs(container_id, lines)` — fetch logs
- Per-VM containers via SSH: `_ssh_exec(config, command)` runs commands on VMs
- SSH configs stored in `.vm-ssh-config.json` (loaded at startup)

### Kubernetes Pod Monitoring
- Uses `kubectl get pods -A -o json` CLI
- `get_kubernetes_pods()` — parses pod list with namespace, node, restarts, status

### Console WebSocket Proxy
- `WS /ws/vm-console/{vm_id}`
- Tries VNC first: parses VNC port from VM XML, proxies raw VNC bytes over WebSocket
- Falls back to serial console: runs `virsh console` subprocess, proxies stdin/stdout
- Sends JSON control messages (`{"type": "vnc", "host": ..., "port": ...}` or `{"type": "serial"}`)

### VM Resize
- `POST /api/vms/{vm_id}/resize` with `{"vcpus": N, "memory_mb": M}`
- Uses `virDomainSetVcpusFlags()` and `virDomainSetMemoryFlags()` via libvirt API
- Falls back to `sudo virsh setvcpus` / `sudo virsh setmem --config`

---

## Frontend Architecture (frontend/js/app.js)

### State Object
```javascript
state = {
    currentTab, currentSubTab,       // Navigation
    processFilter, processSearch,    // Process manager
    logLevel, logLines, logAutoTail, // Log inspector
    vmSearch, vmStateFilter, vmSort, // VM filters
    vmSelected: Set,                 // Bulk selection
    vmRefreshMs, vmAutoTimer,        // VM auto-refresh
    vmCapabilities,                  // VM control auth status
    vmPending: Set,                  // VMs with actions in flight
    vmLastAction: Map,               // Optimistic state tracking
    consoleTerminal, consoleWs,      // xterm.js console state
    resizeVmId, resizeVcpus, resizeMemMb, // Resize modal
    sshVmId,                         // SSH config modal
    containerStats,                  // Container stats cache
}
```

### Tab System
- 5 main tabs: Dashboard, Processes, Troubleshoot Hub, VMs, Systemd Services
- Troubleshoot Hub has 5 sub-tabs: Health Scan, Log Inspector, Network Suite, Bottlenecks, Terminal
- Tab switching via `switchTab(tabId)` and `switchSubTab(subtabId)`

### VM Card Rendering
- `renderVms(vms)` — rebuilds VM card grid
- `vmActionButtons(vm)` — lifecycle buttons (start/stop/reboot/etc.)
- `vmExtraButtons(vm)` — Console, Resize, SSH Config buttons
- `initVmDelegation(container)` — single delegated click handler per container
- Prevents re-render while actions in flight (`state.vmPending`)
- Optimistic state tracking prevents stale polls flipping cards back

### Key Rendering Functions
```javascript
updateDashboard(data)       // Main entry — updates all sections
updateCpu(cpu)              // CPU bars + stats
updateMemory(mem)           // RAM progress bar
updateDisk(disk)            // Partition list
updateNetwork(net)          // Interface list
updateGpu(gpus)             // GPU cards
updateSystem(sys)           // System metadata
updateTopProcesses(procs)   // Top 10 table
checkOSIssues(data)         // Alerts panel
updateCharts(data)          // Canvas sparklines
renderContainers(containers) // Docker container grid
renderPods(pods)            // Kubernetes pod grid
renderVms(vms)              // VM card grid
renderVmContainers(data)    // Per-VM container chips
```

---

## CSS Architecture (frontend/css/styles.css)

### Design System
- Glassmorphism dark theme (default) + light theme toggle
- **v2.2 NASA mission-control theme** (`nasa-theme.css`, loaded last): deep-space palette, HUD corner brackets on every panel, CRT scanlines + drifting starfield/grid overlay, MET/UTC clocks, live telemetry ticker, radar-sweep health gauge, flight-control boot sequence
- CSS custom properties for all colors (`--accent-blue`, `--success`, `--danger`, etc.); NASA theme overrides these to a telemetry spectrum
- Fonts: Inter (UI) + JetBrains Mono (code/metrics) + Orbitron (HUD headings)
- Responsive: mobile breakpoint at 768px

### NASA Theme Files (v2.2)
```css
nasa-theme.css   // Full visual overhaul: palette, HUD brackets, scanlines, grids,
                 //   ticker, boot, radar gauge. Loaded AFTER styles.css + modern-overrides.css.
nasa-enhance.js  // MET clock, UTC clock, telemetry ticker (reads app.js DOM values),
                 //   flight-control boot sequence. Progressive enhancement, guards all nodes.
```
- Overlay `.nasa-overlay` (z-index 40, pointer-events none) sits below header(100)/tabs(90)/modals(200).
- All original IDs/classes preserved so `app.js` is untouched. Theme toggle still switches to a "clean-room" light variant.

### Key CSS Classes
```css
.metric-card          // Dashboard metric cards (CPU, RAM, etc.)
.vm-card              // VM card with state-aware borders
.container-card       // Docker container card
.vm-state.running     // Green state pill
.vm-state.shutoff     // Red state pill
.vm-state.paused      // Yellow state pill
.toast.success/error  // Toast notifications
.modal.show           // Active modal overlay
.btn-primary/danger   // Button variants
```

---

## API Endpoints Reference

### System Stats (auto-broadcast via WebSocket)
```
GET  /api/stats                    // Full snapshot (SystemStats model)
GET  /api/stats/cpu
GET  /api/stats/memory
GET  /api/stats/disk
GET  /api/stats/network
GET  /api/stats/gpu
GET  /api/stats/processes
GET  /api/stats/system
GET  /api/stats/vms
GET  /api/stats/containers         // Docker containers on host
GET  /api/stats/containers/stats   // Live container resource usage
GET  /api/stats/containers/{id}/logs?lines=100
GET  /api/stats/pods               // Kubernetes pods
```

### VM Control
```
GET    /api/vms/capabilities       // Auth status check
POST   /api/vms/{id}/{action}      // start/shutdown/reboot/suspend/resume/poweroff
POST   /api/vms/{id}/resize        // CPU/RAM resize
GET    /api/vms/log                // Audit log
GET    /api/vms/{id}/ssh-config    // Get SSH config
POST   /api/vms/{id}/ssh-config    // Set SSH config
DELETE /api/vms/{id}/ssh-config    // Remove SSH config
GET    /api/vms/{id}/containers    // Docker containers inside VM (via SSH)
```

### WebSocket Endpoints
```
WS  /ws                            // Stats broadcast (2s interval)
WS  /ws/vm-console/{vm_id}         // VM console proxy (VNC or serial)
```

### Troubleshoot APIs
```
GET  /api/troubleshoot/health-check
GET  /api/troubleshoot/logs?lines=100&level=all&search=
POST /api/troubleshoot/ping
POST /api/troubleshoot/port-check
POST /api/troubleshoot/dns-lookup
GET  /api/troubleshoot/network-ports
GET  /api/troubleshoot/bottlenecks
POST /api/troubleshoot/remediate
POST /api/commands/run             // Approved diagnostic presets only
```

### Process Management
```
GET  /api/processes/{pid}
POST /api/processes/{pid}/kill?signal=15
```

### Systemd Services
```
GET  /api/services/capabilities
GET  /api/services
POST /api/services/{name}/{action}  // start/stop/restart/reload/enable/disable
```

---

## Important Patterns & Gotchas

1. **No auth by design** — MonitorX is for trusted internal networks only
2. **Single-file backend** — all 2940 lines in `main.py`, no separate route files
3. **Rate calculations** use two successive samples (cumulative counters from psutil/libvirt)
4. **VM action debouncing** — `state.vmPending` prevents re-render during actions
5. **Optimistic state** — `vmLastAction` map prevents stale polls from flipping cards
6. **Dual libvirt paths** — native API preferred, `sudo virsh` fallback for unprivileged users
7. **Console WebSocket** — sends JSON control messages first, then raw bytes
8. **SSH per-VM** — configs in `.vm-ssh-config.json`, loaded at startup, used for container monitoring
9. **Docker monitoring** — uses CLI (`docker ps`), not Python SDK (no extra pip deps)
10. **Safe commands only** — terminal runner only allows approved diagnostic presets

---

## File Change Log

### v2.5 (2026-08-09) — FLIGHT CONTROL LOOP REMOVED + COMPACT HOST IDENTITY
**Removed**:
- The GO/NO-GO Flight Control Loop board (`#mission-board`) from the top of the Dashboard tab, with `frontend/js/mission-control.js` and `frontend/css/mission-control.css` deleted outright. The header MET/UTC readout keeps the **DOY mission clock** — its tick moved into `nasa-enhance.js` and its styles into `nasa-theme.css`.
**Changed**:
- Host identity (System Metadata) card sized to content so it does not eat freed space: `align-self: start` (no stretching to taller grid neighbors) and compact rows (no 38px floor, tighter padding/gap) scoped to `.system-metadata-card` — the process-inspector modal keeps the roomier default `.system-info` list.
- `quality-overrides.css` focus-mode rule no longer references `.mission-board`.
**Tests**: `tests/test_contracts.py` drops the mission-control.js source reads; new contract asserts the board markup, asset links, and files stay removed, and that no frontend module besides `app.js` opens a raw `/ws` socket.

### v2.4 (2026-08-05) — NASA-LEVEL PASS: FLIGHT CONTROL LOOP + EFFECT RESTORATION
**Added**:
- **GO/NO-GO Flight Control Loop board** (top of Dashboard tab): nine flight-controller stations — BOOSTER (storage), GUIDO (CPU), TELMU (memory), INCO (network), EECOM (thermal), FIDO (zombie/stuck processes), SURGEON (GPU), GC (systemd failed units), CAPCOM (WebSocket datalink) — each voting GO / CAUTION / NO-GO from live telemetry. Worst vote drives the MISSION STATUS lamp; any NO-GO raises a blinking **MASTER ALARM** (board wash + aria-live announcement). Hardware-less stations (no GPU/sensors/systemd bus) degrade to STANDBY instead of failing.
- **DOY mission clock** (UTC day-of-year `DDD/HH:MM:SS`) added to the header MET/UTC readout.

**Fixed**:
- `dashboard-refresh.css` had `display: none !important` on `.nasa-overlay`, `.nasa-boot`, `.nasa-ticker`, `.nx-fab`, `.nx-progress`, `.nx-cmd-overlay`, `.nx-shortcuts-overlay` — suppressing the entire NASA theme and **breaking the ⌘K command palette** (it opens via `.open` class but could never show). NASA effects are restored; only the duplicate `.nx-stars` layer stays off (nasa-overlay provides its own starfield) and scanline/grid opacity is toned down for data legibility.
- Backend default bind aligned with docs: `MONITORX_HOST` now defaults to `0.0.0.0` (README/MEMORY always promised 0.0.0.0:8080; override with `MONITORX_HOST=127.0.0.1`).
- FastAPI app version bumped 2.0.0 → 2.4.0.

**Files changed**: `frontend/js/mission-control.js` (NEW ~300 lines), `frontend/css/mission-control.css` (NEW ~260 lines), `frontend/index.html` (+board markup, +DOY line, +asset links), `frontend/css/dashboard-refresh.css`, `backend/main.py`, `README.md`, `.opencode/MEMORY.md`.

**Verified**: headless DOM-stub execution against the live server — nominal frame → ALL STATIONS GO; synthetic hot frame → 6 stations NO-GO + MASTER ALARM rollup; datalink loss → CAPCOM NO-GO; 503 `/api/services` → GC STANDBY. All 12 static assets 200; `node --check` + `py_compile` clean.

### v2.3 (Maintenance — 2026-08-05) — RELIABILITY & BUG-FIX PASS
**Fixed** (backend/main.py):
- `_virsh_command()` / `_build_virsh_modify_command()` now emit `--no-pkttyagent`, matching the sudoers policy installed by `systemd/install-service.sh`. Previously sudo rejected EVERY VM control command on unprivileged installs ("not allowed to execute") — same bug class as the old `--no-ask-password` mismatch.
- Alert engine: `cooldown_minutes` is now enforced and acknowledged/resolved incidents no longer re-open every 2s snapshot (incident flood). History cleanup DELETE throttled to every 10 min; SQLite ops DB now uses WAL + busy_timeout.
- Zombie/hung-process detection in the health check scans ALL processes instead of the CPU-sorted top-200 (zombies have ~0% CPU and were truncated away).
- `kill_process`: SIGTERM now gets a 5s grace period before escalating to SIGKILL (was 0.5s, making SIGTERM pointless).
- `get_process_detail`: per-field degradation on AccessDenied instead of failing the whole request (unprivileged installs can now inspect root-owned processes).
- VM console: VNC autoport (-1) handled, dead VNC listeners fall back to serial console, and the serial path runs `virsh console` on a real pty with policy-matching argv (was broken under pipes + not whitelisted).
- `resize` endpoint: audit log records real success/failure instead of always `true`.
- DNS lookup uses `get_running_loop()`; `/api/stats/processes` limit bounded.

**Fixed** (frontend):
- app.js keyboard 'r' handler now ignores Ctrl/Cmd/Alt combos (browser reload still works); duplicate 'r' handler removed from nexus-hud.js.
- VM console ResizeObserver is disconnected on close (leak).

**Files changed**: `backend/main.py`, `systemd/install-service.sh` (policy now also whitelists `console -- *`, `setvcpus *`, `setmem *`, `setmaxmem *`), `frontend/js/app.js`, `frontend/js/nexus-hud.js`, `README.md`, `.opencode/MEMORY.md`.

### v2.6 (2026-08-09) — CARD-BASED SERVICES MANAGER + 13 THEMES + SCREEN-FIT POLISH
**Added** (frontend only — no backend changes):
- Systemd Services tab rewritten from a wide 6-column table into the KVM-style card manager: KPI counters (Total / Active / Failed / Inactive / Loaded), permission notice, search + state filter + sort bar, auto-refresh interval selector (Manual / 2s / 5s / 10s / 30s), sticky bulk-action toolbar (Start / Stop / Restart / Reload / Enable / Disable / Clear), and per-unit cards showing description, load/active/sub states, and the full action set (Start/Stop/Restart/Reload/Enable/Disable/📜 Logs) with the existing confirm modal for destructive actions. Graceful inline empty/error states when systemd is unreachable. State lives in `state.svc*`; delegated listeners + `svcPending` suppression mirror the VM tab patterns; `tests/smoke-services.js` (jsdom) covers rendering/filter/bulk/logs.
- Three new NATURAL themes in `themes.css` + picker: **Lagoon** (turquoise reef), **Meadow** (spring green), **Canyon** (red rock), **Sakura** (cherry blossom) — 13 themes total. Theme menu gained max-height + internal scroll.
- Dashboard sizing polish in `quality-overrides.css`: legacy panel margins nulled inside the 2-column grid (spacing was doubling), `min-width:0` + ellipsis guards on card footers and disk/net rows, nested `.top-processes > .table-container` panel stripped, compact chart-card padding, clamp()ed metric values, tighter context strip.

**Files changed**: `frontend/index.html`, `frontend/js/app.js`, `frontend/js/nexus-hud.js` (removed dead #services-body tinting), `frontend/css/themes.css`, `frontend/css/quality-overrides.css`, `frontend/css/services.css` (NEW), `tests/smoke-services.js` (NEW), `README.md`, `.gitignore`, `.opencode/MEMORY.md`.

### v2.2 (Previous — 2026-07-29) — NASA MISSION-CONTROL THEME
**Added**: Full flight-control / mission-control HUD re-skin of the existing dashboard. No backend or app.js changes — pure progressive enhancement.

**Files changed**:
- `frontend/css/nasa-theme.css`: NEW (~470 lines) — deep-space palette, HUD corner brackets on all panels, CRT scanlines + drifting starfield/grid overlay, vignette, radar-sweep health gauge, MET/UTC mission readout, live telemetry ticker, flight-control boot sequence, glow/telemetry typography (Orbitron headings, mono data).
- `frontend/js/nasa-enhance.js`: NEW (~150 lines) — MET clock (T+ elapsed), UTC clock, live telemetry marquee (reads values rendered by app.js), datalink status relabel, boot sequence with failsafe auto-fade.
- `frontend/index.html`: +overlay/boot markup, +Orbitron font link, +`nasa-theme.css` link, +MET/UTC readout in header, +telemetry ticker strip in main content, +`nasa-enhance.js` script.
- `.opencode/MEMORY.md`: documented v2.2 + theme architecture.

### v2.1 (Previous — 2026-07-29)
**Added**: VM Console access, VM CPU/RAM Resize, Docker container monitoring, Kubernetes pod monitoring, per-VM container monitoring via SSH

**Files changed**:
- `backend/main.py`: +678 lines (console WS proxy, resize API, container/pod functions, SSH config, Docker endpoints)
- `frontend/js/app.js`: +665 lines (xterm.js console, resize modal, container/pod rendering, SSH config UI)
- `frontend/css/styles.css`: +274 lines (console modal, resize sliders, container cards, pod grid, per-VM container chips)
- `frontend/index.html`: +141 lines (xterm.js CDN, console/resize/SSH/logs modals, container/pod panels)

### v2.0 PRO (Previous)
- Full dashboard: CPU, RAM, Disk, Network, GPU, System Info
- Process manager with kill capability
- Troubleshoot Hub: Health Scan, Logs, Network Suite, Bottlenecks, Terminal
- VM management: Lifecycle controls, bulk actions, audit log
- Systemd service management
