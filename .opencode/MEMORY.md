# MonitorX - AI Agent Memory File

> **Purpose**: This file gives any AI agent a fast, complete understanding of the MonitorX codebase without scanning every file. Read this FIRST before any task.

---

## Quick Facts

| Fact | Value |
|------|-------|
| **What** | Real-time Linux server monitoring dashboard (self-hosted, no auth) |
| **Version** | v2.0 PRO |
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
│   ├── index.html           # ~735 lines — Full dashboard HTML
│   ├── css/
│   │   └── styles.css       # ~1580 lines — All styling (glassmorphism dark/light)
│   └── js/
│       └── app.js           # ~2400 lines — All frontend logic
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
- Google Fonts (Inter + JetBrains Mono)

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
- CSS custom properties for all colors (`--accent-blue`, `--success`, `--danger`, etc.)
- Fonts: Inter (UI) + JetBrains Mono (code/metrics)
- Responsive: mobile breakpoint at 768px

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

### v2.1 (Latest — 2026-07-29)
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
