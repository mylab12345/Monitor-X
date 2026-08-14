# MonitorX

A local-first Linux monitoring dashboard for live host metrics, diagnostics, processes, systemd services, and libvirt virtual machines.

## Run locally

```bash
./setup.sh
./launch.sh
```

Open <http://localhost:8080>.

> `setup.sh` targets Debian/Ubuntu hosts and installs the optional libvirt dependencies. To expose MonitorX beyond localhost, put it behind a trusted HTTPS reverse proxy and set a strong `MONITORX_AUTH_TOKEN`.

## Run with Docker

```bash
MONITORX_AUTH_TOKEN="change-this" docker compose up --build -d
```

The container provides dashboard metrics. Host systemd control requires the native installation; mount the libvirt socket only when VM monitoring is needed.

## Configuration

Copy `.env.example` to `.env` for local settings. Common variables:

- `MONITORX_HOST` — bind address (default: `127.0.0.1`)
- `MONITORX_PORT` — HTTP port (default: `8080`)
- `MONITORX_AUTH_TOKEN` — required when the app is exposed beyond localhost
- `MONITORX_STATE_DIR` — directory for local state and audit data

## Project layout

```text
backend/       FastAPI application
frontend/      Static dashboard assets
systemd/       Optional native service installer and unit template
tests/         API, security, and browser smoke tests
Dockerfile     Container image
docker-compose.yml  Local container deployment
```

## Verify

```bash
python -m pytest
node tests/smoke-services.js
node tests/smoke-rootstorage-processes.js
node tests/smoke-vm-insights.js
```

## Security

MonitorX can inspect and, when explicitly authorized, operate host services and VMs. Keep it private, use token authentication outside localhost, and review `systemd/install-service.sh` before granting its narrowly scoped sudo policy.
