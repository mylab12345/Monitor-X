# System Monitoring Dashboard (MonitorX)

Real-time monitoring dashboard for Linux Mint servers with CPU, RAM, GPU, Disk, I/O, VM, and troubleshooting features.

## Features

- **Live Dashboard** — CPU (per-core), RAM, Disk, GPU, Network, System info
- **Process Manager** — View, search, filter, and kill processes; full detail modal
- **VM Monitoring** — See all VMs running on libvirt/VMM with state, memory, vCPUs
- **Troubleshooting** — System health checks, error log viewer, network diagnostics, command runner
- **Service Management** — Start/stop/restart systemd services
- **OS Issue Detection** — Auto alerts for high CPU, memory, disk, swap, zombie processes, GPU overheating
- **Real-time Updates** — WebSocket push every 2 seconds

## Quick Start

```bash
./setup.sh
./launch.sh
```

Then open **http://localhost:8080**

## Tabs

| Tab | Description |
|-----|-------------|
| Dashboard | Real-time metrics with WebSocket |
| Processes | Full process list with kill capability |
| Troubleshoot | System checks, error logs, resource analysis, command runner |
| VMs | Virtual machines from libvirt |
| Services | Systemd services with start/stop/restart |

## System Requirements

- Linux Mint (or any Linux with systemd)
- Python 3.12+
- libvirt (for VM monitoring)
- NVIDIA GPU + py3nvml (for GPU monitoring, optional)

## Project Structure

```
monitoring-dashboard/
├── backend/
│   ├── main.py           # FastAPI application
│   └── requirements.txt  # Python deps
├── frontend/
│   ├── index.html        # Dashboard HTML
│   ├── css/styles.css    # Styling
│   └── js/app.js         # Real-time logic
├── systemd/
│   └── monitoring-dashboard.service
├── launch.sh
├── setup.sh
└── README.md
```