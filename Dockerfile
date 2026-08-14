# syntax=docker/dockerfile:1

# =============================================================================
# MonitorX — multi-stage Docker image
#
# Stage 1 (builder) compiles the Python dependencies, including the optional
# python3-libvirt bindings, against a compatible ABI.
# Stage 2 (runtime) is the minimal image that ships the app, narrowly scoped
# host tools (virsh/system diagnostics), and a non-root user.
#
# Build:
#   docker build -t monitorx .
#
# Run (host integration — recommended):
#   docker run -d --name monitorx \
#     -p 8080:8080 \
#     -v /var/run/libvirt/libvirt-sock:/var/run/libvirt/libvirt-sock:ro \
#     -v monitorx-data:/app/data \
#     monitorx
#
# The dashboard runs unprivileged in the container; VM/service *control*
# buttons need the host integration documented in README/systemd. Metrics
# panels (CPU/RAM/disk/network/GPU) work out of the box. Mounting libvirt's
# socket enables VM monitoring; container and Kubernetes CLI integrations are
# intentionally not part of MonitorX.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — compile wheels + libvirt bindings
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build toolchain required to compile psutil / py3nvml / libvirt-python wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libvirt-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install from the single canonical dependency list.
COPY requirements.txt ./

# Create a self-contained virtualenv that the runtime image can copy verbatim.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install libvirt-python==11.3.0

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim image with app + optional CLI tools
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MONITORX_HOST="0.0.0.0" \
    MONITORX_LIBVIRT_URI="qemu:///system" \
    PATH="/opt/venv/bin:$PATH"

# CA certs (outbound ping/DNS tests), tini (proper PID 1 / signal handling),
# virsh (libvirt-clients) for the VM control fallback path, curl for the
# healthcheck, and procps for diagnostics. systemd is intentionally absent:
# the services panel reports "systemd unavailable" inside a container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        libvirt-clients \
        procps \
        curl \
    && rm -rf /var/lib/apt/lists/*


# Copy the prebuilt virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Application code (backend + static frontend served by FastAPI).
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create a dedicated unprivileged user.
RUN useradd --create-home --uid 1000 monitorx \
    && mkdir -p /app/data \
    && chown -R monitorx:monitorx /app

USER monitorx

# Persistent SQLite operations history lives here; mount a volume to retain it.
ENV MONITORX_OPERATIONS_DB="/app/data/monitorx-operations.db"

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# tini reaps zombies and forwards signals so `docker stop` works cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

WORKDIR /app/backend
CMD ["python", "main.py"]
