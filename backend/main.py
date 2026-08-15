"""
Monitoring Dashboard Backend - FastAPI Application
Provides real-time system monitoring via WebSocket and REST API
"""
import asyncio
import collections
import concurrent.futures
import glob
import hmac
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import signal
import socket
import sqlite3
import shutil
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
import psutil

# Optional imports for GPU monitoring
try:
    import py3nvml.py3nvml as nvml
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False

try:
    import libvirt
    LIBVIRT_AVAILABLE = True
except ImportError:
    LIBVIRT_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Runtime tuning. Core telemetry stays responsive while slower host integrations
# are cached independently (see _cached_optional below).
MONITORX_HOST = os.environ.get("MONITORX_HOST", "127.0.0.1")
STATS_INTERVAL = max(float(os.environ.get("MONITORX_STATS_INTERVAL", "2")), 0.5)
PROCESS_STATS_LIMIT = max(int(os.environ.get("MONITORX_PROCESS_LIMIT", "30")), 1)
PROCESS_STATS_TTL = max(float(os.environ.get("MONITORX_PROCESS_TTL", "5")), 1.0)
NETWORK_CONNECTIONS_TTL = max(float(os.environ.get("MONITORX_CONNECTIONS_TTL", "10")), 1.0)
MONITORX_AUTH_TOKEN = os.environ.get("MONITORX_AUTH_TOKEN", "").strip()
AUTH_COOKIE_NAME = "monitorx_auth"
AUTH_EXEMPT_PATHS = {"/api/health", "/api/auth/login", "/api/auth/logout"}
MAX_DIAGNOSTIC_OUTPUT = 100_000
# Browser sessions are short-lived random ids mapped to an expiry, never the
# shared secret itself. See _issue_session/_session_valid below.
SESSION_TTL_SECONDS = max(int(os.environ.get("MONITORX_SESSION_TTL", str(12 * 3600))), 60)
# Optional hardware integrations must never hold up the first dashboard frame.
# A later telemetry frame fills them in once available.
OPTIONAL_COLLECTOR_TIMEOUT = max(float(os.environ.get("MONITORX_OPTIONAL_TIMEOUT", "2")), 0.5)

# Global state tracking for rate calculations
# Network/disk rates are derived from a previous sample; serialize snapshots.
stats_lock = asyncio.Lock()

last_net_io = None
last_net_time = None
last_disk_io = None
last_disk_time = None
last_net_connections_count = None
last_net_connections_time = 0.0

# Slow optional collectors are cached so hardware and hypervisor reads
# cannot block the two-second core telemetry loop.
_peripheral_cache = {}
_peripheral_cache_lock = asyncio.Lock()
_process_stats_cache = {}
_process_stats_cache_lock = asyncio.Lock()

# Libvirt counters are cumulative. Keep one prior sample per domain to calculate
# instantaneous CPU, disk, and network rates.
vm_metric_samples: Dict[str, Dict[str, float]] = {}
vm_metrics_lock = asyncio.Lock()

# Initialize NVML if available
if NVML_AVAILABLE:
    try:
        nvml.nvmlInit()
        logger.info("NVML initialized successfully")
    except Exception as e:
        logger.warning(f"NVML initialization failed: {e}")
        NVML_AVAILABLE = False

# ==============================================================================
# LIBVIRT CONNECTION MANAGEMENT
#
# Two connections are maintained against the same hypervisor URI:
#   * read-only  -> inventory + metrics polling
#   * read-write -> guest lifecycle control (start/shutdown/reboot/...)
#
# IMPORTANT: ``LIBVIRT_AVAILABLE`` reflects only whether the *Python module*
# could be imported. It is never mutated at runtime. Connection health is
# tracked separately and re-dialled lazily on every access, so a libvirtd
# restart (package upgrade, crash, socket activation) can no longer wedge the
# VM tab into a permanently disabled state until MonitorX itself is restarted.
# ==============================================================================

# Hypervisor URI. Must match between metrics and control paths, otherwise the
# dashboard lists guests from qemu:///system while control commands silently
# target the caller's qemu:///session (where the domain does not exist).
LIBVIRT_URI = os.environ.get("MONITORX_LIBVIRT_URI", "qemu:///system")

# CRITICAL SECURITY FIX: Validate allowed URIs at startup (root cause: env var injection risk)
ALLOWED_LIBVIRT_URIS = {"qemu:///system", "qemu:///session"}
if LIBVIRT_URI not in ALLOWED_LIBVIRT_URIS:
    logger.warning("Non-standard libvirt URI '%s' detected. Falling back to qemu:///system", LIBVIRT_URI)
    LIBVIRT_URI = "qemu:///system"

libvirt_conn = None      # read-only connection (metrics/inventory)
libvirt_rw_conn = None   # read-write connection (lifecycle control)

# Serialize (re)connect attempts so a burst of requests cannot open a storm of
# sockets against a libvirtd that is still starting up.
_libvirt_connect_lock = asyncio.Lock()
_libvirt_last_error: Optional[str] = None

# Thread executor for blocking libvirt operations
_libvirt_executor = None


def _get_libvirt_executor():
    """Get or create the thread executor for libvirt operations."""
    global _libvirt_executor
    if _libvirt_executor is None:
        _libvirt_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="libvirt"
        )
    return _libvirt_executor


async def _run_libvirt(func, timeout: float = 10.0):
    """Run a blocking libvirt call in the executor with a hard timeout.

    The connection health checks, domain lookups, and lifecycle operations
    all share this executor so that no libvirt call ever runs on the event
    loop thread — the python3-libvirt bindings are not thread-safe.
    """
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_get_libvirt_executor(), func), timeout=timeout
    )


async def _conn_alive_async(conn) -> bool:
    """Async check whether a libvirt connection is usable.

    ``_conn_alive`` calls ``conn.isAlive()`` which touches the libvirt
    connection object.  Because python3-libvirt is **not** thread-safe the
    check must run in the executor alongside every other libvirt call,
    never directly on the event-loop thread.
    """
    if not conn:
        return False
    try:
        return await _run_libvirt(lambda: conn.isAlive(), timeout=5.0) == 1
    except Exception:
        return False


async def _ensure_libvirt_conn() -> bool:
    """Ensure the read-only libvirt connection is alive, reconnecting if needed.

    Returns True when a usable connection is available.
    """
    global libvirt_conn, _libvirt_last_error
    if not LIBVIRT_AVAILABLE:
        return False
    if await _conn_alive_async(libvirt_conn):
        return True

    async with _libvirt_connect_lock:
        # Another waiter may have reconnected while we waited for the lock.
        if await _conn_alive_async(libvirt_conn):
            return True
        if libvirt_conn is not None:
            try:
                libvirt_conn.close()
            except Exception:
                pass
            libvirt_conn = None
        try:
            libvirt_conn = await _run_libvirt(
                lambda: libvirt.openReadOnly(LIBVIRT_URI), timeout=10.0
            )
            _libvirt_last_error = None
            logger.info("Libvirt read-only connection established (%s)", LIBVIRT_URI)
            return True
        except Exception as exc:
            libvirt_conn = None
            _libvirt_last_error = str(exc)
            logger.warning("Libvirt read-only connection failed: %s", exc)
            return False


async def _ensure_libvirt_rw_conn():
    """Ensure a read-write libvirt connection for lifecycle control.

    Returns ``(connection, error_message)``. A read-write connection succeeds
    when MonitorX runs as root or its user is in the ``libvirt``/``kvm`` group
    (or a polkit rule grants ``org.libvirt.unix.manage``). When it fails the
    caller transparently falls back to the ``sudo virsh`` path.
    """
    global libvirt_rw_conn
    if not LIBVIRT_AVAILABLE:
        return None, "libvirt Python bindings are not installed on this host."
    if await _conn_alive_async(libvirt_rw_conn):
        return libvirt_rw_conn, None

    async with _libvirt_connect_lock:
        if await _conn_alive_async(libvirt_rw_conn):
            return libvirt_rw_conn, None
        if libvirt_rw_conn is not None:
            try:
                libvirt_rw_conn.close()
            except Exception:
                pass
            libvirt_rw_conn = None
        try:
            libvirt_rw_conn = await _run_libvirt(
                lambda: libvirt.open(LIBVIRT_URI), timeout=10.0
            )
            logger.info("Libvirt read-write connection established (%s)", LIBVIRT_URI)
            return libvirt_rw_conn, None
        except Exception as exc:
            libvirt_rw_conn = None
            return None, str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    init_operations_store()
    asyncio.create_task(broadcast_stats())
    logger.info("Monitoring Dashboard started")
    yield
    # Shutdown
    if NVML_AVAILABLE:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass
    for _conn in (libvirt_conn, libvirt_rw_conn):
        if _conn:
            try:
                _conn.close()
            except Exception:
                pass
    global _libvirt_executor
    if _libvirt_executor:
        _libvirt_executor.shutdown(wait=False)
        _libvirt_executor = None
    logger.info("Monitoring Dashboard stopped")


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# ==============================================================================
# LOCAL STATE DIRECTORY
#
# Metrics history and the kill/diagnostic audit trail used to live at fixed
# paths under /tmp (mode 0644, in a world-writable sticky directory). That
# leaked host telemetry to every local user and — worse for the audit log —
# let an unprivileged user pre-create or symlink the path before MonitorX
# started, redirecting appends to a file of their choosing.
#
# State now defaults to the repo/install directory and is created 0700, with
# every file written 0600 and O_NOFOLLOW to defeat symlink swaps.
# ==============================================================================
STATE_DIR = Path(os.environ.get("MONITORX_STATE_DIR", str(BASE_DIR))).expanduser()


def _ensure_state_dir() -> Path:
    """Create (once) and return the private state directory."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_DIR != BASE_DIR:
            # Only tighten a directory we own; never chmod the repo root.
            os.chmod(STATE_DIR, 0o700)
    except OSError as exc:
        logger.warning("Could not prepare state directory %s: %s", STATE_DIR, exc)
    return STATE_DIR


def _secure_open_append(path: Path):
    """Open ``path`` for append with 0600, refusing to follow a symlink.

    O_NOFOLLOW makes the open fail outright if an attacker planted a symlink
    at this path, instead of silently writing through it.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, "a")


def _harden_file_mode(path: Path) -> None:
    """Best-effort chmod 0600 on a state file we just created."""
    try:
        if path.exists() and not path.is_symlink():
            os.chmod(path, 0o600)
    except OSError:
        pass


# ==============================================================================
# BROWSER SESSIONS
#
# The login cookie used to contain MONITORX_AUTH_TOKEN verbatim. That is the
# long-lived shared secret for the whole deployment: anything that could read
# one browser's cookie jar (a backup, an XSS bypass of HttpOnly via a proxy, a
# shared machine) obtained permanent API access, and "log out" only deleted the
# client-side copy — the value it had handed out stayed valid forever.
#
# Logging in now mints an opaque random session id that maps to an expiry
# server-side. Logout deletes the mapping, so the credential is actually dead.
# ==============================================================================
_sessions: Dict[str, float] = {}


def _prune_sessions(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for sid in [s for s, exp in _sessions.items() if exp <= now]:
        _sessions.pop(sid, None)


def _issue_session() -> str:
    """Mint an opaque session id bound to a server-side expiry."""
    _prune_sessions()
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = time.time() + SESSION_TTL_SECONDS
    return sid


def _session_valid(sid: str) -> bool:
    if not sid:
        return False
    expiry = _sessions.get(sid)
    if expiry is None:
        return False
    if expiry <= time.time():
        _sessions.pop(sid, None)
        return False
    return True


def _revoke_session(sid: str) -> None:
    _sessions.pop(sid, None)


def _credential_valid(bearer: str, cookie: str) -> bool:
    """True when either a valid bearer token or a live session cookie is present."""
    if bearer and hmac.compare_digest(bearer, MONITORX_AUTH_TOKEN):
        return True
    return _session_valid(cookie)


def _request_bearer(request: Request) -> str:
    """Read the optional bearer token from the Authorization header."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _request_authenticated(request: Request) -> bool:
    """Validate a request via bearer token or session cookie."""
    if not MONITORX_AUTH_TOKEN:
        return True
    return _credential_valid(
        _request_bearer(request), request.cookies.get(AUTH_COOKIE_NAME, "")
    )


def _websocket_authenticated(websocket: WebSocket) -> bool:
    """Validate the optional token for WebSocket upgrades."""
    if not MONITORX_AUTH_TOKEN:
        return True
    auth = websocket.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    bearer = bearer or websocket.query_params.get("token", "")
    return _credential_valid(bearer, websocket.cookies.get(AUTH_COOKIE_NAME, ""))


class AuthMiddleware(BaseHTTPMiddleware):
    """Optional authentication guard for non-static HTTP endpoints.

    Local installs remain plug-and-play when MONITORX_AUTH_TOKEN is unset.
    Operators exposing MonitorX beyond localhost can set a token and use the
    built-in login form; the HttpOnly SameSite cookie also authenticates the
    WebSocket connections without putting the token in browser JavaScript.
    """

    async def dispatch(self, request: Request, call_next):
        if (not MONITORX_AUTH_TOKEN
                or request.url.path.startswith("/static/")
                or request.url.path == "/"
                or request.url.path in AUTH_EXEMPT_PATHS
                or request.method == "OPTIONS"):
            return await call_next(request)
        if not _request_authenticated(request):
            return JSONResponse(
                {"detail": "Authentication required. Set the MonitorX auth token and sign in."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline browser hardening without blocking the embedded preview UI."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # The dashboard renders host telemetry and exposes process-kill and VM
        # lifecycle controls, so it must not be framable and must not be able
        # to load or exfiltrate to third-party origins. 'unsafe-inline' for
        # styles only: the UI ships inline style attributes, but no inline
        # <script> is required.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Hashed/versioned static asset URLs are safe to keep locally. This
        # removes repeated transfers and parsing on every dashboard visit.
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
        return response


class AuthLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


app = FastAPI(
    title="System Monitoring Dashboard",
    description="Real-time system monitoring dashboard with WebSocket support and Troubleshoot Suite",
    version="2.5.0",
    lifespan=lifespan
)

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.post("/api/auth/login")
async def auth_login(payload: AuthLoginRequest, request: Request):
    """Create a short-lived browser session for protected deployments."""
    if not MONITORX_AUTH_TOKEN:
        return {"authenticated": True, "auth_required": False}
    if not hmac.compare_digest(payload.token, MONITORX_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid MonitorX authentication token.")
    response = JSONResponse({"authenticated": True, "auth_required": True})
    # The cookie carries a revocable session id, never MONITORX_AUTH_TOKEN.
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _issue_session(),
        httponly=True,
        secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"),
        samesite="strict",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Revoke the session server-side, not just in the browser."""
    _revoke_session(request.cookies.get(AUTH_COOKIE_NAME, ""))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


# Pydantic models
class SystemStats(BaseModel):
    timestamp: str
    cpu: Dict[str, Any]
    memory: Dict[str, Any]
    disk: Dict[str, Any]
    network: Dict[str, Any]
    gpu: Optional[List[Dict[str, Any]]] = None
    processes: List[Dict[str, Any]]
    system: Dict[str, Any]
    vms: Optional[List[Dict[str, Any]]] = None
    thermal: Optional[Dict[str, Any]] = None


class PingRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    count: int = Field(default=4, ge=1, le=10)


class PortCheckRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    timeout: float = Field(default=3.0, ge=0.1, le=10.0)


class DNSCheckRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)


class RemediateRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    target: Optional[str] = Field(default=None, max_length=128)


class VMActionRequest(BaseModel):
    """Optional payload for VM control endpoints (reserved for future use)."""
    confirm: bool = Field(default=False, description="Set true for destructive actions (poweroff/destroy).")


class VMResizeRequest(BaseModel):
    """Payload for resizing VM CPU and/or memory."""
    vcpus: Optional[int] = Field(default=None, ge=1, le=256, description="New number of vCPUs.")
    memory_mb: Optional[int] = Field(default=None, ge=256, le=1048576, description="New memory in MiB.")


# Approved libvirt domain control actions exposed to the dashboard.
# - start / shutdown / reboot / suspend / resume: graceful control operations
# - poweroff / destroy: forced termination; require explicit confirm=1
VM_ACTIONS_GRACEFUL = ("start", "shutdown", "reboot", "suspend", "resume")
VM_ACTIONS_DESTRUCTIVE = ("poweroff", "destroy")
VM_ACTIONS = VM_ACTIONS_GRACEFUL + VM_ACTIONS_DESTRUCTIVE

# `poweroff` is dashboard vocabulary, NOT a virsh command. The real virsh verb
# for a forced stop is `destroy`. Mapping it here is what makes the Poweroff
# button work instead of failing with "unknown command: 'poweroff'".
VM_ACTION_TO_VIRSH = {
    "start": "start",
    "shutdown": "shutdown",
    "reboot": "reboot",
    "suspend": "suspend",
    "resume": "resume",
    "poweroff": "destroy",
    "destroy": "destroy",
}

# Libvirt domain state enums -> human-readable names, shared by metrics
# collection and post-action status reads. Empty on hosts without libvirt.
VM_STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "no_state",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
} if LIBVIRT_AVAILABLE else {}

# Domain names may legitimately contain spaces and other characters, so the
# identifier is passed to virsh as a single argv element (never a shell string).
# We only reject leading dashes, which would be parsed as virsh options.
VM_ID_PATTERN = re.compile(r"^[^-\s][^\x00\n\r]{0,127}$")
# Bounded in-memory ring buffer of VM control actions for the audit panel.
_vm_action_log: List[Dict[str, Any]] = []
_VM_ACTION_LOG_LIMIT = 50
_vm_action_log_lock = asyncio.Lock()

VIRSH_BIN = shutil.which("virsh") or "/usr/bin/virsh"

# Guests can be slow to react (graceful shutdown waits on the guest OS ACK),
# so control commands get a longer budget than metric polls.
VM_ACTION_TIMEOUT = 60.0


async def _run_cmd(cmd: list, timeout: float = 15.0, **kwargs):
    """Run a subprocess with a hard timeout; never hang the request forever.

    Returns ``(returncode, stdout_bytes, stderr_bytes)``. On timeout the
    process is killed and ``asyncio.TimeoutError`` is raised so callers can
    degrade gracefully instead of blocking the event loop indefinitely.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise
    return proc.returncode, stdout, stderr


class ConnectionManager:
    """Manages WebSocket connections"""
    
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Client connected. Total: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"Client disconnected. Total: {len(self.active_connections)}")
    
    async def broadcast(self, message: dict):
        """Fan out one frame concurrently so a slow browser cannot stall all peers."""
        connections = list(self.active_connections)
        if not connections:
            return

        async def send(connection):
            try:
                await asyncio.wait_for(connection.send_json(message), timeout=3.0)
                return connection, None
            except Exception as exc:
                return connection, exc

        results = await asyncio.gather(*(send(connection) for connection in connections))
        for connection, error in results:
            if error is not None:
                self.disconnect(connection)


manager = ConnectionManager()


async def get_cpu_stats() -> Dict[str, Any]:
    """Get CPU statistics without blocking interval"""
    cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    load_avg = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
    
    cpu_times = psutil.cpu_times()
    return {
        "percent_per_core": cpu_percent,
        "percent_total": sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0,
        "count_logical": cpu_count or 1,
        "count_physical": cpu_count_physical or 1,
        "frequency_current": cpu_freq.current if cpu_freq else 0,
        "frequency_min": cpu_freq.min if cpu_freq else 0,
        "frequency_max": cpu_freq.max if cpu_freq else 0,
        "load_1min": load_avg[0],
        "load_5min": load_avg[1],
        "load_15min": load_avg[2],
        "times": dict(cpu_times._asdict()) if cpu_times else {}
    }


async def get_memory_stats() -> Dict[str, Any]:
    """Get memory statistics"""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    
    return {
        "total": vm.total,
        "available": vm.available,
        "used": vm.used,
        "free": vm.free,
        "buffers": getattr(vm, 'buffers', 0),
        "cached": getattr(vm, 'cached', 0),
        "percent": vm.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_free": swap.free,
        "swap_percent": swap.percent
    }


async def get_disk_stats() -> Dict[str, Any]:
    """Get disk statistics for the root filesystem (/) and transfer rates.

    Storage capacity and inode monitoring intentionally cover ONLY the root
    filesystem — space used/free/total and inodes used/free/total for "/".
    No other mount is tracked anywhere in the dashboard. ``partitions`` keeps
    its historical list shape (holding just the root entry) so existing
    consumers that iterate it keep working unchanged.
    """
    global last_disk_io, last_disk_time

    root: Dict[str, Any] = {
        "device": "",
        "mountpoint": "/",
        "fstype": "",
        "total": 0,
        "used": 0,
        "free": 0,
        "percent": 0.0,
        "inode_total": 0,
        "inode_used": 0,
        "inode_free": 0,
        "inode_percent": 0.0,
    }

    try:
        usage = psutil.disk_usage("/")
        root.update({
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round((usage.used / usage.total * 100) if usage.total > 0 else 0, 1),
        })
    except (PermissionError, FileNotFoundError, OSError):
        pass

    # Label-only: which device/fstype backs "/" (not extra monitoring).
    try:
        for partition in psutil.disk_partitions():
            if partition.mountpoint == "/":
                root["device"] = partition.device
                root["fstype"] = partition.fstype
                break
    except Exception:
        pass

    try:
        st = os.statvfs("/")
        if st.f_files > 0:
            root["inode_total"] = st.f_files
            root["inode_free"] = st.f_ffree
            root["inode_used"] = st.f_files - st.f_ffree
            root["inode_percent"] = round((root["inode_used"] / st.f_files) * 100, 1)
    except (PermissionError, FileNotFoundError, OSError):
        pass

    now = time.time()
    disk_io = psutil.disk_io_counters()
    
    read_bytes_sec = 0.0
    write_bytes_sec = 0.0
    
    if disk_io and last_disk_io and last_disk_time:
        dt = max(now - last_disk_time, 0.1)
        read_bytes_sec = max(0.0, (disk_io.read_bytes - last_disk_io.read_bytes) / dt)
        write_bytes_sec = max(0.0, (disk_io.write_bytes - last_disk_io.write_bytes) / dt)
    
    last_disk_io = disk_io
    last_disk_time = now

    return {
        "root": root,
        "partitions": [root],
        "io_read_bytes": disk_io.read_bytes if disk_io else 0,
        "io_write_bytes": disk_io.write_bytes if disk_io else 0,
        "io_read_count": disk_io.read_count if disk_io else 0,
        "io_write_count": disk_io.write_count if disk_io else 0,
        "read_bytes_sec": round(read_bytes_sec, 1),
        "write_bytes_sec": round(write_bytes_sec, 1)
    }


async def get_network_stats() -> Dict[str, Any]:
    """Get network statistics and transfer rates"""
    global last_net_io, last_net_time
    
    now = time.time()
    net_io = psutil.net_io_counters(pernic=True)
    interfaces = {}
    
    rx_bytes_sec = 0.0
    tx_bytes_sec = 0.0
    
    if net_io and last_net_io and last_net_time:
        dt = max(now - last_net_time, 0.1)
        curr_rx = sum(stat.bytes_recv for stat in net_io.values())
        curr_tx = sum(stat.bytes_sent for stat in net_io.values())
        prev_rx = sum(stat.bytes_recv for stat in last_net_io.values())
        prev_tx = sum(stat.bytes_sent for stat in last_net_io.values())
        rx_bytes_sec = max(0.0, (curr_rx - prev_rx) / dt)
        tx_bytes_sec = max(0.0, (curr_tx - prev_tx) / dt)

    last_net_io = net_io
    last_net_time = now

    for name, stats in net_io.items():
        interfaces[name] = {
            "bytes_sent": stats.bytes_sent,
            "bytes_recv": stats.bytes_recv,
            "packets_sent": stats.packets_sent,
            "packets_recv": stats.packets_recv,
            "errin": stats.errin,
            "errout": stats.errout,
            "dropin": stats.dropin,
            "dropout": stats.dropout
        }
    
    global last_net_connections_count, last_net_connections_time
    if (last_net_connections_count is None
            or now - last_net_connections_time >= NETWORK_CONNECTIONS_TTL):
        try:
            last_net_connections_count = await asyncio.to_thread(_count_net_connections_sync)
            last_net_connections_time = now
        except Exception:
            # Keep the last known count when permissions are restricted.
            if last_net_connections_count is None:
                last_net_connections_count = 0

    connections_count = last_net_connections_count or 0

    return {
        "interfaces": interfaces,
        "connections_count": connections_count,
        "rx_bytes_sec": round(rx_bytes_sec, 1),
        "tx_bytes_sec": round(tx_bytes_sec, 1)
    }


async def get_gpu_stats() -> Optional[List[Dict[str, Any]]]:
    """Get GPU statistics using NVML"""
    if not NVML_AVAILABLE:
        return None
    
    gpus = []
    try:
        device_count = nvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = nvml.nvmlDeviceGetHandleByIndex(i)
            
            name = nvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode('utf-8')
            
            try:
                temp = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = 0
            
            try:
                util = nvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_util = util.gpu
                mem_util = util.memory
            except Exception:
                gpu_util = 0
                mem_util = 0
            
            try:
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                mem_used = mem.used
                mem_total = mem.total
                mem_free = mem.free
            except Exception:
                mem_used = mem_total = mem_free = 0
            
            try:
                power_draw = nvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                power_limit = nvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)[1] / 1000.0
            except Exception:
                power_draw = 0.0
                power_limit = 0.0
            
            gpus.append({
                "index": i,
                "name": name,
                "temperature": temp,
                "utilization_gpu": gpu_util,
                "utilization_memory": mem_util,
                "memory_used": mem_used,
                "memory_total": mem_total,
                "memory_free": mem_free,
                "power_draw": round(power_draw, 1),
                "power_limit": round(power_limit, 1)
            })
    except Exception as e:
        logger.error(f"Error getting GPU stats: {e}")
        return None
    
    return gpus if gpus else None


def _count_net_connections_sync() -> int:
    """Blocking socket-table read. Call via asyncio.to_thread."""
    return len(psutil.net_connections(kind='inet'))


def _list_inet_connections_sync():
    """Blocking snapshot of the inet socket table. Call via asyncio.to_thread."""
    return psutil.net_connections(kind='inet')


def _count_threads_sync() -> int:
    """Blocking sum of thread counts across all processes."""
    total = 0
    for proc in psutil.process_iter(["num_threads"]):
        try:
            total += proc.info["num_threads"] or 0
        except Exception:
            pass
    return total


def _collect_process_stats_sync(limit: int) -> List[Dict[str, Any]]:
    """Synchronous psutil walk kept off the asyncio event loop."""
    processes = []
    for proc in psutil.process_iter([
        'pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info',
        'status', 'username', 'create_time', 'num_threads'
    ]):
        try:
            info = proc.info
            processes.append({
                "pid": info['pid'],
                "name": info['name'][:50] if info['name'] else "unknown",
                "cpu_percent": round(info['cpu_percent'] or 0.0, 1),
                "memory_percent": round(info['memory_percent'] or 0.0, 1),
                "memory_mb": round((info['memory_info'].rss / 1024 / 1024) if info['memory_info'] else 0.0, 1),
                "status": info['status'] or "unknown",
                "username": info['username'] or "unknown",
                "threads": info['num_threads'] or 1,
                "create_time": datetime.fromtimestamp(info['create_time']).strftime('%Y-%m-%d %H:%M:%S') if info['create_time'] else "unknown"
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
            continue
    processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
    return processes[:limit]


async def get_process_stats(limit: int = PROCESS_STATS_LIMIT) -> List[Dict[str, Any]]:
    """Get a cached process sample without blocking the asyncio event loop."""
    limit = max(int(limit), 1)
    now = time.monotonic()
    async with _process_stats_cache_lock:
        cached = _process_stats_cache.get(limit)
        if cached and now - cached["time"] < PROCESS_STATS_TTL:
            return cached["value"]

        value = await asyncio.to_thread(_collect_process_stats_sync, limit)
        _process_stats_cache[limit] = {"time": time.monotonic(), "value": value}
        return value


def _scan_process_states_sync() -> Dict[str, int]:
    """Blocking full-table process state walk. Call via asyncio.to_thread."""
    counts: Dict[str, int] = {}
    for proc in psutil.process_iter(['status']):
        try:
            status = (proc.info['status'] or "unknown").lower()
            counts[status] = counts.get(status, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return counts


async def _scan_process_states() -> Dict[str, int]:
    """Count processes by state across ALL processes (not a top-N subset).

    ``get_process_stats(limit=N)`` sorts by CPU and truncates, so zombies and
    D-state processes — which consume ~0% CPU — routinely fall off the end of
    the list on busy hosts. Diagnostic scans must therefore never reuse the
    top-N view for state counting.

    The walk itself reads every /proc entry and takes tens of milliseconds on
    a busy host, so it runs in a worker thread rather than stalling the
    telemetry loop.
    """
    return await asyncio.to_thread(_scan_process_states_sync)


async def get_system_info() -> Dict[str, Any]:
    """Get system information"""
    boot_time = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot_time
    
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "boot_time": boot_time.strftime('%Y-%m-%d %H:%M:%S'),
        "uptime_seconds": int(uptime.total_seconds()),
        "uptime_str": str(uptime).split('.')[0],
        "python_version": platform.python_version()
    }


async def get_thermal_stats() -> Dict[str, Any]:
    """Read CPU/SoC temperatures, fans, and battery state.

    Uses psutil's sysfs-backed sensors where available and falls back to
    reading /sys/class/thermal directly. Returns a normalized payload so the
    dashboard renders identically regardless of which source is present.
    Failures are swallowed and reported as empty structures so the thermal
    panel degrades gracefully on hardware with no exposed sensors.
    """
    temps: List[Dict[str, Any]] = []
    fans: List[Dict[str, Any]] = []
    battery: Optional[Dict[str, Any]] = None

    # 1) psutil sensor APIs (preferred; reads hwmon/thermal sysfs on Linux)
    try:
        t = psutil.sensors_temperatures() or {}
        for label, entries in t.items():
            for e in entries:
                # psutil returns namedtuples; be defensive about attribute access
                current = getattr(e, "current", None)
                high = getattr(e, "high", None)
                critical = getattr(e, "critical", None)
                temps.append({
                    "label": label,
                    "name": getattr(e, "label", None) or label,
                    "current_c": current if current is not None else None,
                    "high_c": high if high is not None else None,
                    "critical_c": critical if critical is not None else None,
                })
    except Exception as exc:
        logger.debug("psutil.sensors_temperatures failed: %s", exc)

    try:
        f = psutil.sensors_fans() or {}
        for label, entries in f.items():
            for e in entries:
                fans.append({
                    "label": label,
                    "name": getattr(e, "label", None) or label,
                    "current_rpm": getattr(e, "current", None) if getattr(e, "current", None) else 0,
                })
    except Exception as exc:
        logger.debug("psutil.sensors_fans failed: %s", exc)

    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {
                "percent": b.percent,
                "plugged": bool(b.power_plugged),
                "seconds_left": b.secsleft if b.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except Exception as exc:
        logger.debug("psutil.sensors_battery failed: %s", exc)

    # 2) Fallback: raw /sys/class/thermal thermal_zone* (no psutil parser).
    #    Only used when psutil produced no temperature entries.
    if not temps:
        try:
            zone_root = Path("/sys/class/thermal")
            if zone_root.is_dir():
                for z in sorted(zone_root.glob("thermal_zone*")):
                    try:
                        ztype = (z / "type").read_text().strip()
                        ztemp = int((z / "temp").read_text().strip())
                        temps.append({
                            "label": "thermal_zone",
                            "name": ztype,
                            "current_c": round(ztemp / 1000.0, 1),
                            "high_c": None,
                            "critical_c": None,
                        })
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug("thermal_zone fallback failed: %s", exc)

    # 3) Fallback: /sys/class/hwmon fan*_input (no psutil parser).
    if not fans:
        try:
            hwmon_root = Path("/sys/class/hwmon")
            if hwmon_root.is_dir():
                for h in sorted(hwmon_root.glob("hwmon*")):
                    try:
                        name = (h / "name").read_text().strip()
                    except Exception:
                        name = h.name
                    for fan in sorted(h.glob("fan*_input")):
                        try:
                            rpm = int(fan.read_text().strip())
                            fans.append({"label": name, "name": fan.stem, "current_rpm": rpm})
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug("hwmon fan fallback failed: %s", exc)

    peak = max((t["current_c"] for t in temps if t["current_c"] is not None), default=None)

    def _status(temp_c):
        if temp_c is None:
            return "unknown"
        if temp_c >= 80:
            return "critical"
        if temp_c >= 70:
            return "warning"
        return "ok"

    return {
        "temperatures": temps,
        "fans": fans,
        "battery": battery,
        "peak_c": peak,
        "status": _status(peak),
        "available": bool(temps or fans),
    }


# =============================================================================
# LIBVIRT / VM SUPPORT
# =============================================================================
def _virsh_present() -> bool:
    """True when a virsh binary is actually available to execute."""
    return bool(shutil.which(VIRSH_BIN) or Path(VIRSH_BIN).exists())


_virsh_has_pkttyagent_cache: Optional[bool] = None


def _virsh_supports_no_pkttyagent() -> bool:
    """Whether this host's virsh understands ``--no-pkttyagent``.

    Added in libvirt 11.4 (2025). Older binaries reject the flag with
    ``unsupported option '--no-pkttyagent'``. The result is cached for the
    lifetime of the process because virsh binaries do not change at runtime.
    """
    global _virsh_has_pkttyagent_cache
    if _virsh_has_pkttyagent_cache is not None:
        return _virsh_has_pkttyagent_cache
    try:
        import subprocess
        result = subprocess.run(
            [VIRSH_BIN, "--help"],
            capture_output=True, text=True, timeout=5,
        )
        out = (result.stdout or "") + (result.stderr or "")
        _virsh_has_pkttyagent_cache = "--no-pkttyagent" in out
    except Exception:
        # If we cannot probe, assume the older behaviour so the retry
        # without the flag will be attempted; the sudoers policy now
        # whitelists both forms.
        _virsh_has_pkttyagent_cache = False
    return _virsh_has_pkttyagent_cache


async def _virsh_fallback_allowed() -> bool:
    """Report whether the ``virsh`` fallback path can actually run.

    As root, virsh runs directly, so only its presence matters. Otherwise we ask
    sudo to validate the exact argv we would execute.

    The previous implementation scraped ``sudo -l`` text and looked for any line
    containing both "virsh" and an action substring. That matched loosely (the
    word "start" appears in unrelated policy lines) and, worse, kept reporting
    "authorized" for a policy that whitelisted the invalid
    ``--no-ask-password`` form. Asking sudo to validate the real argv removes
    the guesswork entirely.

    Both ``--no-pkttyagent`` and legacy forms are checked because the updated
    sudoers policy whitelists both for compatibility with libvirt <11.4.
    """
    if not _virsh_present():
        return False
    if os.geteuid() == 0:
        return True

    sudo = shutil.which("sudo")
    if not sudo:
        return False

    # Probe both variants; either being allowed means the fallback can run
    # (the executor will retry without --no-pkttyagent on old virsh).
    probes = []
    probe_pkt = _virsh_command("start", "monitorx-capability-probe")
    if probe_pkt:
        probes.append(probe_pkt)
        no_pkt = _without_pkttyagent_argv(probe_pkt)
        if no_pkt:
            probes.append(no_pkt)
    for probe in probes:
        try:
            proc = await asyncio.create_subprocess_exec(
                sudo, "-n", "-l", "--", *probe[2:],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            if proc.returncode == 0:
                return True
        except (asyncio.TimeoutError, OSError):
            continue
    return False


@app.get("/api/vms/capabilities")
async def vm_capabilities():
    """Expose whether the running dashboard can control libvirt guests.

    Control works through either of two independent paths, so the UI enables
    the buttons when *either* succeeds:
      1. a read-write libvirt connection (root, or user in the 'libvirt' group)
      2. the narrowly scoped sudo policy from systemd/install-service.sh
    """
    if not LIBVIRT_AVAILABLE:
        return {
            "can_control": False,
            "can_list": False,
            "mode": "unavailable",
            "message": "libvirt is not installed on this host. Install python3-libvirt and start libvirtd to enable VM monitoring.",
        }

    # Check if connection is alive - attempt reconnect if stale
    conn_alive = await _ensure_libvirt_conn()
    list_ok = conn_alive

    if not conn_alive:
        detail = f" ({_libvirt_last_error})" if _libvirt_last_error else ""
        return {
            "can_control": False,
            "can_list": False,
            "mode": "disconnected",
            "message": f"Cannot reach libvirtd at {LIBVIRT_URI}{detail}. "
                       f"Start it with 'sudo systemctl start libvirtd', then retry.",
        }

    # Path 1: native read-write connection.
    rw_conn, rw_error = await _ensure_libvirt_rw_conn()
    if rw_conn is not None:
        mode = "root" if os.geteuid() == 0 else "libvirt-rw"
        detail = ("running as root" if os.geteuid() == 0
                  else "read-write libvirt access")
        return {
            "can_control": True,
            "can_list": list_ok,
            "mode": mode,
            "message": f"VM controls are available ({detail}).",
        }

    # Path 2: virsh fallback (direct as root, or via the sudo policy).
    if await _virsh_fallback_allowed():
        return {
            "can_control": True,
            "can_list": list_ok,
            "mode": "root" if os.geteuid() == 0 else "sudo",
            "message": "VM controls are available (virsh)." if os.geteuid() == 0
            else "VM controls are available (sudo virsh policy).",
        }

    if not _virsh_present():
        message = ("VM controls need libvirt-clients (virsh) installed, or "
                   "MonitorX's user added to the 'libvirt' group. "
                   "Run ./setup.sh and systemd/install-service.sh, then restart MonitorX.")
    else:
        message = ("VM controls need authorization. Run systemd/install-service.sh "
                   "(adds MonitorX's user to the 'libvirt' group and installs the "
                   "sudo policy), then restart MonitorX.")
    logger.info("VM control unavailable: rw connection error=%s", rw_error)

    return {
        "can_control": False,
        "can_list": list_ok,
        "mode": "unconfigured",
        "message": message,
    }


async def _resolve_domain(vm_id: str, conn=None):
    """Look up a libvirt domain by id (UUID, numeric ID, or name).

    Returns ``(domain, error_message)``. ``error_message`` is ``None`` on success.
    Runs blocking libvirt calls in a thread executor to avoid blocking the event loop.

    ``conn`` selects which connection performs the lookup. Control paths must
    pass the read-write connection, because a domain object obtained from a
    read-only connection rejects every lifecycle call with
    "operation forbidden: read only access prevents ...".
    """
    if not LIBVIRT_AVAILABLE:
        return None, "libvirt is not installed on this host."
    if not VM_ID_PATTERN.fullmatch(vm_id):
        return None, "Invalid VM identifier."

    if conn is None:
        if not await _ensure_libvirt_conn():
            return None, "libvirt connection is not available. Check that libvirtd is running."
        conn = libvirt_conn
    if not await _conn_alive_async(conn):
        return None, "libvirt connection is not available. Check that libvirtd is running."

    lookups = []
    # 1. Numeric domain id (only valid for active domains).
    if vm_id.isdigit():
        lookups.append(lambda: conn.lookupByID(int(vm_id)))
    # 2. Domain UUID.
    lookups.append(lambda: conn.lookupByUUIDString(vm_id))
    # 3. Domain name.
    lookups.append(lambda: conn.lookupByName(vm_id))

    for lookup in lookups:
        try:
            domain = await _run_libvirt(lookup, timeout=5.0)
            if domain:
                return domain, None
        except (libvirt.libvirtError, asyncio.TimeoutError):
            continue
        except Exception:
            continue

    return None, f"VM '{vm_id}' was not found."


def _without_pkttyagent_argv(cmd: List[str]) -> Optional[List[str]]:
    """Return *cmd* without ``--no-pkttyagent`` if present, else ``None``.

    ``--no-pkttyagent`` was added in libvirt 11.4 (2025-06-02) to suppress
    polkit noise. Hosts with an older virsh reject it with
    ``unsupported option '--no-pkttyagent'``. Callers that hit that error
    should transparently retry without the flag so resize and lifecycle
    controls keep working on older distros. The sudoers policy now whitelists
    *both* forms (``install-service.sh``), so the retry remains authorized.
    """
    if "--no-pkttyagent" not in cmd:
        return None
    return [c for c in cmd if c != "--no-pkttyagent"]


def _is_pkttyagent_unsupported_error(msg: str) -> bool:
    low = (msg or "").lower()
    return "unsupported option" in low and "no-pkttyagent" in low


def _virsh_command(action: str, vm_id: str) -> List[str]:
    """Build the argv for a constrained virsh lifecycle command.

    Correctness notes (these were the actual bugs):
      * ``--no-ask-password`` is a *systemctl* flag, not a virsh flag.
        virsh rejects it with "unsupported option", so every control
        command failed before it ever reached libvirtd. Just omit it.
      * ``poweroff`` is not a virsh command; the forced-stop verb is
        ``destroy`` (see VM_ACTION_TO_VIRSH).
      * ``--connect`` must be pinned. Without it, ``sudo virsh`` runs as root
        and may resolve a different default URI than the one the dashboard
        polls for inventory, so it reports "domain not found" for a guest that
        is plainly visible in the UI.
      * ``--`` terminates option parsing so a domain name is never mistaken
        for a flag.
      * ``--no-pkttyagent`` is included when the host virsh supports it
        (libvirt ≥11.4); older virsh versions reject it as
        ``unsupported option`` and callers transparently retry without it.
        The sudoers policy whitelists both variants.
    """
    verb = VM_ACTION_TO_VIRSH[action]
    args = [
        VIRSH_BIN,
        "--quiet",
        "--no-pkttyagent",
        "--connect", LIBVIRT_URI,
        verb, "--", vm_id,
    ]
    if os.geteuid() == 0:
        return args
    sudo = shutil.which("sudo")
    if not sudo:
        return []
    return [sudo, "-n", *args]


async def _run_virsh_with_retry(command: List[str], timeout: float = 30.0,
                                timeout_message: str = "virsh command timed out") -> tuple:
    """Execute a (possibly sudo-prefixed) virsh argv with a hard timeout.

    Returns ``(proc, error, returncode)``; ``proc`` is ``None`` when the
    binary could not be executed at all. Transparently retries without
    ``--no-pkttyagent`` when the host virsh predates libvirt 11.4 and rejects
    that flag (both forms are whitelisted by the installer's sudoers policy).
    """
    async def _exec(cmd: List[str]) -> tuple:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return None, f"Could not execute {cmd[0]}: file not found.", -1
        except PermissionError:
            return None, f"Could not execute {cmd[0]}: permission denied.", -1
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except (ProcessLookupError, Exception):
                pass
            return proc, timeout_message, -1
        err = (stderr.decode(errors="replace").strip()
               or stdout.decode(errors="replace").strip())
        return proc, err, proc.returncode

    proc, err, rc = await _exec(command)
    if proc is None:
        return proc, err, rc
    if rc != 0 and _is_pkttyagent_unsupported_error(err):
        retry = _without_pkttyagent_argv(command)
        if retry:
            proc2, err2, rc2 = await _exec(retry)
            if proc2 is None or rc2 == 0:
                return proc2, err2, rc2
            err, rc = err2, rc2
    return proc, err, rc


async def _run_virsh_action(action: str, vm_id: str) -> Optional[str]:
    """Run a constrained virsh command as the privileged fallback path.

    Returns ``None`` on success, or a human-readable error string on failure.
    """
    if not _virsh_present():
        return ("virsh is not installed on this host. Install the libvirt-clients "
                "package, then re-run systemd/install-service.sh.")

    command = _virsh_command(action, vm_id)
    if not command:
        return "sudo is not installed. Re-run systemd/install-service.sh to configure VM controls."

    proc, err, rc = await _run_virsh_with_retry(
        command, timeout=VM_ACTION_TIMEOUT,
        timeout_message=(f"virsh {action} timed out after {int(VM_ACTION_TIMEOUT)}s. "
                         f"The guest may be unresponsive; try Poweroff to force-stop it."),
    )
    if proc is None:
        return err
    if rc != 0:
        return _humanize_vm_error(err, action, rc)
    return None


def _humanize_vm_error(err: str, action: str, returncode: Optional[int] = None) -> str:
    """Translate raw libvirt/sudo failures into actionable operator guidance."""
    low = (err or "").lower()
    if "a password is required" in low or "sudo: a terminal is required" in low:
        return ("MonitorX is not authorized to control VMs (sudo asked for a password). "
                "Run systemd/install-service.sh, then restart MonitorX.")
    if "not allowed to execute" in low or "is not in the sudoers" in low:
        return ("MonitorX is not authorized to run virsh. "
                "Run systemd/install-service.sh, then restart MonitorX.")
    if "authentication unavailable" in low or "polkit" in low or "access denied" in low:
        return ("libvirt denied the request (polkit authentication unavailable). "
                "Add MonitorX's user to the 'libvirt' group or run "
                "systemd/install-service.sh, then restart MonitorX.")
    if "read only access" in low or "read-only" in low:
        return ("libvirt connection is read-only. Run systemd/install-service.sh "
                "to grant MonitorX read-write access, then restart MonitorX.")
    if "failed to connect to the hypervisor" in low or "refused to connect" in low:
        return ("Could not reach libvirtd. Start it with "
                "'sudo systemctl start libvirtd', then retry.")
    if "domain is already running" in low:
        return "The guest is already running."
    if "domain is not running" in low:
        return "The guest is not running."
    if "guest agent" in low or "acpi" in low:
        return (f"The guest did not accept the {action} request (no ACPI/guest-agent "
                f"support). Use Poweroff to force-stop it.")
    if err:
        return err
    return f"virsh {action} failed (exit code {returncode})."


async def _record_vm_action(vm_id: str, action: str, success: bool, message: str) -> None:
    """Append an entry to the bounded audit log used by the UI."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "vm": vm_id,
        "action": action,
        "success": success,
        "message": message,
    }
    async with _vm_action_log_lock:
        _vm_action_log.append(entry)
        if len(_vm_action_log) > _VM_ACTION_LOG_LIMIT:
            del _vm_action_log[: len(_vm_action_log) - _VM_ACTION_LOG_LIMIT]


async def _run_native_action(action: str, vm_id: str):
    """Drive the domain through the libvirt API on a read-write connection.

    This is the preferred path: it needs no sudo policy at all when MonitorX's
    user is in the ``libvirt`` group, and it reports precise libvirt errors.

    Returns ``(handled, error)``. ``handled`` is False when no read-write
    connection could be opened, signalling the caller to fall back to
    ``sudo virsh``.
    """
    conn, conn_error = await _ensure_libvirt_rw_conn()
    if conn is None:
        logger.debug("No read-write libvirt connection (%s); using virsh fallback.", conn_error)
        return False, conn_error

    # Re-resolve the domain on the READ-WRITE connection. A domain object bound
    # to the read-only connection refuses every lifecycle call.
    domain, lookup_error = await _resolve_domain(vm_id, conn=conn)
    if lookup_error or domain is None:
        return False, lookup_error

    verb = VM_ACTION_TO_VIRSH[action]
    operations = {
        "start": domain.create,
        "shutdown": domain.shutdown,
        "reboot": lambda: domain.reboot(0),
        "suspend": domain.suspend,
        "resume": domain.resume,
        "destroy": domain.destroy,
    }
    operation = operations[verb]

    try:
        await _run_libvirt(operation, timeout=VM_ACTION_TIMEOUT)
        return True, None
    except asyncio.TimeoutError:
        return True, (f"{action} timed out after {int(VM_ACTION_TIMEOUT)}s. "
                      f"The guest may be unresponsive; try Poweroff to force-stop it.")
    except libvirt.libvirtError as exc:
        message = str(exc)
        low = message.lower()
        # Permission-shaped failures are worth retrying through sudo virsh.
        if ("read only" in low or "read-only" in low or "access denied" in low
                or "polkit" in low or "authentication" in low or "permission denied" in low):
            return False, message
        return True, _humanize_vm_error(message, action)
    except Exception as exc:
        return True, _humanize_vm_error(str(exc), action)


# =============================================================================
# VM RESIZE ENDPOINT (must be before the generic /{action} route)
# =============================================================================

@app.post("/api/vms/{vm_id}/resize")
async def resize_vm(vm_id: str, payload: VMResizeRequest):
    """Resize VM CPU and/or memory via libvirt API.

    Works for both running and powered-off guests:

    * running  -> live (+ config for persistence) when possible, config-only fallback
    * powered-off -> config only
    The maximum is raised first when the requested size exceeds the current
    maximum, otherwise ``cannot set memory higher than max memory``/
    ``greater than max vcpus`` errors are unavoidable.
    """
    if not LIBVIRT_AVAILABLE:
        raise HTTPException(status_code=503, detail="libvirt is not installed on this host.")
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")
    if payload.vcpus is None and payload.memory_mb is None:
        raise HTTPException(status_code=400, detail="Provide at least one of: vcpus, memory_mb.")

    domain, lookup_error = await _resolve_domain(vm_id)
    if lookup_error:
        raise HTTPException(status_code=404, detail=lookup_error)

    # Determine whether the VM is running.
    is_running = False
    try:
        info = await _run_libvirt(domain.info, timeout=5.0)
        is_running = info[0] == libvirt.VIR_DOMAIN_RUNNING
    except Exception:
        pass

    # Helpers to obtain the *persistent* maximum from XML + info fallback.
    async def _get_domain_max(tgt_domain) -> tuple:
        """Return (max_mem_kib, max_vcpus) for *tgt_domain*."""
        max_mem = None
        max_vcpus = None
        try:
            xml = await _run_libvirt(lambda: tgt_domain.XMLDesc(0), timeout=5.0)
            root = ET.fromstring(xml)
            mem_elem = root.find("memory")
            if mem_elem is not None and mem_elem.text and mem_elem.text.strip().isdigit():
                v = int(mem_elem.text.strip())
                unit = (mem_elem.get("unit") or "KiB").strip()
                if unit == "KiB":
                    max_mem = v
                elif unit == "MiB":
                    max_mem = v * 1024
                elif unit == "GiB":
                    max_mem = v * 1024 * 1024
                elif unit == "kB":
                    max_mem = v
                else:
                    max_mem = v
            vcpu_elem = root.find("vcpu")
            if vcpu_elem is not None and vcpu_elem.text:
                try:
                    max_vcpus = int(vcpu_elem.text.strip())
                except ValueError:
                    pass
        except Exception:
            pass
        # Fallback to domain.info when XML did not yield a value.
        try:
            info2 = await _run_libvirt(tgt_domain.info, timeout=5.0)
            if max_mem is None:
                max_mem = info2[1]  # maxMem KiB — NOT info[2]
            if max_vcpus is None:
                max_vcpus = info2[3]
        except Exception:
            pass
        return max_mem, max_vcpus

    # Libvirt flag compatibility shims — VIR_DOMAIN_MEM_MAXIMUM /
    # VIR_DOMAIN_VCPU_MAXIMUM are the documented flags, but older bindings
    # expose only VIR_DOMAIN_AFFECT_MAXIMUM (same bit 4).
    VCPU_MAX = getattr(libvirt, "VIR_DOMAIN_VCPU_MAXIMUM",
                       getattr(libvirt, "VIR_DOMAIN_AFFECT_MAXIMUM", 4))
    MEM_MAX = getattr(libvirt, "VIR_DOMAIN_MEM_MAXIMUM",
                      getattr(libvirt, "VIR_DOMAIN_AFFECT_MAXIMUM", 4))

    async def _resolve_rw_domain():
        """Return a domain handle bound to the RW connection if available."""
        try:
            conn, _ = await _ensure_libvirt_rw_conn()
            if conn:
                d, _ = await _resolve_domain(vm_id, conn=conn)
                if d:
                    return d
        except Exception:
            pass
        return domain

    messages = []
    errors: List[str] = []

    # ------------------------------------------------------------------
    # vCPUs  (live + power-off, with max bump when needed)
    # ------------------------------------------------------------------
    if payload.vcpus is not None:
        tgt = await _resolve_rw_domain()

        # Step 1: raise persistent max when the request exceeds it.
        # For a running guest the live max may stay capped on some
        # hypervisors — that is swallowed and reported as a persistent-only
        # change later.
        try:
            _, max_vcpus_val = await _get_domain_max(tgt)
            # Fallback when XML parsing failed to yield a max.
            if max_vcpus_val is None:
                info_tmp = await _run_libvirt(tgt.info, timeout=5.0)
                max_vcpus_val = info_tmp[3]
            if payload.vcpus > max_vcpus_val:
                try:
                    await _run_libvirt(
                        lambda: tgt.setVcpusFlags(
                            payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_CONFIG | VCPU_MAX
                        ),
                        timeout=10.0,
                    )
                except Exception as e:
                    # Native config max failed — try virsh fallback now,
                    # otherwise the following current-set will hit
                    # "greater than max".
                    cmd = _build_virsh_modify_command(
                        "setvcpus", vm_id, str(payload.vcpus), "--maximum", "--config"
                    )
                    err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                    if err:
                        # Keep exception for outer handling but record it.
                        raise RuntimeError(f"vCPU max (config) failed: {err}") from e
                if is_running:
                    try:
                        await _run_libvirt(
                            lambda: tgt.setVcpusFlags(
                                payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_LIVE | VCPU_MAX
                            ),
                            timeout=10.0,
                        )
                    except Exception:
                        # Hypervisors that forbid LIVE|MAXIMUM (QEMU/KVM must
                        # set max via --config only) will hit this.
                        pass
        except Exception as exc:
            # Only treat as hard error when virsh also failed — otherwise the
            # current-set below will still succeed via --config.
            msg = str(exc)
            if "vCPU max" not in msg and "maximum" not in msg.lower():
                # Attempt virsh max bump as last resort.
                cmd = _build_virsh_modify_command(
                    "setvcpus", vm_id, str(payload.vcpus), "--maximum", "--config"
                )
                err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                if err:
                    errors.append(f"vCPU max: {err}")
                else:
                    # Max bump via virsh succeeded — clear the native error.
                    pass
            else:
                errors.append(f"vCPU max: {msg}")

        # Step 2: set current count.
        # Running: LIVE first, then CONFIG for persistence.
        # Powered-off: CONFIG only.
        vcpu_live_failed = False
        vcpu_live_succeeded = False
        try:
            tgt = await _resolve_rw_domain()
            if is_running:
                try:
                    await _run_libvirt(
                        lambda: tgt.setVcpusFlags(payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_LIVE),
                        timeout=10.0,
                    )
                    vcpu_live_succeeded = True
                    messages.append(f"vCPUs set to {payload.vcpus}")
                except Exception as e:
                    vcpu_live_failed = True
                    low = str(e).lower()
                    # "greater than max" here means the max bump above failed
                    # (live max is still capped). Fall through to CONFIG path.
                    if "greater than max" not in low and "max allowable" not in low:
                        raise
            if not is_running or vcpu_live_failed:
                await _run_libvirt(
                    lambda: tgt.setVcpusFlags(payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                    timeout=10.0,
                )
                if vcpu_live_failed:
                    # Live failed but persistent succeeded — still useful.
                    if not vcpu_live_succeeded:
                        messages.append(
                            f"vCPUs set to {payload.vcpus} (persistent — will take effect after reboot; "
                            f"live max is capped by the hypervisor)"
                        )
                else:
                    if not is_running:
                        messages.append(f"vCPUs set to {payload.vcpus}")
            # If running and live succeeded, also persist config for next boot.
            if is_running and vcpu_live_succeeded:
                try:
                    await _run_libvirt(
                        lambda: tgt.setVcpusFlags(payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                        timeout=10.0,
                    )
                except Exception:
                    pass
        except libvirt.libvirtError as exc:
            msg = str(exc)
            low = msg.lower()
            if ("read only" in low or "denied" in low
                    or "greater than max" in low or "max allowable" in low
                    or "operation not supported" in low
                    or "cannot set" in low):
                if is_running:
                    # Running: try LIVE via virsh, then CONFIG for persistence.
                    # At least one must succeed.
                    cmd_live = _build_virsh_modify_command("setvcpus", vm_id, str(payload.vcpus), "--live")
                    err_live = await _run_virsh_modify(cmd_live) if cmd_live else "virsh unavailable"
                    cmd_cfg = _build_virsh_modify_command("setvcpus", vm_id, str(payload.vcpus), "--config")
                    err_cfg = await _run_virsh_modify(cmd_cfg) if cmd_cfg else "virsh unavailable"
                    if err_live is None and err_cfg is None:
                        messages.append(f"vCPUs set to {payload.vcpus} (via virsh)")
                    elif err_live is None:
                        messages.append(f"vCPUs set to {payload.vcpus} (via virsh live)")
                        if err_cfg:
                            messages.append(f"vCPUs persistent config pending: {err_cfg}")
                    elif err_cfg is None:
                        messages.append(f"vCPUs set to {payload.vcpus} (persistent via virsh; reboot to apply)")
                        if err_live:
                            # Live failed likely due to max cap; not fatal when config succeeded.
                            pass
                    else:
                        errors.append(f"vCPUs: live: {err_live}; config: {err_cfg}")
                else:
                    cmd = _build_virsh_modify_command("setvcpus", vm_id, str(payload.vcpus), "--config")
                    err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                    if err:
                        errors.append(f"vCPUs: {err}")
                    else:
                        messages.append(f"vCPUs set to {payload.vcpus} (via virsh)")
            else:
                errors.append(f"vCPUs: {msg}")
        except Exception as exc:
            errors.append(f"vCPUs: {exc}")

        # Ensure virsh path covers CONFIG for running guests even when native
        # LIVE succeeded but native CONFIG silently failed — already attempted
        # above, so no further action needed.

    # ------------------------------------------------------------------
    # Memory (live balloon + power-off config, with max bump)
    # ------------------------------------------------------------------
    if payload.memory_mb is not None:
        mem_kib = payload.memory_mb * 1024
        tgt = await _resolve_rw_domain()

        # Step 1: raise persistent max when needed (must happen before current).
        # Use XML-derived maxMem; fallback to info[1] (the original code used
        # info[2] which is *current* memory, so "higher than max" was never
        # caught correctly).
        try:
            max_mem_val, _ = await _get_domain_max(tgt)
            if max_mem_val is None:
                info_tmp = await _run_libvirt(tgt.info, timeout=5.0)
                max_mem_val = info_tmp[1]
            if mem_kib > max_mem_val:
                try:
                    await _run_libvirt(
                        lambda: tgt.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_CONFIG | MEM_MAX),
                        timeout=10.0,
                    )
                except Exception as e:
                    cmd = _build_virsh_modify_command("setmaxmem", vm_id, str(mem_kib), "--config")
                    err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                    if err:
                        raise RuntimeError(f"Memory max (config) failed: {err}") from e
                if is_running:
                    try:
                        await _run_libvirt(
                            lambda: tgt.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_LIVE | MEM_MAX),
                            timeout=10.0,
                        )
                    except Exception:
                        # Live max increase often unsupported (requires guest
                        # not using NUMA, and QEMU hotplug slots). Persisted
                        # max above is sufficient for --config current.
                        pass
                    # Also attempt virsh live max for hosts where native failed
                    # but virsh supports it.
                    cmd_live = _build_virsh_modify_command("setmaxmem", vm_id, str(mem_kib), "--live")
                    # Best-effort, ignore errors.
                    if cmd_live:
                        await _run_virsh_modify(cmd_live)
        except Exception as exc:
            msg = str(exc)
            if "Memory max" not in msg:
                cmd = _build_virsh_modify_command("setmaxmem", vm_id, str(mem_kib), "--config")
                err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                if err:
                    errors.append(f"Memory max: {err}")
            else:
                errors.append(f"Memory max: {msg}")

        # Step 2: set current allocation (balloon).
        mem_live_failed = False
        mem_live_succeeded = False
        try:
            tgt = await _resolve_rw_domain()
            if is_running:
                try:
                    await _run_libvirt(
                        lambda: tgt.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_LIVE),
                        timeout=10.0,
                    )
                    mem_live_succeeded = True
                    messages.append(f"Memory set to {payload.memory_mb} MiB")
                except Exception as e:
                    mem_live_failed = True
                    low = str(e).lower()
                    if "greater than max" not in low and "cannot set memory higher than max" not in low:
                        raise
            if not is_running or mem_live_failed:
                await _run_libvirt(
                    lambda: tgt.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                    timeout=10.0,
                )
                if mem_live_failed and not mem_live_succeeded:
                    messages.append(
                        f"Memory set to {payload.memory_mb} MiB (persistent — will take effect after reboot; "
                        f"live max is capped or balloon unavailable)"
                    )
                elif not is_running:
                    messages.append(f"Memory set to {payload.memory_mb} MiB")
            if is_running and mem_live_succeeded:
                # Persist for next boot as well.
                try:
                    await _run_libvirt(
                        lambda: tgt.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                        timeout=10.0,
                    )
                except Exception:
                    pass
        except libvirt.libvirtError as exc:
            msg = str(exc)
            low = msg.lower()
            if ("read only" in low or "denied" in low
                    or "greater than max" in low or "cannot set memory higher than max" in low
                    or "max allowable" in low or "operation not supported" in low
                    or "cannot set" in low):
                if is_running:
                    cmd_live = _build_virsh_modify_command("setmem", vm_id, str(mem_kib), "--live")
                    err_live = await _run_virsh_modify(cmd_live) if cmd_live else "virsh unavailable"
                    cmd_cfg = _build_virsh_modify_command("setmem", vm_id, str(mem_kib), "--config")
                    err_cfg = await _run_virsh_modify(cmd_cfg) if cmd_cfg else "virsh unavailable"
                    if err_live is None and err_cfg is None:
                        messages.append(f"Memory set to {payload.memory_mb} MiB (via virsh)")
                    elif err_live is None:
                        messages.append(f"Memory set to {payload.memory_mb} MiB (via virsh live)")
                    elif err_cfg is None:
                        messages.append(f"Memory set to {payload.memory_mb} MiB (persistent via virsh; reboot to apply)")
                    else:
                        errors.append(f"Memory: live: {err_live}; config: {err_cfg}")
                else:
                    cmd = _build_virsh_modify_command("setmem", vm_id, str(mem_kib), "--config")
                    err = await _run_virsh_modify(cmd) if cmd else "virsh unavailable"
                    if err:
                        errors.append(f"Memory: {err}")
                    else:
                        messages.append(f"Memory set to {payload.memory_mb} MiB (via virsh)")
            else:
                errors.append(f"Memory: {msg}")
        except Exception as exc:
            errors.append(f"Memory: {exc}")

    result_msg = "; ".join(messages) or "Resize requested"
    success = not errors
    if errors:
        result_msg = (result_msg + "; " if result_msg else "") + "Errors: " + "; ".join(errors)
    await _record_vm_action(vm_id, "resize", success, result_msg)
    return {"success": success, "message": result_msg, "vm_id": vm_id}


@app.post("/api/vms/{vm_id}/{action}")
async def control_vm(vm_id: str, action: str, payload: Optional[VMActionRequest] = None):
    """Perform a libvirt domain action (start, shutdown, poweroff, reboot, ...).

    Control is attempted natively through a read-write libvirt connection and
    falls back to a narrowly scoped ``sudo virsh`` invocation, so the buttons
    work whether MonitorX runs as root, as a member of the ``libvirt`` group,
    or as an unprivileged user with the installer's sudo policy.
    """
    if action not in VM_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action '{action}'. Valid: {list(VM_ACTIONS)}",
        )

    # Destructive actions require explicit confirmation from the UI.
    if action in VM_ACTIONS_DESTRUCTIVE and not (payload and payload.confirm):
        raise HTTPException(
            status_code=400,
            detail=f"The '{action}' action is destructive and requires an explicit confirmation payload.",
        )

    if not LIBVIRT_AVAILABLE:
        raise HTTPException(status_code=503, detail="libvirt is not installed on this host.")
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")

    # Sanity check: surface libvirt state so the UI can decide before sending
    # graceful vs. destructive commands (e.g. "shutdown" is a no-op on a stopped VM).
    domain, lookup_error = await _resolve_domain(vm_id)
    if lookup_error:
        await _record_vm_action(vm_id, action, False, lookup_error)
        status = 503 if "connection is not available" in lookup_error else 404
        raise HTTPException(status_code=status, detail=lookup_error)

    try:
        info = await _run_libvirt(domain.info, timeout=5.0)
        current_state = info[0]
    except Exception as exc:
        await _record_vm_action(vm_id, action, False, f"Could not read domain state: {exc}")
        raise HTTPException(status_code=503, detail=f"Could not read VM state: {exc}")

    # Skip no-ops so the UI does not log a misleading failure.
    stopped_states = (libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_CRASHED)
    if action == "start" and current_state == libvirt.VIR_DOMAIN_RUNNING:
        msg = f"VM '{vm_id}' is already running."
        await _record_vm_action(vm_id, action, True, msg)
        return {"success": True, "message": msg, "state": "running", "noop": True}
    if action in ("shutdown", "reboot", "poweroff", "destroy") and current_state in stopped_states:
        msg = f"VM '{vm_id}' is already stopped."
        await _record_vm_action(vm_id, action, True, msg)
        return {"success": True, "message": msg, "state": "shutoff", "noop": True}
    if action == "suspend" and current_state == libvirt.VIR_DOMAIN_PAUSED:
        msg = f"VM '{vm_id}' is already paused."
        await _record_vm_action(vm_id, action, True, msg)
        return {"success": True, "message": msg, "state": "paused", "noop": True}
    if action == "resume" and current_state == libvirt.VIR_DOMAIN_RUNNING:
        msg = f"VM '{vm_id}' is already running."
        await _record_vm_action(vm_id, action, True, msg)
        return {"success": True, "message": msg, "state": "running", "noop": True}

    # Reject transitions libvirt cannot satisfy, with a clear explanation
    # instead of a raw driver error.
    if action == "resume" and current_state not in (
        libvirt.VIR_DOMAIN_PAUSED, libvirt.VIR_DOMAIN_PMSUSPENDED
    ):
        detail = f"VM '{vm_id}' is not paused, so it cannot be resumed."
        await _record_vm_action(vm_id, action, False, detail)
        raise HTTPException(status_code=409, detail=detail)
    if action in ("suspend", "shutdown", "reboot") and current_state != libvirt.VIR_DOMAIN_RUNNING:
        detail = f"VM '{vm_id}' is not running, so it cannot be {action}ed."
        await _record_vm_action(vm_id, action, False, detail)
        raise HTTPException(status_code=409, detail=detail)

    # 1) Preferred: native libvirt API over a read-write connection.
    handled, error = await _run_native_action(action, vm_id)
    used_fallback = False

    # 2) Fallback: narrowly scoped `sudo virsh` (unprivileged deployments).
    if not handled:
        used_fallback = True
        error = await _run_virsh_action(action, vm_id)

    if error:
        await _record_vm_action(vm_id, action, False, error)
        low = error.lower()
        status = 403 if ("not authorized" in low or "denied" in low or "polkit" in low) else 502
        raise HTTPException(status_code=status, detail=error)

    friendly = {
        "start": "started", "shutdown": "shut down", "reboot": "rebooted",
        "poweroff": "powered off", "destroy": "force-stopped",
        "suspend": "suspended", "resume": "resumed",
    }[action]

    # Report the post-action state so the UI can refresh with confidence.
    # `shutdown`/`reboot` are asynchronous requests to the guest OS: virsh
    # returns immediately and the guest may take a while to actually stop.
    new_state = await _read_domain_state(vm_id)
    pending = action in ("shutdown", "reboot") and new_state == "running"
    if pending:
        message = (f"{friendly.capitalize()} request sent to '{vm_id}'. "
                   f"The guest OS is completing the operation.")
    else:
        message = f"VM '{vm_id}' {friendly} successfully."

    await _record_vm_action(vm_id, action, True, message)
    return {
        "success": True,
        "message": message,
        "state": new_state,
        "pending": pending,
        "via": "virsh" if used_fallback else "libvirt",
    }


async def _read_domain_state(vm_id: str) -> Optional[str]:
    """Best-effort read of a domain's current state name after an action."""
    try:
        domain, error = await _resolve_domain(vm_id)
        if error or domain is None:
            return None
        info = await _run_libvirt(domain.info, timeout=5.0)
        return VM_STATE_NAMES.get(info[0], "unknown")
    except Exception:
        return None


@app.get("/api/vms/log")
async def vm_action_log(limit: int = Query(20, ge=1, le=_VM_ACTION_LOG_LIMIT)):
    """Return the most recent VM control actions, newest first."""
    async with _vm_action_log_lock:
        recent = list(reversed(_vm_action_log[-limit:]))
    return {"entries": recent, "total": len(_vm_action_log)}


# ==============================================================================
# VM GUEST INSIGHTS — processes, logged-in users, and root disk on every VM
#
# Hypervisor metrics (vCPU/RAM/disk-IO rates) cannot see *inside* a guest, so
# this feature inspects each VM over SSH and surfaces its process table, user
# sessions/accounts, and root-filesystem usage in the dashboard.
#
# SECURITY MODEL
# --------------
# * No shell is ever invoked. Commands run via asyncio.create_subprocess_exec
#   with a fixed argv; a shell interpreter appears nowhere in this code path.
# * Only four read-only commands are executed on guests, and their argv is
#   built exclusively from the INSIGHTS_CMD_* constants below. Operator input
#   (host/user/port/key) never becomes part of a remote command line — it is
#   strictly validated and placed only in its designated argv slot.
# * ssh is hardened with BatchMode + disabled password/keyboard auth, so it
#   can neither prompt nor hang waiting for interactive input.
# * Every execution has a hard timeout, a capped output read (a hostile or
#   broken guest cannot exhaust memory), and a global concurrency semaphore.
# * Connection profiles persist in the private state directory (0600, atomic
#   replace, O_NOFOLLOW on read) and never contain secrets — an identity
#   *file path*, not key material.
# * The feature is read-only by design: nothing here can modify a guest.
# ==============================================================================

INSIGHTS_SSH_TIMEOUT = max(float(os.environ.get("MONITORX_INSIGHTS_SSH_TIMEOUT", "12")), 3.0)
INSIGHTS_CACHE_TTL = max(float(os.environ.get("MONITORX_INSIGHTS_TTL", "5")), 1.0)
INSIGHTS_OVERVIEW_TTL = max(float(os.environ.get("MONITORX_INSIGHTS_OVERVIEW_TTL", "10")), 2.0)
INSIGHTS_SSH_CONCURRENCY = min(max(int(os.environ.get("MONITORX_INSIGHTS_CONCURRENCY", "6")), 1), 32)
INSIGHTS_MAX_OUTPUT = 512 * 1024          # bytes read per guest command
INSIGHTS_MAX_ERROR = 4096                 # stderr retained for diagnostics
INSIGHTS_MAX_PROCESSES = 800
INSIGHTS_MAX_SESSIONS = 200
INSIGHTS_MAX_ACCOUNTS = 500
INSIGHTS_MAX_FILESYSTEMS = 100

# The ONLY commands ever executed on a guest. Fixed argv, read-only tools,
# header-free machine-readable output where available.
INSIGHTS_CMD_PROCESSES = ("ps", "-eo", "pid=,ppid=,user=,pcpu=,pmem=,rss=,etime=,comm=")
INSIGHTS_CMD_SESSIONS = ("who",)
INSIGHTS_CMD_ACCOUNTS = ("getent", "passwd")
INSIGHTS_CMD_FILESYSTEMS = ("df", "-kP")

# Pseudo/virtual devices reported by ``df`` that are never real root disks.
INSIGHTS_PSEUDO_DEVICES = {
    "tmpfs", "devtmpfs", "udev", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
    "mqueue", "hugetlbfs", "debugfs", "tracefs", "fusectl", "configfs",
    "securityfs", "pstore", "bpf", "autofs", "binfmt_misc", "rpc_pipefs",
    "nsfs", "ramfs", "fuse.lxcfs", "systemd-1", "selinuxfs", "efivarfs",
}

_INSIGHTS_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_INSIGHTS_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# Cache of recent collections so UI polling cannot hammer guests.
_insights_cache: Dict[str, Dict[str, Any]] = {}
_insights_overview_cache: Dict[str, Any] = {}
_insights_config_lock = asyncio.Lock()
_insights_ssh_semaphore = asyncio.Semaphore(INSIGHTS_SSH_CONCURRENCY)


def _insights_config_path() -> Path:
    """Location of the per-VM SSH profiles inside the private state dir."""
    return Path(os.environ.get(
        "MONITORX_INSIGHTS_CONFIG",
        str(_ensure_state_dir() / "vm-insights-config.json"),
    ))


def _validate_insights_host(host: str) -> str:
    """Accept an IPv4/IPv6 literal or RFC-1123 hostname; reject everything else.

    The value only ever lands in the ``user@host`` argv slot, but it is still
    validated hard: whitespace, control characters, shell metacharacters and
    leading dashes (option injection) are all refused by construction.
    """
    value = str(host or "")
    if not value or value != value.strip():
        raise ValueError("Host must not be empty or contain surrounding whitespace.")
    if len(value) > 253:
        raise ValueError("Host must be a non-empty address (max 253 chars).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Host contains control characters.")
    if value.startswith("-"):
        raise ValueError("Host must not start with '-'.")
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(candidate)
        return value
    except ValueError:
        pass
    if not _INSIGHTS_HOSTNAME_RE.fullmatch(candidate):
        raise ValueError("Host must be an IP address or a valid hostname (letters, digits, '.', '-').")
    return value


def _validate_insights_user(user: str) -> str:
    value = str(user or "")
    if not value or value != value.strip():
        raise ValueError("SSH user must not be empty or contain surrounding whitespace.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("SSH user contains control characters.")
    if not _INSIGHTS_USER_RE.fullmatch(value):
        raise ValueError("SSH user must match [A-Za-z0-9][A-Za-z0-9._-]{0,31}.")
    return value


def _validate_identity_file(path: Optional[str]) -> Optional[str]:
    """Validate a private-key *path* on the MonitorX host (never key data)."""
    if path is None or not str(path).strip():
        return None
    value = str(path).strip()
    if len(value) > 4096:
        raise ValueError("Identity file path is too long.")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Identity file path contains control characters.")
    resolved = Path(value).expanduser()
    if not resolved.is_file():
        raise ValueError(f"Identity file does not exist: {resolved}")
    return str(resolved)


class VmInsightsConfigRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="root", min_length=1, max_length=32)
    identity_file: Optional[str] = Field(default=None, max_length=4096)


def _load_insights_configs() -> Dict[str, Dict[str, Any]]:
    """Read the profile store. O_NOFOLLOW: a planted symlink aborts the read
    instead of redirecting it to a file of an attacker's choosing."""
    path = _insights_config_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    configs: Dict[str, Dict[str, Any]] = {}
    for key, entry in data.items():
        if isinstance(key, str) and isinstance(entry, dict) and isinstance(entry.get("host"), str):
            configs[key[:128]] = entry
    return configs


async def _save_insights_configs(configs: Dict[str, Dict[str, Any]]) -> None:
    """Atomically persist the profile store with 0600 permissions."""
    path = _insights_config_path()
    _ensure_state_dir()
    payload = json.dumps(configs, indent=2, sort_keys=True)

    def _write() -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    await asyncio.get_running_loop().run_in_executor(None, _write)


def _ssh_binary() -> str:
    return shutil.which("ssh") or "/usr/bin/ssh"


def _vm_insights_ssh_argv(config: Dict[str, Any], remote_command) -> List[str]:
    """Build the exact argv for one guest inspection.

    Only constants plus pre-validated profile fields are interpolated. The
    remote command comes from the INSIGHTS_CMD_* allowlist and is appended
    verbatim — ssh joins it for the guest's shell, which is safe because it
    contains no operator-controlled data whatsoever.
    """
    argv = [
        _ssh_binary(),
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "PubkeyAuthentication=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UpdateHostKeys=no",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1",
        "-o", "LogLevel=ERROR",
    ]
    identity = config.get("identity_file")
    if identity:
        argv += ["-o", "IdentitiesOnly=yes", "-i", str(identity)]
    argv += ["-p", str(int(config["port"])), f"{config['user']}@{config['host']}"]
    argv += list(remote_command)
    return argv


async def _read_stream_capped(stream, cap: int):
    """Read at most ``cap`` bytes so a guest cannot exhaust host memory."""
    chunks = []
    total = 0
    while total < cap:
        chunk = await stream.read(min(65536, cap - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), total >= cap


async def _run_insights_ssh(config: Dict[str, Any], remote_command) -> Dict[str, Any]:
    """Run one allowlisted read-only command inside a guest over SSH.

    Returns ``{"returncode", "stdout", "stderr", "truncated", "timed_out"}``.
    """
    argv = _vm_insights_ssh_argv(config, remote_command)
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    async def _collect():
        out_task = asyncio.ensure_future(_read_stream_capped(proc.stdout, INSIGHTS_MAX_OUTPUT))
        err_task = asyncio.ensure_future(_read_stream_capped(proc.stderr, INSIGHTS_MAX_ERROR))
        await asyncio.gather(out_task, err_task)
        await proc.wait()
        return out_task.result(), err_task.result()

    try:
        (stdout_b, out_capped), (stderr_b, _err_capped) = await asyncio.wait_for(
            _collect(), timeout=INSIGHTS_SSH_TIMEOUT
        )
        return {
            "returncode": proc.returncode if proc.returncode is not None else -1,
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "truncated": out_capped,
            "timed_out": False,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"Command timed out after {INSIGHTS_SSH_TIMEOUT:.0f}s.",
            "truncated": False,
            "timed_out": True,
        }


def _insights_error(result: Dict[str, Any]) -> Optional[str]:
    """Translate an SSH result into an operator-friendly error, or None."""
    if result["timed_out"]:
        return result["stderr"] or "SSH command timed out."
    if result["returncode"] != 0:
        detail = (result["stderr"] or result["stdout"] or "").strip().splitlines()
        tail = detail[-1][:300] if detail else f"exit code {result['returncode']}"
        if result["returncode"] == 255:
            return (f"SSH connection failed: {tail}. Check the host/port/user, "
                    f"that sshd is running in the guest, and that MonitorX's "
                    f"public key is authorized there.")
        return f"Guest command exited with code {result['returncode']}: {tail}"
    return None


# --- Parsers (pure functions; hostile guest output must never crash us) -----

def _parse_ps_table(text: str, limit: int = INSIGHTS_MAX_PROCESSES) -> Dict[str, Any]:
    """Parse ``ps -eo pid=,ppid=,user=,pcpu=,pmem=,rss=,etime=,comm=`` output."""
    rows = []
    truncated = False
    for line in text.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid_s, ppid_s, user, pcpu_s, pmem_s, rss_s, etime, comm = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
            rss_kb = max(int(rss_s), 0)
        except ValueError:
            continue
        try:
            cpu = min(max(float(pcpu_s), 0.0), 100000.0)
        except ValueError:
            cpu = 0.0
        try:
            mem = min(max(float(pmem_s), 0.0), 100.0)
        except ValueError:
            mem = 0.0
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "user": user[:64],
            "cpu_percent": cpu,
            "memory_percent": mem,
            "memory_mb": round(rss_kb / 1024.0, 1),
            "etime": etime[:32],
            "name": comm[:256],
        })
        if len(rows) >= limit:
            truncated = True
            break
    rows.sort(key=lambda r: r["cpu_percent"], reverse=True)
    return {"processes": rows, "truncated": truncated}


_WHO_LINE_RE = re.compile(r"^(?P<user>\S+)\s+(?P<tty>\S+)\s+(?P<when>.+?)(?:\s*\((?P<source>[^)]*)\))?\s*$")


def _parse_who_sessions(text: str, limit: int = INSIGHTS_MAX_SESSIONS) -> Dict[str, Any]:
    """Parse ``who`` output into login sessions (user, tty, when, source)."""
    sessions = []
    for line in text.splitlines():
        if len(sessions) >= limit:
            break
        match = _WHO_LINE_RE.fullmatch(line.strip())
        if not match:
            continue
        sessions.append({
            "user": match.group("user")[:64],
            "tty": match.group("tty")[:64],
            "login_time": (match.group("when") or "").strip()[:64],
            "from": (match.group("source") or "").strip()[:128],
        })
    return {"sessions": sessions, "truncated": len(sessions) >= limit}


def _parse_passwd_accounts(text: str, limit: int = INSIGHTS_MAX_ACCOUNTS) -> Dict[str, Any]:
    """Parse ``getent passwd``; report human users (uid 0 or >= 1000)."""
    accounts = []
    total = 0
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) < 7:
            continue
        total += 1
        try:
            uid = int(fields[2])
        except ValueError:
            continue
        if uid != 0 and uid < 1000:
            continue  # system/service account
        if len(accounts) < limit:
            accounts.append({
                "name": fields[0][:64],
                "uid": uid,
                "home": fields[5][:256],
                "shell": fields[6][:128],
            })
    accounts.sort(key=lambda a: a["uid"])
    return {"accounts": accounts, "total_entries": total, "truncated": len(accounts) >= limit}


def _parse_df_filesystems(text: str, limit: int = INSIGHTS_MAX_FILESYSTEMS) -> Dict[str, Any]:
    """Parse ``df -kP`` (POSIX one-line-per-fs) into filesystem records.

    The root filesystem is the entry mounted at ``/``; when a guest has none
    (rare), the first real (non-pseudo) filesystem is used as the fallback.
    """
    filesystems = []
    for index, line in enumerate(text.splitlines()):
        if index == 0 and line.split()[:1] == ["Filesystem"]:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        device, size_s, used_s, avail_s, capacity, mountpoint = parts
        try:
            size_kb = max(int(size_s), 0)
            used_kb = max(int(used_s), 0)
            avail_kb = max(int(avail_s), 0)
            percent = int(capacity.rstrip("%")) if capacity.rstrip("%").isdigit() else 0
        except ValueError:
            continue
        device_short = device.split("/")[-1][:64] if "/" in device else device[:64]
        filesystems.append({
            "device": device[:256],
            "device_short": device_short,
            "mountpoint": mountpoint[:256],
            "size_kb": size_kb,
            "used_kb": used_kb,
            "avail_kb": avail_kb,
            "percent": min(max(percent, 0), 100),
            "pseudo": device.split(":")[0].lower() in INSIGHTS_PSEUDO_DEVICES,
        })
        if len(filesystems) >= limit:
            break
    root = next((fs for fs in filesystems if fs["mountpoint"] == "/"), None)
    if root is None:
        root = next((fs for fs in filesystems if not fs["pseudo"]), None)
    return {"root": root, "filesystems": filesystems}


# --- Section probes ----------------------------------------------------------

async def _probe_guest_processes(config: Dict[str, Any]) -> Dict[str, Any]:
    result = await _run_insights_ssh(config, INSIGHTS_CMD_PROCESSES)
    error = _insights_error(result)
    if error:
        return {"ok": False, "error": error}
    parsed = _parse_ps_table(result["stdout"])
    return {
        "ok": True,
        "count": len(parsed["processes"]),
        "truncated": parsed["truncated"] or result["truncated"],
        "processes": parsed["processes"],
    }


async def _probe_guest_users(config: Dict[str, Any]) -> Dict[str, Any]:
    sessions_res, accounts_res = await asyncio.gather(
        _run_insights_ssh(config, INSIGHTS_CMD_SESSIONS),
        _run_insights_ssh(config, INSIGHTS_CMD_ACCOUNTS),
    )
    sessions_error = _insights_error(sessions_res)
    accounts_error = _insights_error(accounts_res)
    if sessions_error and accounts_error:
        return {"ok": False, "error": sessions_error}
    sessions = _parse_who_sessions(sessions_res["stdout"]) if not sessions_error else {"sessions": [], "truncated": False}
    accounts = _parse_passwd_accounts(accounts_res["stdout"]) if not accounts_error else {"accounts": [], "total_entries": 0, "truncated": False}
    return {
        "ok": True,
        "sessions": sessions["sessions"],
        "sessions_truncated": sessions["truncated"],
        "accounts": accounts["accounts"],
        "account_entries_total": accounts["total_entries"],
        "accounts_truncated": accounts["truncated"],
        **({"sessions_error": sessions_error} if sessions_error else {}),
        **({"accounts_error": accounts_error} if accounts_error else {}),
    }


async def _probe_guest_root_disk(config: Dict[str, Any]) -> Dict[str, Any]:
    result = await _run_insights_ssh(config, INSIGHTS_CMD_FILESYSTEMS)
    error = _insights_error(result)
    if error:
        return {"ok": False, "error": error}
    parsed = _parse_df_filesystems(result["stdout"])
    if parsed["root"] is None:
        return {"ok": False, "error": "The guest reported no root filesystem (no mount at '/')."}
    return {
        "ok": True,
        "root": parsed["root"],
        "filesystems": parsed["filesystems"],
        "truncated": result["truncated"],
    }


async def _resolve_insights_config(vm_id: str) -> Optional[Dict[str, Any]]:
    if not VM_ID_PATTERN.fullmatch(vm_id):
        return None
    configs = _load_insights_configs()
    config = configs.get(vm_id)
    if not isinstance(config, dict) or not config.get("host"):
        return None
    return config


async def collect_vm_insights(vm_id: str, force: bool = False) -> Dict[str, Any]:
    """Collect all three guest sections concurrently, with a short cache."""
    now = time.monotonic()
    cached = _insights_cache.get(vm_id)
    if not force and cached and now - cached["at"] < INSIGHTS_CACHE_TTL:
        return cached["payload"]

    config = await _resolve_insights_config(vm_id)
    if config is None:
        payload = {
            "vm": vm_id,
            "configured": False,
            "error": "No SSH profile is configured for this VM yet. "
                     "Save a connection profile in the Insights panel first.",
        }
        return payload

    async with _insights_ssh_semaphore:
        processes, users, root_disk = await asyncio.gather(
            _probe_guest_processes(config),
            _probe_guest_users(config),
            _probe_guest_root_disk(config),
            return_exceptions=True,
        )

    def _section(value, fallback_error):
        if isinstance(value, Exception):
            logger.warning("Guest insights probe failed for %s: %s", vm_id, value)
            return {"ok": False, "error": fallback_error}
        return value

    payload = {
        "vm": vm_id,
        "configured": True,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "host": config.get("host"),
        "processes": _section(processes, "Process collection failed unexpectedly."),
        "users": _section(users, "User collection failed unexpectedly."),
        "root_disk": _section(root_disk, "Disk collection failed unexpectedly."),
    }
    _insights_cache[vm_id] = {"at": now, "payload": payload}
    return payload


async def _discover_vm_addresses(vm_id: str) -> List[str]:
    """Best-effort guest IP discovery via libvirt (agent first, DHCP leases
    second). Empty list when libvirt or the guest provides nothing."""
    if not LIBVIRT_AVAILABLE or not VM_ID_PATTERN.fullmatch(vm_id):
        return []
    domain, error = await _resolve_domain(vm_id)
    if error or domain is None:
        return []

    def _addresses() -> List[str]:
        found: List[str] = []
        sources = [
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
        ]
        for source in sources:
            try:
                interfaces = domain.interfaceAddresses(source) or {}
            except Exception:
                continue
            for info in interfaces.values():
                for addr in (info or {}).get("addrs", []):
                    ip = str(addr.get("addr") or "").strip()
                    if not ip or ip.lower().startswith("fe80:"):
                        continue
                    if ip not in found:
                        found.append(ip)
            if found:
                break
        return found[:8]

    try:
        return await _run_libvirt(_addresses, timeout=6.0)
    except Exception:
        return []


@app.get("/api/vms/insights")
async def vms_insights_overview(force: bool = Query(False)):
    """Fleet view: root-disk usage, user sessions, and process counts for
    every VM with an SSH profile, collected concurrently and cached."""
    now = time.monotonic()
    cached = _insights_overview_cache.get("payload")
    if not force and cached and now - _insights_overview_cache.get("at", 0) < INSIGHTS_OVERVIEW_TTL:
        return cached

    configs = _load_insights_configs()
    if not configs:
        payload = {"vms": [], "configured": 0, "message": "No VMs have an Insights SSH profile yet. Open 'Insights' on a running VM card to connect it."}
        _insights_overview_cache.update({"at": now, "payload": payload})
        return payload

    results = await asyncio.gather(
        *(collect_vm_insights(vm_id, force=force) for vm_id in configs),
        return_exceptions=True,
    )
    vms = []
    for vm_id, result in zip(configs, results):
        entry: Dict[str, Any] = {"vm": vm_id, "vm_name": str(configs[vm_id].get("vm_name") or vm_id)}
        if isinstance(result, Exception) or not isinstance(result, dict):
            entry.update({"ok": False, "error": "Insights collection failed unexpectedly."})
        elif not result.get("configured"):
            entry.update({"ok": False, "error": result.get("error", "Not configured.")})
        else:
            entry["collected_at"] = result.get("collected_at")
            section_errors = []
            for key in ("processes", "users", "root_disk"):
                section = result.get(key) or {}
                if not section.get("ok"):
                    entry[key] = {"ok": False, "error": section.get("error", "unavailable")}
                    section_errors.append(entry[key]["error"])
                    continue
                if key == "processes":
                    entry[key] = {"ok": True, "count": section.get("count", 0)}
                elif key == "users":
                    entry[key] = {"ok": True, "sessions": len(section.get("sessions", [])), "accounts": len(section.get("accounts", []))}
                else:
                    root = section.get("root") or {}
                    entry[key] = {"ok": True, "device": root.get("device"), "mountpoint": root.get("mountpoint"),
                                  "percent": root.get("percent"), "size_kb": root.get("size_kb"),
                                  "used_kb": root.get("used_kb"), "avail_kb": root.get("avail_kb")}
            # A guest is only "live" when at least one section succeeded;
            # otherwise the fleet view must show it as unreachable.
            if len(section_errors) == 3:
                entry.update({"ok": False, "error": section_errors[0]})
            else:
                entry["ok"] = True
        vms.append(entry)
    payload = {"vms": vms, "configured": len(configs)}
    _insights_overview_cache.update({"at": now, "payload": payload})
    return payload


@app.get("/api/vms/{vm_id}/insights")
async def vm_insights(vm_id: str, force: bool = Query(False)):
    """Processes, logged-in users/accounts, and root-disk usage for one VM."""
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")
    return await collect_vm_insights(vm_id, force=force)


@app.get("/api/vms/{vm_id}/insights/config")
async def get_vm_insights_config(vm_id: str):
    """Return the stored SSH profile for a VM plus discovered guest IPs."""
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")
    configs = _load_insights_configs()
    config = configs.get(vm_id)
    return {
        "vm": vm_id,
        "configured": bool(config),
        "config": {
            "host": config.get("host"),
            "port": config.get("port", 22),
            "user": config.get("user"),
            "identity_file": config.get("identity_file"),
        } if config else None,
        "discovered_addresses": await _discover_vm_addresses(vm_id),
        "ssh_available": shutil.which("ssh") is not None,
    }


@app.put("/api/vms/{vm_id}/insights/config")
async def put_vm_insights_config(vm_id: str, payload: VmInsightsConfigRequest):
    """Validate and store the SSH profile used to inspect this VM."""
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")
    try:
        host = _validate_insights_host(payload.host)
        user = _validate_insights_user(payload.user)
        identity_file = _validate_identity_file(payload.identity_file)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    entry = {"host": host, "port": int(payload.port), "user": user, "identity_file": identity_file}

    # Best-effort friendly name for the fleet overview (libvirt may be down).
    try:
        domain, error = await _resolve_domain(vm_id)
        if not error and domain is not None:
            entry["vm_name"] = await _run_libvirt(lambda: domain.name(), timeout=5.0)
    except Exception:
        pass

    async with _insights_config_lock:
        configs = _load_insights_configs()
        configs[vm_id] = entry
        await _save_insights_configs(configs)
    _insights_cache.pop(vm_id, None)
    _append_audit_line("vm-insights-config", f"saved SSH profile for {vm_id} ({user}@{host}:{payload.port})")
    return {"vm": vm_id, "configured": True, "config": {**entry, "vm_name": entry.get("vm_name")}}


@app.delete("/api/vms/{vm_id}/insights/config")
async def delete_vm_insights_config(vm_id: str):
    """Forget the SSH profile for a VM."""
    if not VM_ID_PATTERN.fullmatch(vm_id):
        raise HTTPException(status_code=400, detail="Invalid VM identifier.")
    async with _insights_config_lock:
        configs = _load_insights_configs()
        if vm_id not in configs:
            raise HTTPException(status_code=404, detail="No Insights profile is stored for this VM.")
        del configs[vm_id]
        await _save_insights_configs(configs)
    _insights_cache.pop(vm_id, None)
    _append_audit_line("vm-insights-config", f"removed SSH profile for {vm_id}")
    return {"vm": vm_id, "configured": False}


def _build_virsh_modify_command(subcmd: str, vm_id: str, *args) -> List[str]:
    """Build a virsh command for domain modification via the fallback path.

    The argv shape must stay in sync with the sudoers policy installed by
    systemd/install-service.sh (``virsh --quiet [--no-pkttyagent] --connect
    <URI> <subcmd> <domain> …``). ``--no-pkttyagent`` is included for
    libvirt ≥11.4 and transparently stripped on retry when the host virsh
    rejects it as ``unsupported option``; both variants are whitelisted.
    """
    base = [VIRSH_BIN, "--quiet", "--no-pkttyagent", "--connect", LIBVIRT_URI,
            subcmd, vm_id, *args]
    if os.geteuid() == 0:
        return base
    sudo = shutil.which("sudo")
    if not sudo:
        return []
    return [sudo, "-n", *base]


async def _run_virsh_modify(command: List[str]) -> Optional[str]:
    """Run a virsh modify command and return error string or None on success.

    Transparently retries without ``--no-pkttyagent`` when the host virsh is
    older than 11.4 and rejects that flag. Both forms are allowed by the
    updated sudoers policy.
    """
    if not command:
        return "sudo/virsh not available"
    proc, err, rc = await _run_virsh_with_retry(command, timeout=30)
    if proc is None:
        return err
    if rc != 0:
        return err or f"virsh command failed (exit code {rc})"
    return None



async def get_vm_stats() -> Optional[List[Dict[str, Any]]]:
    """Return libvirt domain inventory and live metrics for running KVM guests.

    Libvirt exposes CPU time and I/O counters cumulatively, therefore rates are
    derived from two successive samples. Values are zero on the first poll.

    Every libvirt call — including the per-domain reads — runs in a thread
    executor with a timeout to prevent the async event loop from hanging.
    """
    if not LIBVIRT_AVAILABLE:
        return None

    # Ensure connection is alive before attempting operations
    if not await _ensure_libvirt_conn():
        return None

    # Run blocking libvirt call in thread executor with timeout
    global libvirt_conn
    try:
        conn = libvirt_conn
        domains = await _run_libvirt(lambda: conn.listAllDomains(0), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Timed out listing libvirt domains")
        return None
    except libvirt.libvirtError as exc:
        # Drop the handle so the next poll dials a fresh connection instead of
        # reusing a socket that libvirtd already closed.
        logger.warning("Could not list libvirt domains: %s", exc)
        libvirt_conn = None
        return None
    except Exception as exc:
        logger.warning("Could not list libvirt domains: %s", exc)
        return None

    async with vm_metrics_lock:
        now = time.monotonic()

        vms: List[Dict[str, Any]] = []
        active_domain_ids = set()
        for domain in domains:
            try:
                # All blocking libvirt reads for one domain run together in the
                # executor so a slow guest cannot stall the event loop.
                vm = await _run_libvirt(
                    lambda d=domain: _collect_domain_snapshot(d, VM_STATE_NAMES),
                    timeout=10.0,
                )
                if vm is None:
                    continue

                domain_id = vm.pop("_domain_id", -1)
                if vm["active"]:
                    active_domain_ids.add(vm["uuid"])
                    previous = vm_metric_samples.get(vm["uuid"])
                    if previous:
                        elapsed = now - previous["time"]
                        if elapsed > 0:
                            vm["cpu_percent"] = round(
                                min(100, max(0, (vm["cpu_time"] - previous["cpu_time"])
                                             / elapsed / 1e7 / max(vm["vcpus"], 1))), 1)
                            vm["disk_read_bytes_sec"] = round(
                                max(0, (vm["disk_read"] - previous["disk_read"]) / elapsed), 1)
                            vm["disk_write_bytes_sec"] = round(
                                max(0, (vm["disk_write"] - previous["disk_write"]) / elapsed), 1)
                            vm["network_rx_bytes_sec"] = round(
                                max(0, (vm["net_rx"] - previous["net_rx"]) / elapsed), 1)
                            vm["network_tx_bytes_sec"] = round(
                                max(0, (vm["net_tx"] - previous["net_tx"]) / elapsed), 1)
                            vm["rates_available"] = True
                    vm_metric_samples[vm["uuid"]] = {
                        "time": now, "cpu_time": vm["cpu_time"],
                        "disk_read": vm["disk_read"], "disk_write": vm["disk_write"],
                        "net_rx": vm["net_rx"], "net_tx": vm["net_tx"],
                    }
                else:
                    domain_id = -1
                # Strip internal rate-accumulation counters from the payload.
                for _key in ("cpu_time", "disk_read", "disk_write", "net_rx", "net_tx"):
                    vm.pop(_key, None)
                vm["id"] = domain_id
                vms.append(vm)
            except asyncio.TimeoutError:
                logger.warning("Timed out collecting metrics for a libvirt domain")
                continue
            except Exception as exc:
                logger.warning("Could not collect metrics for a libvirt domain: %s", exc)

        # Discard counters for guests that were stopped or removed.
        for domain_uuid in list(vm_metric_samples):
            if domain_uuid not in active_domain_ids:
                vm_metric_samples.pop(domain_uuid, None)
        return vms


def _collect_domain_snapshot(domain, state_map: Dict[int, str]) -> Optional[Dict[str, Any]]:
    """Synchronously read one domain's inventory + raw counters.

    Runs inside the libvirt executor thread. Returns a dict with the raw
    counters still attached so the caller can compute rates, or ``None`` when
    the domain vanished mid-collection. The single domain object is only ever
    touched from this one executor thread, so it stays thread-safe.
    """
    try:
        info = domain.info()  # state, maxMem KiB, memory KiB, vCPUs, cpuTime ns
        state = state_map.get(info[0], "unknown")
        vm: Dict[str, Any] = {
            "_domain_id": domain.ID() if domain.isActive() else -1,
            "uuid": domain.UUIDString(), "name": domain.name(),
            "state": state, "active": bool(domain.isActive()), "vcpus": info[3],
            "max_memory": info[1], "memory": info[2], "cpu_time": info[4],
            "cpu_percent": 0.0, "memory_used": 0, "memory_total": info[1],
            "memory_percent": 0.0, "disk_read_bytes_sec": 0.0,
            "disk_write_bytes_sec": 0.0, "network_rx_bytes_sec": 0.0,
            "network_tx_bytes_sec": 0.0, "rates_available": False,
            "disks": [], "interfaces": [],
            "disk_read": 0, "disk_write": 0, "net_rx": 0, "net_tx": 0,
        }
        if not vm["active"]:
            return vm

        memory = domain.memoryStats()
        memory_total = memory.get("actual", info[1]) or info[1]
        # rss is the best guest-used figure when the balloon driver reports it.
        memory_used = memory.get("rss", memory.get("actual", info[2]))
        if "unused" in memory and "actual" in memory:
            memory_used = max(memory_used, memory["actual"] - memory["unused"])
        vm.update({
            "memory_used": memory_used, "memory_total": memory_total,
            "memory_percent": round((memory_used / memory_total * 100) if memory_total else 0, 1),
        })

        disk_read = disk_write = net_rx = net_tx = 0
        try:
            root = ET.fromstring(domain.XMLDesc(0))
            disk_targets = [node.get("dev") for node in root.findall("./devices/disk/target") if node.get("dev")]
            interface_targets = [node.get("dev") for node in root.findall("./devices/interface/target") if node.get("dev")]
        except ET.ParseError:
            disk_targets, interface_targets = [], []

        for target in disk_targets:
            try:
                stats = domain.blockStats(target)
                # rd_req, rd_bytes, wr_req, wr_bytes, errs
                read_bytes, write_bytes = stats[1], stats[3]
                disk_read += read_bytes
                disk_write += write_bytes
                try:
                    capacity, allocation, _ = domain.blockInfo(target, 0)
                except Exception:
                    capacity, allocation = 0, 0
                vm["disks"].append({"target": target, "read_bytes": read_bytes, "write_bytes": write_bytes,
                                    "capacity": capacity, "allocation": allocation})
            except Exception:
                continue
        for target in interface_targets:
            try:
                stats = domain.interfaceStats(target)
                # rx_bytes, rx_packets, rx_errs, rx_drop, tx_bytes, ...
                rx_bytes, tx_bytes = stats[0], stats[4]
                net_rx += rx_bytes
                net_tx += tx_bytes
                vm["interfaces"].append({"target": target, "rx_bytes": rx_bytes, "tx_bytes": tx_bytes})
            except Exception:
                continue

        vm.update({
            "disk_read": disk_read, "disk_write": disk_write,
            "net_rx": net_rx, "net_tx": net_tx,
        })
        return vm
    except libvirt.libvirtError as exc:
        logger.warning("libvirt error collecting domain snapshot: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Error collecting domain snapshot: %s", exc)
        return None


async def _cached_optional(name: str, factory, ttl: float, timeout: float = 20.0):
    """Run a slow optional collector at most once per TTL.

    A cached ``None`` is intentional: unavailable hardware should not cause a
    subprocess or libvirt retry on every frame. The next TTL expiry retries it.
    """
    now = time.monotonic()
    async with _peripheral_cache_lock:
        cached = _peripheral_cache.get(name)
        if cached and now - cached["time"] < ttl:
            return cached["value"]
    try:
        value = await asyncio.wait_for(factory(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Peripheral stats collector %s timed out", name)
        value = None
    except Exception as exc:
        logger.warning("Peripheral stats collector %s failed: %s", name, exc)
        value = None
    async with _peripheral_cache_lock:
        _peripheral_cache[name] = {"time": time.monotonic(), "value": value}
    return value


# The most recent telemetry snapshot. psutil's cpu_percent(interval=None)
# measures *between calls*, so every extra collection (REST refresh, a second
# browser tab, a WebSocket reconnect) used to reset the sampling window and
# produce jittery ~0% CPU readings. All readers now share one snapshot that
# only the broadcast loop refreshes at a steady cadence — smoother CPU curves
# and a faster /api/stats, which answers from cache instead of running a
# full duplicate collection behind the stats lock.
_stats_snapshot: Optional["SystemStats"] = None
_stats_snapshot_time = 0.0
# Slightly shorter than STATS_INTERVAL so the broadcast loop always refreshes
# on its due tick while REST reads inside the same tick hit the cache.
_STATS_SNAPSHOT_TTL = STATS_INTERVAL * 0.9


async def collect_all_stats(force: bool = False) -> SystemStats:
    """Collect core telemetry quickly and cache slower optional subsystems.

    Returns the shared snapshot when it is still fresh; callers can pass
    ``force=True`` to demand a brand-new collection.
    """
    global _stats_snapshot, _stats_snapshot_time
    now = time.monotonic()
    if (not force and _stats_snapshot is not None
            and now - _stats_snapshot_time < _STATS_SNAPSHOT_TTL):
        return _stats_snapshot
    async with stats_lock:
        # Another task may have refreshed the snapshot while we waited.
        now = time.monotonic()
        if (not force and _stats_snapshot is not None
                and now - _stats_snapshot_time < _STATS_SNAPSHOT_TTL):
            return _stats_snapshot
        cpu, memory, disk, network, processes, system = await asyncio.gather(
            get_cpu_stats(),
            get_memory_stats(),
            get_disk_stats(),
            get_network_stats(),
            get_process_stats(),
            get_system_info(),
        )

        # GPU, VM, and thermal collectors are useful but do not need to run at
        # the same cadence as CPU/memory. This keeps a slow libvirt or hardware
        # sensor read from making the UI feel laggy.
        gpu, vms, thermal = await asyncio.gather(
            _cached_optional("gpu", get_gpu_stats, 5.0, timeout=OPTIONAL_COLLECTOR_TIMEOUT),
            _cached_optional("vms", get_vm_stats, 3.0, timeout=OPTIONAL_COLLECTOR_TIMEOUT),
            _cached_optional("thermal", get_thermal_stats, 5.0, timeout=OPTIONAL_COLLECTOR_TIMEOUT),
        )

        # NOTE: snapshots are persisted exactly once, by
        # persist_snapshot_and_evaluate_alerts() into metric_history. A second
        # write to a separate /tmp 'metrics' table used to happen here, storing
        # the same samples twice on every telemetry tick.

        _stats_snapshot = SystemStats(
            timestamp=datetime.now().isoformat(),
            cpu=cpu,
            memory=memory,
            disk=disk,
            network=network,
            gpu=gpu,
            processes=processes,
            system=system,
            vms=vms,
            thermal=thermal,
        )
        _stats_snapshot_time = time.monotonic()
        return _stats_snapshot


async def broadcast_stats():
    """Broadcast stats to all clients at a steady cadence.

    The sleep accounts for the time collection/persistence/fan-out already
    took, so frames leave at a constant STATS_INTERVAL rhythm instead of
    drifting to "interval + work time" (which made the UI feel uneven).
    """
    while True:
        started = time.monotonic()
        try:
            stats = await collect_all_stats()
            await asyncio.to_thread(persist_snapshot_and_evaluate_alerts, stats)
            await manager.broadcast(stats.model_dump())
        except Exception as e:
            logger.error(f"Error broadcasting stats: {e}")
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(0.1, STATS_INTERVAL - elapsed))


# =============================================================================
# Operations center: local history, alert rules, incident timeline and webhooks
# =============================================================================
OPERATIONS_DB = Path(os.environ.get(
    "MONITORX_OPERATIONS_DB", str(_ensure_state_dir() / "monitorx-operations.db")
))
DEFAULT_ALERT_RULES = [
    {"id": "cpu-high", "name": "CPU usage high", "metric": "cpu", "operator": ">=", "threshold": 90, "cooldown_minutes": 15, "enabled": True},
    {"id": "memory-high", "name": "Memory pressure", "metric": "memory", "operator": ">=", "threshold": 90, "cooldown_minutes": 15, "enabled": True},
    {"id": "disk-high", "name": "Disk capacity low", "metric": "disk", "operator": ">=", "threshold": 90, "cooldown_minutes": 30, "enabled": True},
]

@contextmanager
def _ops_conn():
    """Yield a SQLite connection that is committed AND closed.

    NOTE: ``with sqlite3.connect(...) as conn`` only wraps a *transaction* —
    it commits or rolls back but never closes the handle. Relying on it (as
    this module previously did) leaks a file descriptor per call, and this
    helper runs on the telemetry loop every STATS_INTERVAL seconds. The inner
    ``with conn`` keeps the transaction semantics callers depend on; the
    ``finally`` is what actually releases the descriptor.
    """
    # Host telemetry must not be world-readable, whoever opens the DB first.
    fresh = not OPERATIONS_DB.exists()
    conn = sqlite3.connect(str(OPERATIONS_DB), timeout=10)
    try:
        if fresh:
            _harden_file_mode(OPERATIONS_DB)
        conn.row_factory = sqlite3.Row
        # The broadcast task and the REST API write concurrently; WAL + a busy
        # timeout keep those writers from tripping over each other.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        with conn:
            yield conn
    finally:
        conn.close()

def init_operations_store():
    _ensure_state_dir()
    with _ops_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS metric_history (timestamp TEXT PRIMARY KEY, cpu REAL, memory REAL, disk REAL, net_rx REAL, net_tx REAL);
        CREATE TABLE IF NOT EXISTS alert_rules (id TEXT PRIMARY KEY, name TEXT, metric TEXT, operator TEXT, threshold REAL, cooldown_minutes INTEGER, enabled INTEGER);
        CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, rule_id TEXT, title TEXT, severity TEXT, value REAL, status TEXT DEFAULT 'open', acknowledged_at TEXT, snoozed_until TEXT);
        CREATE TABLE IF NOT EXISTS operations_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, action TEXT, target TEXT, outcome TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        if not conn.execute("SELECT count(*) FROM alert_rules").fetchone()[0]:
            conn.executemany("INSERT INTO alert_rules VALUES (:id,:name,:metric,:operator,:threshold,:cooldown_minutes,:enabled)", DEFAULT_ALERT_RULES)
        # The 30-day retention DELETE below filters on timestamp; without this
        # index it degrades to a full scan of every two-second sample.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metric_history_ts ON metric_history(timestamp)")
    # Host telemetry is not world-readable. Cover the WAL sidecars too.
    for suffix in ("", "-wal", "-shm"):
        _harden_file_mode(Path(str(OPERATIONS_DB) + suffix))

def _metrics(stats):
    return {"cpu": float(stats.cpu.get("percent_total", 0)), "memory": float(stats.memory.get("percent", 0)), "disk": max([float(x.get("percent", 0)) for x in stats.disk.get("partitions", [])] or [0]), "net_rx": float(stats.network.get("rx_bytes_sec", 0)), "net_tx": float(stats.network.get("tx_bytes_sec", 0))}

_last_history_cleanup = 0.0
_HISTORY_CLEANUP_INTERVAL = 600.0  # seconds; the DELETE scans the whole table


def persist_snapshot_and_evaluate_alerts(stats):
    global _last_history_cleanup
    values = _metrics(stats); now = datetime.now().isoformat()
    with _ops_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO metric_history VALUES (?,?,?,?,?,?)", (now, values['cpu'], values['memory'], values['disk'], values['net_rx'], values['net_tx']))
        # Keep 30 days at the native two-second cadence; older detail is
        # discarded. The DELETE scans the table, so it only runs every 10
        # minutes instead of on every two-second snapshot.
        if time.time() - _last_history_cleanup >= _HISTORY_CLEANUP_INTERVAL:
            conn.execute("DELETE FROM metric_history WHERE timestamp < datetime('now', '-30 days')")
            _last_history_cleanup = time.time()
        for rule in conn.execute("SELECT * FROM alert_rules WHERE enabled=1"):
            value = values.get(rule['metric'], 0); triggered = value >= rule['threshold'] if rule['operator'] == '>=' else value <= rule['threshold']
            # The last incident for this rule regardless of status. Only this
            # row decides whether a NEW incident may be opened, so
            # acknowledged/resolved incidents suppress duplicates instead of
            # re-triggering a fresh incident on every two-second snapshot.
            last = conn.execute(
                "SELECT * FROM incidents WHERE rule_id=? ORDER BY id DESC LIMIT 1",
                (rule['id'],),
            ).fetchone()
            cooldown = max(float(rule['cooldown_minutes'] or 0), 0.0) * 60.0

            def _age_seconds(ref_ts):
                """Age of a timestamp column in seconds (0 when unparsable)."""
                try:
                    return (datetime.now() - datetime.fromisoformat(ref_ts)).total_seconds()
                except (TypeError, ValueError):
                    return 0.0

            if triggered:
                should_open = False
                if last is None:
                    should_open = True
                elif last['status'] == 'open':
                    pass  # already reported; do not duplicate
                elif last['status'] == 'acknowledged':
                    # The operator saw it. Do not re-open until the cooldown
                    # window after the ack has elapsed.
                    ref = last['acknowledged_at'] or last['timestamp']
                    should_open = _age_seconds(ref) >= cooldown
                else:  # resolved
                    # Re-arm the rule only after the cooldown since resolution.
                    should_open = _age_seconds(last['timestamp']) >= cooldown
                if should_open:
                    severity = 'critical' if value >= rule['threshold'] + 5 else 'warning'
                    conn.execute("INSERT INTO incidents(timestamp,rule_id,title,severity,value) VALUES(?,?,?,?,?)", (now, rule['id'], rule['name'], severity, value))
                    conn.execute("INSERT INTO operations_audit(timestamp,action,target,outcome,detail) VALUES(?,?,?,?,?)", (now, 'alert_opened', rule['id'], 'success', f'{value:.1f}'))
                    # Optional outbound notification (fires on the event loop).
                    try:
                        asyncio.create_task(_notify_webhook(
                            rule['name'], severity, f"{value:.1f}", rule['metric'],
                        ))
                    except Exception as exc:
                        logger.warning("Webhook scheduling failed: %s", exc)
            elif last is not None and last['status'] == 'open':
                conn.execute("UPDATE incidents SET status='resolved', acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?", (now, last['id']))

def audit_operation(action, target, outcome='success', detail=''):
    with _ops_conn() as conn:
        conn.execute("INSERT INTO operations_audit(timestamp,action,target,outcome,detail) VALUES(?,?,?,?,?)", (datetime.now().isoformat(), action, target, outcome, detail[:500]))


# =============================================================================
# Alert notification webhooks
#
# An optional, additive outbound channel: when a threshold incident opens, a
# JSON POST is fired to a configured webhook URL (Slack/Discord/generic). The
# payload is deliberately generic ({ title, severity, value, metric, host,
# timestamp }) so any receiver can format it. Config lives in the operations
# `settings` table and is disabled until the operator saves a URL.
# =============================================================================
WEBHOOK_TIMEOUT = 8.0

def _webhook_config():
    with _ops_conn() as conn:
        row_url = conn.execute("SELECT value FROM settings WHERE key='webhook_url'").fetchone()
        row_en = conn.execute("SELECT value FROM settings WHERE key='webhook_enabled'").fetchone()
    return {
        "url": (row_url["value"] if row_url else "") or "",
        "enabled": (row_en["value"] == "1") if row_en else False,
    }


def _fire_webhook_sync(title: str, severity: str, value: str, metric: str) -> Optional[str]:
    """Best-effort synchronous webhook POST. Returns error string or None."""
    cfg = _webhook_config()
    if not cfg["enabled"] or not cfg["url"]:
        return None
    payload = {
        "title": title,
        "severity": severity,
        "value": value,
        "metric": metric,
        "host": socket.gethostname(),
        "timestamp": datetime.now().isoformat(),
        "source": "MonitorX",
    }
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MonitorX/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as resp:
            resp.read()
        return None
    except Exception as exc:
        return str(exc)


async def _notify_webhook(title: str, severity: str, value: str, metric: str):
    """Fire a webhook notification off the event loop (never blocks a frame)."""
    try:
        loop = asyncio.get_running_loop()
        err = await loop.run_in_executor(None, _fire_webhook_sync, title, severity, value, metric)
        if err:
            logger.warning("Webhook notification failed: %s", err)
    except RuntimeError:
        # No running loop (rare, direct call path) — fall back to inline send.
        err = _fire_webhook_sync(title, severity, value, metric)
        if err:
            logger.warning("Webhook notification failed: %s", err)
    except Exception as exc:
        logger.warning("Webhook scheduling failed: %s", exc)

class AlertRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    metric: str = Field(pattern="^(cpu|memory|disk|net_rx|net_tx)$")
    threshold: float = Field(ge=0)
    cooldown_minutes: int = Field(default=15, ge=1, le=1440)
    enabled: bool = True

@app.get('/api/operations/overview')
async def operations_overview(window: str = Query('1h', alias='range', pattern='^(1h|6h|24h|7d)$')):
    # Parameter is 'window' internally; 'range' would shadow the builtin.
    hours = {'1h': 1, '6h': 6, '24h': 24, '7d': 168}[window]
    with _ops_conn() as conn:
        rows = conn.execute("SELECT * FROM metric_history WHERE timestamp >= datetime('now', ?) ORDER BY timestamp", (f'-{hours} hours',)).fetchall()
        incidents = conn.execute("SELECT * FROM incidents WHERE status='open' OR timestamp >= datetime('now','-24 hours') ORDER BY id DESC LIMIT 30").fetchall()
    return {'range': window, 'history': [dict(x) for x in rows], 'incidents': [dict(x) for x in incidents]}

@app.get('/api/operations/alert-rules')
async def list_alert_rules():
    with _ops_conn() as conn: return [dict(x) for x in conn.execute('SELECT * FROM alert_rules ORDER BY name')]

@app.post('/api/operations/alert-rules')
async def create_alert_rule(rule: AlertRuleRequest):
    rule_id = f"custom-{int(time.time() * 1000)}"
    with _ops_conn() as conn: conn.execute("INSERT INTO alert_rules VALUES (?,?,?,?,?,?,?)", (rule_id, rule.name, rule.metric, '>=', rule.threshold, rule.cooldown_minutes, int(rule.enabled)))
    audit_operation('alert_rule_created', rule.name)
    return {'id': rule_id, **rule.model_dump()}


class RuleUpdateRequest(BaseModel):
    """Optional fields to update on an existing alert rule (all fields optional)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    metric: Optional[str] = Field(default=None, pattern="^(cpu|memory|disk|net_rx|net_tx)$")
    threshold: Optional[float] = Field(default=None, ge=0)
    cooldown_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    enabled: Optional[bool] = None


@app.patch('/api/operations/alert-rules/{rule_id}')
async def update_alert_rule(rule_id: str, update: RuleUpdateRequest):
    """Update one or more fields of an alert rule (used to edit or toggle it)."""
    with _ops_conn() as conn:
        row = conn.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alert rule not found")

        sets = []
        params = []
        # CRITICAL FIX: Use explicit whitelist + safe parameterization instead of f-string
        # (root cause: dynamic column construction from user input)
        field_map = {
            "name": update.name,
            "metric": update.metric,
            "threshold": update.threshold,
            "cooldown_minutes": update.cooldown_minutes,
            "enabled": update.enabled,
        }
        for col, val in field_map.items():
            if val is not None:
                sets.append(f"{col}=?")
                params.append(int(val) if col == "enabled" else val)

        if sets:
            params.append(rule_id)
            conn.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id=?", params)

        updated = conn.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone()
    audit_operation('alert_rule_updated', rule_id)
    return dict(updated)


@app.delete('/api/operations/alert-rules/{rule_id}')
async def delete_alert_rule(rule_id: str):
    with _ops_conn() as conn:
        row = conn.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alert rule not found")
        conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,))
        conn.execute("UPDATE incidents SET status='resolved', acknowledged_at=COALESCE(acknowledged_at,?) WHERE rule_id=? AND status='open'", (datetime.now().isoformat(), rule_id))
    audit_operation('alert_rule_deleted', dict(row)['name'])
    return {'success': True}


@app.get('/api/operations/webhook')
async def get_webhook_config():
    cfg = _webhook_config()
    return {"enabled": cfg["enabled"], "url": cfg["url"]}


class WebhookConfigRequest(BaseModel):
    url: str = Field(default="", max_length=512)
    enabled: bool = True


def _reject_ssrf_target(hostname: str) -> Optional[str]:
    """Return a rejection reason if `hostname` resolves somewhere internal.

    The webhook URL is attacker-controllable through the settings API and the
    server then POSTs to it. Without this check it doubles as a blind SSRF
    primitive against loopback services, link-local metadata endpoints
    (169.254.169.254) and the rest of the private network the host sits on.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Webhook host '{hostname}' could not be resolved."
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return (f"Webhook host '{hostname}' resolves to internal address "
                    f"{ip}. Webhooks may only target public endpoints.")
    return None


@app.post('/api/operations/webhook')
async def set_webhook_config(cfg: WebhookConfigRequest):
    if cfg.url:
        parsed = urlparse(cfg.url)
        if (parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password):
            raise HTTPException(status_code=400, detail="Webhook URL must be an http(s) URL without embedded credentials.")
        reason = await asyncio.to_thread(_reject_ssrf_target, parsed.hostname)
        if reason:
            raise HTTPException(status_code=400, detail=reason)
    with _ops_conn() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('webhook_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cfg.url,))
        conn.execute("INSERT INTO settings(key,value) VALUES('webhook_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if cfg.enabled else '0',))
    audit_operation('webhook_configured', 'enabled' if cfg.enabled else 'disabled')
    return {"enabled": cfg.enabled, "url": cfg.url}


@app.post('/api/operations/webhook/test')
async def test_webhook():
    err = await asyncio.get_running_loop().run_in_executor(
        None, _fire_webhook_sync, "MonitorX webhook test", "info", "--", "test"
    )
    if err:
        raise HTTPException(status_code=502, detail=f"Webhook delivery failed: {err}")
    return {"success": True, "message": "Webhook delivered successfully"}

@app.post('/api/operations/incidents/{incident_id}/acknowledge')
async def acknowledge_incident(incident_id: int):
    with _ops_conn() as conn: conn.execute("UPDATE incidents SET status='acknowledged', acknowledged_at=? WHERE id=?", (datetime.now().isoformat(), incident_id))
    audit_operation('incident_acknowledged', str(incident_id)); return {'success': True}

@app.get('/api/operations/audit')
async def operations_audit(limit: int = Query(30, ge=1, le=200)):
    with _ops_conn() as conn: return [dict(x) for x in conn.execute('SELECT * FROM operations_audit ORDER BY id DESC LIMIT ?', (limit,))]

# REST API Endpoints
@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard page"""
    with open(str(FRONTEND_DIR / "index.html"), "r") as f:
        html = f.read()
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# Diagnostic / process-kill audit trail. Lives in the private state directory
# (never /tmp) and is appended through _append_audit_line so a symlink planted
# at the path cannot redirect the write.
AUDIT_LOG = Path(os.environ.get("MONITORX_AUDIT_LOG", str(_ensure_state_dir() / "monitorx-audit.log")))


def _append_audit_line(action: str, detail: str = "") -> bool:
    """Append one audit record. Returns False if the write was refused."""
    line = "{} | {} | {} | {}\n".format(
        datetime.now().isoformat(),
        os.getuid(),
        str(action).replace("\n", " ").replace("|", "/"),
        str(detail).replace("\n", " ").replace("|", "/"),
    )
    try:
        _ensure_state_dir()
        with _secure_open_append(AUDIT_LOG) as f:
            f.write(line)
        return True
    except OSError as exc:
        # ELOOP here means someone planted a symlink at AUDIT_LOG.
        logger.warning("Audit append refused for %s: %s", AUDIT_LOG, exc)
        return False

@app.get("/api/audit")
async def get_audit(log_lines: int = Query(50, ge=1, le=500)):
    """Return recent audit entries (process kills + diagnostics)."""
    entries = []
    if AUDIT_LOG.exists():
        try:
            with open(AUDIT_LOG, "r") as f:
                lines = f.read().splitlines()[-log_lines:]
            for line in lines:
                entries.append({"time": line[:19] if len(line) > 19 else line, "entry": line})
        except Exception:
            pass
    return {"entries": entries, "count": len(entries)}


@app.post("/api/audit")
async def post_audit(action: str = Query(...), detail: str = Query("")):
    """Log a diagnostic or process kill action."""
    return {"logged": _append_audit_line(action, detail)}


@app.get("/api/stats", response_model=SystemStats)
async def get_stats():
    return await collect_all_stats()


@app.get("/api/stats/processes")
async def get_processes(limit: int = Query(30, ge=1, le=500)):
    return await get_process_stats(limit)


# =============================================================================
# DIAGNOSTICS REPORT EXPORT
# =============================================================================

async def _collect_report_data() -> Dict[str, Any]:
    """Gather a self-contained snapshot of the host for a diagnostics report."""
    cpu = await get_cpu_stats()
    memory = await get_memory_stats()
    disk = await get_disk_stats()
    network = await get_network_stats()
    system = await get_system_info()
    processes = await get_process_stats(limit=15)
    thermal = await get_thermal_stats()
    return {
        "generated": datetime.now().isoformat(),
        "system": system,
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "network": {
            "rx_bytes_sec": network.get("rx_bytes_sec"),
            "tx_bytes_sec": network.get("tx_bytes_sec"),
            "connections_count": network.get("connections_count"),
            "interfaces": network.get("interfaces"),
        },
        "thermal": thermal,
        "top_processes": processes,
    }


def _report_to_markdown(data: Dict[str, Any]) -> str:
    s = data["system"]
    lines = [
        "# MonitorX Diagnostics Report",
        "",
        f"Generated: `{data['generated']}`",
        "",
        "## System",
        f"- Hostname: `{s['hostname']}`",
        f"- Platform: `{s['platform']} {s['platform_release']}`",
        f"- Kernel: `{s['platform_version']}`",
        f"- Architecture: `{s['architecture']}`",
        f"- Processor: `{s['processor']}`",
        f"- Boot time: `{s['boot_time']}`",
        f"- Uptime: `{s['uptime_str']}`",
        "",
        "## CPU",
        f"- Utilization: `{data['cpu'].get('percent_total', 0):.1f}%`",
        f"- Cores: `{data['cpu'].get('count_logical', 0)}`",
        f"- Load (1/5/15m): `{data['cpu'].get('load_1min', 0):.2f} / {data['cpu'].get('load_5min', 0):.2f} / {data['cpu'].get('load_15min', 0):.2f}`",
        "",
        "## Memory",
        f"- Used: `{data['memory'].get('percent', 0)}%`",
        f"- Available: `{data['memory'].get('available', 0)}` bytes",
        f"- Swap: `{data['memory'].get('swap_percent', 0)}%`",
        "",
        "## Thermal",
    ]
    if data["thermal"]["temperatures"]:
        for t in data["thermal"]["temperatures"]:
            cur = f"{t['current_c']}°C" if t["current_c"] is not None else "n/a"
            lines.append(f"- `{t['name']}`: {cur}")
    else:
        lines.append("- No temperature sensors exposed.")
    if data["thermal"]["fans"]:
        for f in data["thermal"]["fans"]:
            lines.append(f"- Fan `{f['name']}`: {f['current_rpm']} RPM")
    lines.append("")
    lines.append("## Top Processes (by CPU)")
    lines.append("")
    lines.append("| PID | Name | CPU % | RAM % | RSS | User | Status |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for p in data["top_processes"]:
        lines.append(f"| {p['pid']} | {p['name']} | {p['cpu_percent']} | {p['memory_percent']} | {p['memory_mb']} MB | {p['username']} | {p['status']} |")
    return "\n".join(lines) + "\n"


@app.get("/api/report/export")
async def report_export(fmt: str = Query("json", alias="format", pattern="^(json|markdown)$")):
    # Parameter is 'fmt' internally; 'format' would shadow the builtin.
    data = await _collect_report_data()
    if fmt == "markdown":
        body = _report_to_markdown(data)
        filename = f"monitorx-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        return Response(
            content=body,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    body = json.dumps(data, indent=2)
    filename = f"monitorx-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/stats/vms")
async def get_vms():
    vms = await get_vm_stats()
    if vms is None:
        raise HTTPException(status_code=404, detail="VM monitoring not available")
    return vms


@app.get("/api/health")
async def health_check_endpoint():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "gpu_available": NVML_AVAILABLE,
        "vm_available": LIBVIRT_AVAILABLE
    }


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not _websocket_authenticated(websocket):
        await websocket.close(code=4401, reason="Authentication required")
        return
    await manager.connect(websocket)
    try:
        try:
            stats = await asyncio.wait_for(collect_all_stats(), timeout=25.0)
        except Exception:
            # Never let a slow initial collection stall the connection; the
            # broadcast task keeps the client fed with fresher data anyway.
            stats = None
        if stats is not None:
            await websocket.send_json(stats.model_dump())
        
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong", "timestamp": datetime.now().isoformat()})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


# =============================================================================
# VM CONSOLE WEBSOCKET PROXY
# =============================================================================

@app.websocket("/ws/vm-console/{vm_id}")
async def vm_console_ws(websocket: WebSocket, vm_id: str):
    """WebSocket proxy for an authenticated VM serial console.

    Proxies the guest serial console through a PTY to xterm.js.

    Graphical VNC is intentionally not advertised: rendering it requires a
    dedicated noVNC client rather than a terminal emulator.
    """
    if not _websocket_authenticated(websocket):
        await websocket.close(code=4401, reason="Authentication required")
        return
    await websocket.accept()

    if not LIBVIRT_AVAILABLE:
        await websocket.close(code=1011, reason="libvirt is not installed")
        return

    if not VM_ID_PATTERN.fullmatch(vm_id):
        await websocket.close(code=1011, reason="Invalid VM identifier")
        return

    domain, error = await _resolve_domain(vm_id)
    if error:
        await websocket.close(code=1011, reason=error)
        return

    # The dashboard exposes a serial console only. xterm.js is a terminal
    # emulator, not a VNC renderer; advertising raw VNC bytes as a terminal
    # made the previous graphical-console button appear broken.
    # Fallback: serial console via `virsh console`.
    #
    # The argv mirrors the sudoers policy from systemd/install-service.sh
    # (virsh --quiet --no-pkttyagent --connect <URI> console -- <domain>), so
    # the command is authorized on unprivileged installs. virsh console
    # demands a real TTY, so the subprocess runs on a pty allocated here and
    # the pty master is bridged to the WebSocket; without the pty virsh fails
    # with "unable to open a pseudo-terminal" under pipes.
    await websocket.send_json({"type": "serial"})

    # Prefer --no-pkttyagent on hosts that support it (libvirt ≥11.4);
    # older virsh rejects it. Try with the flag first when supported;
    # virsh-help probing decides the default, but even when probing says
    # supported we still handle an unsupported error by retrying below.
    if _virsh_supports_no_pkttyagent():
        cmd = [VIRSH_BIN, "--quiet", "--no-pkttyagent", "--connect", LIBVIRT_URI, "console", "--", vm_id]
    else:
        cmd = [VIRSH_BIN, "--quiet", "--connect", LIBVIRT_URI, "console", "--", vm_id]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if sudo:
            cmd = [sudo, "-n", *cmd]

    proc = None
    master_fd = None
    try:
        master_fd, slave_fd = os.openpty()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
    except Exception as e:
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.close(code=1011, reason=str(e))
        return

    # Wrap the pty master in asyncio streams so reads never block the loop.
    loop = asyncio.get_running_loop()
    try:
        console_reader = asyncio.StreamReader()
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(console_reader),
            os.fdopen(os.dup(master_fd), "rb", buffering=0),
        )
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin,
            os.fdopen(os.dup(master_fd), "wb", buffering=0),
        )
        console_writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    except Exception as e:
        try:
            os.close(master_fd)
        except OSError:
            pass
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.close(code=1011, reason=str(e))
        return

    async def read_console():
        try:
            while True:
                data = await console_reader.read(4096)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass

    async def write_console():
        try:
            while True:
                data = await websocket.receive_bytes()
                console_writer.write(data)
                await console_writer.drain()
        except Exception:
            pass

    try:
        await asyncio.gather(read_console(), write_console())
    except Exception:
        pass
    finally:
        try:
            write_transport.close()
            read_transport.close()
            console_writer.close()
            os.close(master_fd)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


# Process management endpoints
@app.get("/api/processes/{pid}")
async def get_process_detail(pid: int):
    """Get detailed process information.

    Fields that require elevated privileges (cmdline, environ, open files,
    sockets) are read individually and degrade to a sensible placeholder on
    ``AccessDenied`` instead of failing the whole request — an unprivileged
    MonitorX can then still inspect the metadata of root-owned processes.
    """
    try:
        proc = psutil.Process(pid)

        def safe(getter, default):
            try:
                value = getter()
                return default if value is None else value
            except (psutil.AccessDenied, psutil.ZombieProcess):
                return default
            except Exception:
                return default

        connections = []
        try:
            connections = [conn._asdict() for conn in proc.connections()]
        except Exception:
            connections = []
        open_files = []
        try:
            open_files = [f._asdict() for f in proc.open_files() or []]
        except Exception:
            open_files = []
        environ = {}
        try:
            environ = dict(list(proc.environ().items())[:20])
        except Exception:
            environ = {}

        return {
            "pid": proc.pid,
            "name": safe(proc.name, "unknown"),
            "exe": safe(proc.exe, ""),
            "cmdline": safe(proc.cmdline, []),
            "status": safe(proc.status, "unknown"),
            "username": safe(proc.username, "unknown"),
            "create_time": safe(lambda: datetime.fromtimestamp(proc.create_time()).isoformat(), ""),
            "cpu_percent": safe(lambda: proc.cpu_percent(interval=0.1), 0.0),
            "memory_percent": round(safe(proc.memory_percent, 0.0), 2),
            "memory_info": safe(lambda: dict(proc.memory_info()._asdict()), {}),
            "num_threads": safe(proc.num_threads, 1),
            "num_fds": safe(proc.num_fds, 0),
            "connections": connections,
            "open_files": open_files,
            "environ": environ,
        }
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="Process not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied")


@app.post("/api/processes/{pid}/kill")
async def kill_process(pid: int, sig: int = Query(15, alias="signal")):
    """Terminate a process with SIGTERM (15) or SIGKILL (9).

    A SIGTERM request gets a 5-second grace period for the process to exit
    cleanly; only then is it escalated to SIGKILL (and the response says so).
    Previously the escalation happened after 0.5s, which made SIGTERM requests
    effectively indistinguishable from SIGKILL.

    NOTE: the parameter is ``sig``, not ``signal``. Naming it ``signal``
    shadowed the stdlib ``signal`` module inside this function's scope; the
    query-string name is preserved via ``alias`` so the wire API is unchanged.
    """
    if sig not in (9, 15):
        raise HTTPException(status_code=400, detail="Only SIGTERM (15) and SIGKILL (9) are allowed.")
    try:
        proc = psutil.Process(pid)
        # Security: prevent killing processes owned by other users (P0)
        try:
            proc_uids = proc.uids()
            current_uid = os.getuid()
            if proc_uids and proc_uids.real != current_uid and current_uid != 0:
                raise HTTPException(status_code=403, detail=f"Process {pid} belongs to UID {proc_uids.real}; you are UID {current_uid}.")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        _append_audit_line("kill", f"pid={pid} signal={sig}")
        proc.send_signal(sig)
        if sig == 9:
            return {"success": True, "message": f"Process {pid} killed (SIGKILL)"}
        # Graceful window: poll briefly, then escalate only if still alive.
        escalated = False
        for _ in range(10):
            await asyncio.sleep(0.5)
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
        else:
            try:
                proc.kill()
                escalated = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if escalated:
            return {"success": True, "message": f"Process {pid} did not exit within 5s and was escalated to SIGKILL"}
        return {"success": True, "message": f"Process {pid} terminated (SIGTERM)"}
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="Process not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied")


class BatchKillRequest(BaseModel):
    """Payload for the multi-select (bulk) process termination endpoint."""
    pids: List[int] = Field(..., min_length=1, max_length=500,
                            description="PIDs to terminate (deduplicated server-side).")
    signal: int = Field(15, description="Signal: 15 (SIGTERM, graceful) or 9 (SIGKILL).")


def _set_batch_result_message(results: List[Dict[str, Any]], pid: int, message: str) -> None:
    """Update the message of an already-recorded batch result (grace phase)."""
    for r in results:
        if r["pid"] == pid:
            r["message"] = message
            return


@app.post("/api/processes/kill")
async def kill_processes_batch(payload: BatchKillRequest):
    """Terminate several processes in a single request.

    The ownership guard is enforced **per process**: a PID owned by another
    user (or that vanished/permission-denied) is individually refused and
    reported in its own result entry, while every authorized PID is still
    killed. The bulk kill therefore never aborts wholesale on the first
    unauthorized target.

    SIGTERM targets share one 5-second grace window that polls all of them in
    parallel, so killing N processes takes ~5s total rather than 5s each;
    survivors are escalated to SIGKILL exactly like the single-PID endpoint.
    """
    if payload.signal not in (9, 15):
        raise HTTPException(status_code=400, detail="Only SIGTERM (15) and SIGKILL (9) are allowed.")
    pids = list(dict.fromkeys(payload.pids))  # dedupe, preserve order
    current_uid = os.getuid()
    results: List[Dict[str, Any]] = []
    pending: List[psutil.Process] = []  # SIGTERM targets awaiting exit

    def record(pid: int, success: bool, message: str) -> None:
        results.append({"pid": pid, "success": success, "message": message})

    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            record(pid, False, "process not found")
            continue
        # Security (P0): per-process ownership guard — never touch a process
        # owned by another user unless the dashboard itself runs as root.
        try:
            proc_uids = proc.uids()
        except psutil.AccessDenied:
            record(pid, False, f"permission denied (cannot read owner of PID {pid})")
            continue
        if proc_uids and proc_uids.real != current_uid and current_uid != 0:
            record(pid, False,
                   f"belongs to UID {proc_uids.real}; you are UID {current_uid} (skipped)")
            continue
        # Audit log, mirroring the single-PID endpoint.
        _append_audit_line("kill", f"pid={pid} signal={payload.signal}")
        try:
            proc.send_signal(payload.signal)
        except psutil.NoSuchProcess:
            record(pid, False, "process not found")
            continue
        except psutil.AccessDenied:
            record(pid, False, "permission denied")
            continue
        if payload.signal == 9:
            record(pid, True, "killed (SIGKILL)")
        else:
            pending.append(proc)
            record(pid, True, "SIGTERM sent; awaiting exit")

    # One shared grace window for every SIGTERM target.
    if pending:
        for _ in range(10):
            await asyncio.sleep(0.5)
            still_alive = []
            for proc in pending:
                try:
                    if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                        _set_batch_result_message(results, proc.pid, "terminated (SIGTERM)")
                    else:
                        still_alive.append(proc)
                except psutil.NoSuchProcess:
                    _set_batch_result_message(results, proc.pid, "terminated (SIGTERM)")
            pending = still_alive
            if not pending:
                break
        else:
            for proc in pending:
                try:
                    proc.kill()
                    _set_batch_result_message(
                        results, proc.pid, "did not exit within 5s; escalated to SIGKILL")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    _set_batch_result_message(
                        results, proc.pid, "did not exit within 5s; SIGKILL escalation failed")

    killed = sum(1 for r in results if r["success"])
    return {
        "results": results,
        "total": len(results),
        "killed": killed,
        "failed": len(results) - killed,
    }


# Power actions are intentionally not exposed to unauthenticated dashboard clients.
# Service-level actions are available through the constrained service-control API below.
SYSTEMCTL_BIN = shutil.which("systemctl") or "/usr/bin/systemctl"
SYSCTL_BIN = shutil.which("sysctl") or "/usr/sbin/sysctl"
JOURNALCTL_BIN = shutil.which("journalctl") or "/usr/bin/journalctl"

# Rolling history of System Health Index snapshots (one per scan) so the hub
# can show whether the host is improving or degrading over time.
_HEALTH_HISTORY = collections.deque(maxlen=60)
DMESG_BIN = shutil.which("dmesg") or "/usr/bin/dmesg"
SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.:-]*\.service$")
SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable", "disable")


def service_action_label(action: str) -> str:
    """Human-readable past tense for service control notifications."""
    return {"start": "started", "stop": "stopped", "restart": "restarted", "reload": "reloaded",
            "enable": "enabled", "disable": "disabled"}[action]


async def run_service_action(action: str, service_name: str):
    """Run an approved systemctl action without ever prompting for a password.

    MonitorX normally runs as an unprivileged service account.  The installer grants
    that account narrowly scoped, non-interactive sudo access for these commands.
    """
    command = [SYSTEMCTL_BIN, "--no-ask-password", action, service_name]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return None, "sudo is not installed. Re-run systemd/install-service.sh to configure service controls."
        command = [sudo, "-n", *command]
    try:
        returncode, stdout, stderr = await _run_cmd(command, timeout=30.0)
    except asyncio.TimeoutError:
        return None, f"systemctl {action} timed out after 30s (systemd busy)."
    except FileNotFoundError:
        return None, f"Could not execute {command[0]}: file not found."
    output = (stderr or stdout).decode().strip()
    if returncode:
        if "password is required" in output.lower() or "not allowed" in output.lower():
            output = "MonitorX is not authorized to control system services. Run systemd/install-service.sh, then restart MonitorX."
        return None, output or f"systemctl {action} failed (exit code {returncode})."
    return {"output": stdout.decode().strip()}, None


async def _service_sudo_allowed() -> bool:
    """Report whether the sudoers policy actually grants the exact service
    control argv MonitorX runs.

    Mirrors ``_virsh_fallback_allowed()``: ask sudo to validate the real
    command line instead of scraping ``sudo -l`` text. The previous check
    (``sudo -n -l`` returncode) only proved the user holds *some* sudo
    privilege — e.g. a full ``ALL`` grant or an unrelated apt policy — so the
    Services tab reported "available" with every action then failing 403 when
    ``/etc/sudoers.d/monitorx-systemctl`` was missing or stale.
    """
    if os.geteuid() == 0:
        return True
    sudo = shutil.which("sudo")
    if not sudo:
        return False
    # Must mirror run_service_action(): systemctl --no-ask-password <action> <unit>
    probe = [SYSTEMCTL_BIN, "--no-ask-password", "start", "monitorx-capability-probe.service"]
    try:
        proc = await asyncio.create_subprocess_exec(
            sudo, "-n", "-l", "--", *probe,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError):
        return False


@app.get("/api/services/capabilities")
async def service_capabilities():
    """Expose whether the running dashboard can execute service controls.

    Control requires the MonitorX sudoers policy (``/etc/sudoers.d/monitorx-systemctl``)
    or a root service account. The UI disables the Start/Stop/Restart/Reload
    buttons and shows this status when the policy is missing.
    """
    if await _service_sudo_allowed():
        if os.geteuid() == 0:
            return {"can_control": True, "mode": "root", "message": "Service controls are available (running as root)."}
        return {"can_control": True, "mode": "sudo", "message": "Service controls are available (sudo policy)."}
    sudo = shutil.which("sudo")
    if not sudo:
        return {"can_control": False, "mode": "unconfigured", "message": "sudo is unavailable; run the MonitorX installer."}
    return {"can_control": False, "mode": "unconfigured",
            "message": "Controls need the MonitorX sudo policy. Run systemd/install-service.sh and restart MonitorX."}


def _parse_service_units(output: bytes) -> Dict[str, Dict[str, str]]:
    """Parse ``systemctl list-units``' whitespace-delimited service rows."""
    units: Dict[str, Dict[str, str]] = {}
    for line in output.decode(errors="replace").splitlines():
        parts = line.split()
        # NAME LOAD ACTIVE SUB DESCRIPTION
        if len(parts) >= 4 and parts[0].endswith(".service"):
            units[parts[0]] = {
                "name": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                "description": " ".join(parts[4:]),
            }
    return units


def _parse_service_unit_files(output: bytes) -> Dict[str, str]:
    """Extract unit names from ``systemctl list-unit-files`` output.

    ``list-units --all`` is a *runtime* inventory: it does not include a
    disabled service until something has caused systemd to load it.  Unit files
    are the installed inventory, so merging this output is what makes stopped
    and disabled services visible in the manager too.
    """
    names: Dict[str, str] = {}
    for line in output.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(".service"):
            names[parts[0]] = parts[1]
    return names


@app.get("/api/services")
async def list_services():
    """List every installed systemd service plus its current runtime state.

    A service that is stopped/disabled is commonly absent from ``list-units``.
    We therefore merge the live list with ``list-unit-files`` and represent an
    installed-but-not-loaded unit as inactive/dead.  Active units retain their
    precise substate (running, waiting, listening, exited, and so on).
    """
    try:
        unit_result, file_result = await asyncio.gather(
            _run_cmd([SYSTEMCTL_BIN, "list-units", "--type=service", "--no-pager", "--no-legend", "--plain", "--all"], timeout=15.0),
            _run_cmd([SYSTEMCTL_BIN, "list-unit-files", "--type=service", "--no-pager", "--no-legend", "--plain"], timeout=15.0),
        )
        unit_returncode, unit_stdout, unit_stderr = unit_result
        file_returncode, file_stdout, _file_stderr = file_result
        if unit_returncode:
            raise HTTPException(status_code=503, detail=unit_stderr.decode().strip() or "systemd is unavailable")

        services = _parse_service_units(unit_stdout)
        # A unit-file listing is supplementary. Some constrained systemd
        # installations deny it, but their runtime service list is still useful.
        if file_returncode == 0:
            for name, unit_file_state in _parse_service_unit_files(file_stdout).items():
                service = services.setdefault(name, {
                    "name": name, "load": "loaded", "active": "inactive", "sub": "dead",
                    "description": "Installed service (not currently loaded)",
                })
                service["unit_file_state"] = unit_file_state
        return [services[name] for name in sorted(services, key=str.casefold)]
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="systemctl listing timed out (systemd busy?)")
    except (FileNotFoundError, PermissionError):
        raise HTTPException(status_code=503, detail="systemd is not available in this environment (systemctl not found).")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_name}/{action}")
async def control_service(service_name: str, action: str):
    if action not in SERVICE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Valid: {list(SERVICE_ACTIONS)}")
    if not SERVICE_NAME_PATTERN.fullmatch(service_name):
        raise HTTPException(status_code=400, detail="Only valid .service unit names can be controlled.")
    _, error = await run_service_action(action, service_name)
    if error:
        raise HTTPException(status_code=403, detail=error)
    return {"success": True, "message": f"Service {service_name} {service_action_label(action)}"}


# ==============================================================================
# ENHANCED TROUBLESHOOT MODE APIS
# ==============================================================================

@app.get("/api/troubleshoot/health-check")
async def troubleshoot_health_check():
    """
    Comprehensive automated system health diagnostic scanner.
    Evaluates CPU, Load, RAM, Swap, Disk Space, Inodes, Services, Zombies,
    Kernel Logs, Network, and File Descriptors.
    Calculates overall Health Score (0-100) and actionable remediation advice.
    """
    checks = []
    health_score = 100
    
    # 1. CPU & Load Average
    cpu = await get_cpu_stats()
    cores = cpu["count_logical"]
    load1 = cpu["load_1min"]
    cpu_pct = cpu["percent_total"]
    
    if cpu_pct > 85.0 or load1 > (cores * 2.0):
        health_score -= 20
        checks.append({
            "id": "cpu_load",
            "category": "CPU & Load",
            "name": "CPU & Load Spikes",
            "status": "critical",
            "value": f"{cpu_pct:.1f}% CPU, {load1:.2f} Load (Cores: {cores})",
            "message": f"CPU usage is critical ({cpu_pct:.1f}%) or 1m load ({load1}) exceeds core count by >2x.",
            "remediation": "Identify the high CPU processes under the Processes tab or Bottleneck Finder. Terminate runaway processes manually using 'kill -9 <PID>' or optimize application thread/worker pools. If possible, upgrade CPU or allocate more vCPUs to this host.",
            "action": "view_bottlenecks",
            "fix": {"action": "kill_top_cpu", "label": "💀 Kill Top CPU Process", "level": "critical", "sudo": False, "target": None},
            "fixes": [{"action": "kill_top_cpu", "label": "💀 Kill Top CPU Process", "level": "critical", "sudo": False, "target": None}],
        })
    elif cpu_pct > 70.0 or load1 > cores:
        health_score -= 8
        checks.append({
            "id": "cpu_load",
            "category": "CPU & Load",
            "name": "CPU & Load Spikes",
            "status": "warning",
            "value": f"{cpu_pct:.1f}% CPU, {load1:.2f} Load (Cores: {cores})",
            "message": f"CPU load elevated ({cpu_pct:.1f}%). System may experience latency.",
            "remediation": "Monitor active processes for unexpected threads or background operations. Terminate non-critical high-usage processes, or clear RAM cache if system load is secondary to memory thrashing.",
            "action": "view_bottlenecks",
            "fix": {"action": "kill_top_cpu", "label": "💀 Kill Top CPU Process", "level": "warning", "sudo": False, "target": None},
            "fixes": [{"action": "kill_top_cpu", "label": "💀 Kill Top CPU Process", "level": "warning", "sudo": False, "target": None}],
        })
    else:
        checks.append({
            "id": "cpu_load",
            "category": "CPU & Load",
            "name": "CPU & Load Spikes",
            "status": "ok",
            "value": f"{cpu_pct:.1f}% CPU, {load1:.2f} Load (Cores: {cores})",
            "message": "CPU load and utilization are within normal parameters.",
            "remediation": None,
            "fix": None,
        })

    # 2. Memory & Swap
    mem = await get_memory_stats()
    mem_pct = mem["percent"]
    swap_pct = mem["swap_percent"]
    avail_mb = mem["available"] / 1024 / 1024
    
    memory_fixes = [
        {"action": "clear_pagecache", "label": "⚡ Clear RAM Cache", "level": "warning", "sudo": True, "target": None},
        {"action": "system_cleanup", "label": "🧹 Run System Cleanup", "level": "warning", "sudo": True, "target": None},
    ]
    if mem_pct > 90.0 or avail_mb < 500:
        health_score -= 20
        checks.append({
            "id": "memory_swap",
            "category": "Memory",
            "name": "RAM & Swap Exhaustion",
            "status": "critical",
            "value": f"{mem_pct}% RAM used ({avail_mb:.0f} MB free), {swap_pct}% Swap",
            "message": "Memory critically low! Risk of OOM (Out Of Memory) process kills.",
            "remediation": "Clear clean page caches using the 'Clear RAM Cache' button. If usage remains high, identify top memory-consuming processes in the Processes tab and restart/terminate them, or allocate more swap space/physical RAM.",
            "fix": memory_fixes[0],
            "fixes": memory_fixes,
        })
    elif mem_pct > 80.0 or swap_pct > 50.0:
        health_score -= 8
        checks.append({
            "id": "memory_swap",
            "category": "Memory",
            "name": "RAM & Swap Exhaustion",
            "status": "warning",
            "value": f"{mem_pct}% RAM used, {swap_pct}% Swap used",
            "message": "Memory or swap usage is elevated.",
            "remediation": "Drop system page caches using the 'Clear RAM Cache' button below, or run a conservative system cleanup that also reclaims journal, temp, and package-cache space.",
            "fix": memory_fixes[0],
            "fixes": memory_fixes,
        })
    else:
        checks.append({
            "id": "memory_swap",
            "category": "Memory",
            "name": "RAM & Swap Exhaustion",
            "status": "ok",
            "value": f"{mem_pct}% RAM used, {swap_pct}% Swap used ({avail_mb:.0f} MB available)",
            "message": "System memory and swap levels are healthy.",
            "remediation": None,
            "fix": None,
        })

    # 3. Disk Space & Inodes — root filesystem (/) only. The dashboard's
    # storage monitoring intentionally covers no other mount.
    disk = await get_disk_stats()
    root_disk = disk.get("root") or (disk["partitions"][0] if disk.get("partitions") else {})
    disk_pct = float(root_disk.get("percent", 0) or 0)
    inode_pct = float(root_disk.get("inode_percent", 0) or 0)
    free_gb = root_disk.get("free", 0) / 1024 ** 3
    used_gb = root_disk.get("used", 0) / 1024 ** 3
    total_gb = root_disk.get("total", 0) / 1024 ** 3
    inodes_used = int(root_disk.get("inode_used", 0) or 0)
    inodes_free = int(root_disk.get("inode_free", 0) or 0)
    disk_critical = disk_pct > 90.0 or inode_pct > 90.0
    disk_warning = not disk_critical and (disk_pct > 80.0 or inode_pct > 80.0)
    disk_detail = (
        f"/ space {disk_pct:.1f}% used ({used_gb:.1f}G of {total_gb:.1f}G, {free_gb:.1f}G free), "
        f"inodes {inode_pct:.1f}% used ({inodes_used:,} used, {inodes_free:,} free)"
    )

    if disk_critical:
        health_score -= 20
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "critical",
            "value": disk_detail,
            "message": "Root filesystem (/) space or inodes nearly full!",
            "remediation": "Vacuum systemd journal logs to free space immediately, or clean stale temp files. Run 'sudo apt-get clean' or 'sudo yum clean all' to clear package manager cache. Run 'du -sh /* | sort -h' to find large space-consuming folders.",
            "fix": {"action": "system_cleanup", "label": "🧹 Run Full System Cleanup", "level": "critical", "sudo": True, "target": None},
            "fixes": [
                {"action": "system_cleanup", "label": "🧹 Run Full System Cleanup", "level": "critical", "sudo": True, "target": None},
                {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "warning", "sudo": True, "target": None},
                {"action": "vacuum_journal", "label": "⚡ Vacuum Old Journal Logs", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_package_cache", "label": "📦 Clean Package Cache", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_tmp", "label": "🧹 Clean Stale Temp Files", "level": "warning", "sudo": True, "target": None},
                {"action": "rotate_logs", "label": "🔄 Force Log Rotation", "level": "info", "sudo": True, "target": None},
                {"action": "autoremove_packages", "label": "🗑️ Auto-Remove Orphan Packages", "level": "info", "sudo": True, "target": None},
            ],
        })
    elif disk_warning:
        health_score -= 8
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "warning",
            "value": disk_detail,
            "message": "Root filesystem (/) disk usage high (>80% space or inodes).",
            "remediation": "Vacuum journal logs using the button below, force log rotation, or run the full system cleanup. Consider setting up automatic log rotation under '/etc/logrotate.d/' to prevent partition bloat.",
            "fix": {"action": "system_cleanup", "label": "🧹 Run Full System Cleanup", "level": "warning", "sudo": True, "target": None},
            "fixes": [
                {"action": "system_cleanup", "label": "🧹 Run Full System Cleanup", "level": "warning", "sudo": True, "target": None},
                {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "warning", "sudo": True, "target": None},
                {"action": "vacuum_journal", "label": "⚡ Vacuum Old Journal Logs", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_package_cache", "label": "📦 Clean Package Cache", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_tmp", "label": "🧹 Clean Stale Temp Files", "level": "warning", "sudo": True, "target": None},
                {"action": "rotate_logs", "label": "🔄 Force Log Rotation", "level": "info", "sudo": True, "target": None},
            ],
        })
    else:
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "ok",
            "value": disk_detail,
            "message": "Root filesystem (/) has sufficient storage and inode availability.",
            "remediation": None,
            "fix": None,
        })

    # 4. Systemd Failed Services
    failed_services = []
    try:
        returncode, stdout, _ = await _run_cmd(
            ["systemctl", "list-units", "--state=failed", "--no-pager", "--no-legend"],
            timeout=10.0,
        )
        lines = stdout.decode().strip().split('\n')
        for line in lines:
            if line.strip():
                failed_services.append(line.split()[0])
    except Exception:
        pass

    if failed_services:
        health_score -= 15 * len(failed_services)
        service_fixes = [{
            "action": "restart_failed_services",
            "label": "⚡ Restart All Failed Services",
            "level": "critical" if len(failed_services) > 1 else "warning",
            "sudo": True,
            "target": None,
        }]
        # Individual per-unit restart buttons (capped so the card stays usable).
        for unit in failed_services[:5]:
            service_fixes.append({
                "action": "restart_service",
                "label": f"↻ Restart {unit}",
                "level": "warning",
                "sudo": True,
                "target": unit,
            })
        checks.append({
            "id": "systemd_services",
            "category": "Services",
            "name": "Systemd Service Health",
            "status": "critical" if len(failed_services) > 1 else "warning",
            "value": f"{len(failed_services)} failed unit(s): {', '.join(failed_services[:3])}",
            "message": f"Found failed systemd service(s): {', '.join(failed_services)}",
            "remediation": "Restart failed services using the auto-fix buttons below. If a service repeatedly fails, inspect its logs in the Log Inspector sub-tab or run 'journalctl -u <service-name> -e' to find and troubleshoot the root cause.",
            "fix": service_fixes[0],
            "fixes": service_fixes,
        })
    else:
        checks.append({
            "id": "systemd_services",
            "category": "Services",
            "name": "Systemd Service Health",
            "status": "ok",
            "value": "0 failed services",
            "message": "All systemd units are operating normally.",
            "remediation": None,
            "fix": None,
        })

    # 5. Zombie & Disk-Sleep (D State) Processes
    # Full-table scan on purpose: the top-N process view sorts by CPU, which
    # drops the ~0% CPU zombies off the end of the list on busy hosts.
    state_counts = await _scan_process_states()
    zombies = state_counts.get("zombie", 0)
    d_states = state_counts.get("uninterruptible sleep", 0) + state_counts.get("stopped", 0)

    if zombies or d_states:
        health_score -= 10
        msg_parts = []
        if zombies: msg_parts.append(f"{zombies} zombie process(es)")
        if d_states: msg_parts.append(f"{d_states} hung/stopped process(es)")
        checks.append({
            "id": "zombie_hung",
            "category": "Processes",
            "name": "Zombie & Hung Processes",
            "status": "warning",
            "value": ", ".join(msg_parts),
            "message": f"Detected stuck process states: {', '.join(msg_parts)}.",
            "remediation": "Use the 'Reap Zombies' button below to nudge parents to reap their zombie children via SIGCHLD. For uninterruptible D-state processes, investigate disk I/O bottlenecks, network mount connectivity, or hardware errors, as they cannot be killed even with SIGKILL.",
            "fix": {"action": "reap_zombies", "label": "🧟 Reap Zombies (SIGCHLD)", "level": "warning", "sudo": False, "target": None},
        })
    else:
        checks.append({
            "id": "zombie_hung",
            "category": "Processes",
            "name": "Zombie & Hung Processes",
            "status": "ok",
            "value": "0 zombies or hung processes",
            "message": "No defunct or uninterruptible sleep processes found.",
            "remediation": None,
            "fix": None,
        })

    # 6. Kernel & Log Errors (dmesg / journalctl)
    kernel_errors = []
    try:
        returncode, stdout, _ = await _run_cmd(["dmesg", "-T"], timeout=10.0)
        out = stdout.decode().strip()
        if out:
            lines = [l for l in out.split('\n') if l.strip()]
            for l in lines[-100:]:
                if re.search(r'oom-killer|out of memory|panic|error|failed|corruption', l, re.I):
                    kernel_errors.append(l[:120])
    except Exception:
        pass

    if kernel_errors:
        health_score -= 12
        checks.append({
            "id": "kernel_logs",
            "category": "Kernel & Logs",
            "name": "Critical System Errors",
            "status": "warning",
            "value": f"{len(kernel_errors)} recent error entries in kernel buffer",
            "message": f"Recent critical error in dmesg: {kernel_errors[0]}",
            "remediation": "Review system dmesg logs via 'dmesg -T' to understand the source of the crash/error (e.g. hardware faults or OOM-killer). You can empty the dmesg buffer using 'Clear Kernel Logs' to reset this alert.",
            "fix": {"action": "clear_kernel_logs", "label": "🧹 Clear Kernel Logs", "level": "warning", "sudo": True, "target": None},
        })
    else:
        checks.append({
            "id": "kernel_logs",
            "category": "Kernel & Logs",
            "name": "Critical System Errors",
            "status": "ok",
            "value": "Clean recent kernel logs",
            "message": "No OOM-killer or kernel panic logs found recently.",
            "remediation": None,
            "fix": None,
        })

    # 7. Network & DNS Connectivity
    dns_ok = False
    ping_ok = False
    try:
        socket.gethostbyname("dns.google")
        dns_ok = True
    except Exception:
        pass

    try:
        returncode, _, _ = await _run_cmd(
            ["ping", "-c", "1", "-W", "2", "8.8.8.8"],
            timeout=10.0,
        )
        ping_ok = (returncode == 0)
    except Exception:
        pass

    if not ping_ok or not dns_ok:
        health_score -= 15
        # DNS-only breakage can be fixed by flushing the resolver cache;
        # total connectivity failure gets additional DHCP/network repairs.
        network_fixes = []
        if dns_ok is False:
            network_fixes.append({"action": "flush_dns", "label": "🌀 Flush DNS Cache", "level": "warning", "sudo": True, "target": None})
        if ping_ok is False:
            network_fixes.extend([
                {"action": "renew_dhcp_lease", "label": "🔄 Renew DHCP Lease", "level": "warning", "sudo": True, "target": None},
                {"action": "reset_network_manager", "label": "🔁 Restart Network Manager", "level": "critical", "sudo": True, "target": None},
            ])

        if not ping_ok:
            remediation = "Ping failed. Renew DHCP leases, restart NetworkManager/systemd-networkd, then verify interface state with 'ip link'. Check physical/virtual cabling and the local router if failures persist."
        else:
            remediation = "DNS failed but ping succeeded. Flush the local DNS resolver cache using the button below, renew the DHCP lease, or verify name-server settings in '/etc/resolv.conf'."

        checks.append({
            "id": "net_connectivity",
            "category": "Network",
            "name": "Network & DNS Connectivity",
            "status": "warning",
            "value": f"Ping: {'OK' if ping_ok else 'FAIL'}, DNS: {'OK' if dns_ok else 'FAIL'}",
            "message": "Network ping test or DNS resolution failed.",
            "remediation": remediation,
            "action": "run_net_diag" if not network_fixes else None,
            "fix": network_fixes[0] if network_fixes else None,
            "fixes": network_fixes or None,
        })
    else:
        checks.append({
            "id": "net_connectivity",
            "category": "Network",
            "name": "Network & DNS Connectivity",
            "status": "ok",
            "value": "Outbound Internet & DNS operational",
            "message": "Outbound networking and DNS resolution function correctly.",
            "remediation": None,
            "fix": None,
        })

    # 8. Journal disk footprint (journald can silently eat a whole partition)
    journal_usage_human = None
    journal_usage_gb = 0.0
    journal_readable = False
    try:
        returncode, stdout, _ = await _run_cmd([JOURNALCTL_BIN, "--disk-usage"], timeout=10.0)
        if returncode == 0:
            text = stdout.decode(errors="replace")
            m = re.search(r'([\d.]+)\s*([KMGTP]?)i?B', text)
            if m:
                value, unit = float(m.group(1)), m.group(2)
                mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}.get(unit.upper(), 1)
                journal_usage_gb = value * mult / (1024 ** 3)
                journal_usage_human = f"{value:.1f} {unit}B".strip()
                journal_readable = True
    except Exception:
        pass

    if journal_readable and journal_usage_gb >= 2.0:
        health_score -= 12
        checks.append({
            "id": "journal_size",
            "category": "Storage",
            "name": "Journal Disk Footprint",
            "status": "critical",
            "value": f"Journal uses {journal_usage_human} on disk",
            "message": "The systemd journal is consuming significant disk space and may pressure the root partition.",
            "remediation": "Compact the systemd journal immediately using the size-based button, or run full system cleanup. To permanently restrict journal growth, set 'SystemMaxUse=500M' in '/etc/systemd/journald.conf' and restart the service via 'sudo systemctl restart systemd-journald'.",
            "fix": {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "critical", "sudo": True, "target": None},
            "fixes": [
                {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "critical", "sudo": True, "target": None},
                {"action": "vacuum_journal", "label": "⚡ Vacuum Old Journal Logs", "level": "warning", "sudo": True, "target": None},
                {"action": "system_cleanup", "label": "🧹 Run Full System Cleanup", "level": "warning", "sudo": True, "target": None},
            ],
        })
    elif journal_readable and journal_usage_gb >= 0.5:
        health_score -= 5
        checks.append({
            "id": "journal_size",
            "category": "Storage",
            "name": "Journal Disk Footprint",
            "status": "warning",
            "value": f"Journal uses {journal_usage_human} on disk",
            "message": "The systemd journal is growing; consider vacuuming entries older than a few days.",
            "remediation": "Consider compacting the journal or vacuuming entries older than 2 days. Setting a hard limit on systemd-journal storage in '/etc/systemd/journald.conf' is highly recommended to protect server disk space.",
            "fix": {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "warning", "sudo": True, "target": None},
            "fixes": [
                {"action": "trim_journal", "label": "📉 Compact Journal to 200 MB", "level": "warning", "sudo": True, "target": None},
                {"action": "vacuum_journal", "label": "⚡ Vacuum Old Journal Logs", "level": "warning", "sudo": True, "target": None},
            ],
        })
    else:
        checks.append({
            "id": "journal_size",
            "category": "Storage",
            "name": "Journal Disk Footprint",
            "status": "ok",
            "value": journal_usage_human or "Journal usage not exposed",
            "message": "Journal disk footprint is within normal bounds.",
            "remediation": None,
            "fix": None,
        })

    # 9. Pending reboot (e.g. kernel updated under the running system)
    reboot_required = (Path("/var/run/reboot-required").exists()
                       or Path("/run/reboot-required").exists())
    if reboot_required:
        health_score -= 5
        checks.append({
            "id": "reboot_required",
            "category": "Kernel & Logs",
            "name": "Pending Reboot",
            "status": "warning",
            "value": "reboot-required marker present",
            "message": "A kernel or core package update is pending. The host should be rebooted during the next maintenance window.",
            "remediation": "Schedule a system reboot to apply core packages and kernel updates. Restart the host by running 'sudo reboot' or scheduling it via 'sudo shutdown -r +5' (warning users in 5 minutes).",
            "fix": None,
        })
    else:
        checks.append({
            "id": "reboot_required",
            "category": "Kernel & Logs",
            "name": "Pending Reboot",
            "status": "ok",
            "value": "No pending reboot",
            "message": "No reboot-required marker present.",
            "remediation": None,
            "fix": None,
        })

    # 10. File descriptor pressure (informational; no automated fix)
    try:
        with open("/proc/sys/fs/file-nr") as fh:
            parts = fh.read().split()
        if len(parts) >= 3:
            allocated, max_fds = int(parts[0]), int(parts[2])
            fd_pct = (allocated / max_fds * 100) if max_fds else 0.0
            if fd_pct > 80.0:
                health_score -= 5
                checks.append({
                    "id": "file_descriptors",
                    "category": "Processes",
                    "name": "File Descriptor Pressure",
                    "status": "warning",
                    "value": f"{allocated:,} / {max_fds:,} fds ({fd_pct:.0f}%)",
                    "message": "The host is using a large share of its file-descriptor table; runaway processes should be inspected.",
                    "remediation": "Run 'lsof -n | awk \'{print $1}\' | sort | uniq -c | sort -rn | head' in terminal to identify processes with excessive open file handles. Restart the leaking process or service, or increase open file limits in '/etc/security/limits.conf'.",
                    "action": "view_processes",
                    "fix": None,
                })
            else:
                checks.append({
                    "id": "file_descriptors",
                    "category": "Processes",
                    "name": "File Descriptor Pressure",
                    "status": "ok",
                    "value": f"{allocated:,} / {max_fds:,} fds ({fd_pct:.0f}%)",
                    "message": "File descriptor usage is within normal bounds.",
                    "remediation": None,
                    "fix": None,
                })
    except Exception:
        pass

    # 12. CPU thermal / overheating detection
    # Hot CPUs auto-throttle to survive, which silently tanks performance and
    # shortens hardware life — so catching sustained high core temps is valuable.
    therm_sensors = []
    try:
        if hasattr(psutil, "sensors_temperatures"):
            for key, entries in (psutil.sensors_temperatures() or {}).items():
                for e in entries:
                    if getattr(e, "current", None) is not None:
                        label = (e.label or key).strip() or key
                        therm_sensors.append((label, float(e.current)))
        if not therm_sensors:
            for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
                try:
                    with open(path) as fh:
                        raw = float(fh.read().strip())
                    therm_sensors.append((os.path.basename(os.path.dirname(path)), raw / 1000.0))
                except Exception:
                    pass
    except Exception:
        therm_sensors = []

    if therm_sensors:
        max_temp = max(t[1] for t in therm_sensors)
        hottest = max(therm_sensors, key=lambda t: t[1])
        value_str = ", ".join(f"{l}={v:.0f}°C" for l, v in therm_sensors[:3])
        if max_temp > 90.0:
            health_score -= 15
            checks.append({
                "id": "thermal",
                "category": "Hardware",
                "name": "CPU Thermal / Overheating",
                "status": "critical",
                "value": value_str,
                "message": f"Core temperature critical ({hottest[0]} at {max_temp:.0f}°C). The CPU is likely thermal-throttling and degrading performance.",
                "remediation": "Inspect cooling (fans, thermal paste, airflow), check 'sensors' output, and review 'dmesg -T | grep -i thermal' for throttling events. Reduce sustained load or shut the host down to cool it before further operation.",
                "action": None,
                "fix": None,
            })
        elif max_temp > 80.0:
            health_score -= 8
            checks.append({
                "id": "thermal",
                "category": "Hardware",
                "name": "CPU Thermal / Overheating",
                "status": "warning",
                "value": value_str,
                "message": f"Core temperature elevated ({hottest[0]} at {max_temp:.0f}°C). Approaching the throttle threshold.",
                "remediation": "Monitor temperatures with 'watch -n 1 sensors'. Improve cooling or lower sustained workload before the CPU begins throttling.",
                "action": None,
                "fix": None,
            })
        else:
            checks.append({
                "id": "thermal",
                "category": "Hardware",
                "name": "CPU Thermal / Overheating",
                "status": "ok",
                "value": f"Peak {max_temp:.0f}°C",
                "message": f"Core temperatures are within normal bounds (peak {hottest[0]} at {max_temp:.0f}°C).",
                "remediation": None,
                "fix": None,
            })

    # 13. Network interface errors & drops
    # /proc/net/dev reports per-interface RX/TX error and dropped-frame counters;
    # persistent growth here signals flapping links, duplex mismatch or NIC issues.
    net_errors = []
    try:
        with open("/proc/net/dev") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                fields = rest.split()
                if len(fields) >= 16:
                    rx_err, rx_drop = int(fields[2]), int(fields[3])
                    tx_err, tx_drop = int(fields[10]), int(fields[11])
                    total = rx_err + rx_drop + tx_err + tx_drop
                    if total > 0:
                        net_errors.append((iface, rx_err, rx_drop, tx_err, tx_drop))
    except Exception:
        net_errors = []

    if net_errors:
        health_score -= 8
        detail = ", ".join(f"{i}: {rxerr} RXerr/{rxd} RXdrop/{txerr} TXerr/{txd} TXdrop"
                           for i, rxerr, rxd, txerr, txd in net_errors)
        checks.append({
            "id": "net_errors",
            "category": "Network",
            "name": "Interface Errors & Drops",
            "status": "warning",
            "value": f"{sum(e[1]+e[2]+e[3]+e[4] for e in net_errors)} cumulative errors on {len(net_errors)} interface(s)",
            "message": detail,
            "remediation": "Run 'ip -s link' and 'ethtool -S <iface>' to inspect counters. Persistent RX/TX errors usually indicate a duplex mismatch, bad cable/connector, or driver issues. Check 'dmesg | grep -iE \"link|eth|nic\"' for interface events.",
            "action": None,
            "fix": None,
        })
    else:
        checks.append({
            "id": "net_errors",
            "category": "Network",
            "name": "Interface Errors & Drops",
            "status": "ok",
            "value": "No RX/TX errors or drops on active interfaces",
            "message": "Network interface error and drop counters are clean.",
            "remediation": None,
            "fix": None,
        })

    # 14. Process & thread pressure (thread exhaustion can mimic resource starvation)
    threads_max = None
    threads_total = None
    try:
        with open("/proc/sys/kernel/threads-max") as fh:
            threads_max = int(fh.read().split()[0])
        threads_total = await asyncio.to_thread(_count_threads_sync)
    except Exception:
        threads_max = threads_total = None

    if threads_max and threads_total is not None:
        thread_pct = (threads_total / threads_max * 100)
        if thread_pct > 80.0:
            health_score -= 8
            checks.append({
                "id": "thread_pressure",
                "category": "Processes",
                "name": "Process / Thread Pressure",
                "status": "warning",
                "value": f"{threads_total:,} threads of {threads_max:,} allowed ({thread_pct:.0f}%)",
                "message": "The host is approaching its maximum thread count; thread exhaustion can stall services and cause 'resource temporarily unavailable' errors.",
                "remediation": "Find the processes consuming the most threads (view the Threads column in the Process Manager). Restart runaway multi-threaded apps, raise 'ulimit -u' / tasks in '/etc/security/limits.conf', or reduce worker pool sizes.",
                "action": "view_processes",
                "fix": None,
            })
        else:
            checks.append({
                "id": "thread_pressure",
                "category": "Processes",
                "name": "Process / Thread Pressure",
                "status": "ok",
                "value": f"{threads_total:,} threads of {threads_max:,} allowed ({thread_pct:.0f}%)",
                "message": "Process and thread counts are well within kernel limits.",
                "remediation": None,
                "fix": None,
            })

    # 15. Load trend (rising load often precedes a problem, even below a hard cap)
    load1 = cpu["load_1min"]
    load5 = cpu["load_5min"]
    load15 = cpu["load_15min"]
    rising = load1 > cores and load1 > load15 * 1.5 and load1 > load5
    if rising:
        health_score -= 8
        checks.append({
            "id": "load_trend",
            "category": "CPU & Load",
            "name": "Load Trend / Saturation",
            "status": "warning",
            "value": f"Load rising: 1m={load1:.2f} / 5m={load5:.2f} / 15m={load15:.2f} (cores: {cores})",
            "message": "Short-term load is climbing sharply above the 15-minute average and already exceeds core count — an early indicator of a developing bottleneck.",
            "remediation": "Open the Bottleneck Finder to see which processes are spiking. Investigate cron jobs, scheduled backups, or a service restarted in a tight loop before the system becomes fully saturated.",
            "action": "view_bottlenecks",
            "fix": None,
        })
    elif load1 <= cores * 0.7:
        checks.append({
            "id": "load_trend",
            "category": "CPU & Load",
            "name": "Load Trend / Saturation",
            "status": "ok",
            "value": f"Load stable: 1m={load1:.2f} / 5m={load5:.2f} / 15m={load15:.2f} (cores: {cores})",
            "message": "Load is stable and comfortably under the core count.",
            "remediation": None,
            "fix": None,
        })

    # 16. NTP time synchronization
    # Drift or a lost sync link causes log correlation and TLS/authentication
    # failures that are hard to diagnose elsewhere.
    ntp_synced = None
    try:
        rc, stdout, _ = await _run_cmd(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"], timeout=8.0)
        if rc == 0:
            for field in stdout.decode(errors="replace").splitlines():
                if field.startswith("NTPSynchronized="):
                    ntp_synced = field.split("=", 1)[1].strip().lower() == "yes"
    except Exception:
        pass

    if ntp_synced is False:
        health_score -= 6
        checks.append({
            "id": "time_sync",
            "category": "Kernel & Logs",
            "name": "NTP Time Synchronization",
            "status": "warning",
            "value": "Clock not synchronized",
            "message": "The system clock is not synchronized with an NTP source. Drift can break TLS validation, Kerberos/auth, and cross-host log correlation.",
            "remediation": "Enable NTP with the button below, then verify the service via 'systemctl status systemd-timesyncd' (or chronyd). Verify with 'timedatectl'.",
            "fix": {"action": "enable_ntp", "label": "🕒 Enable NTP Sync", "level": "warning", "sudo": True, "target": None},
            "fixes": [{"action": "enable_ntp", "label": "🕒 Enable NTP Sync", "level": "warning", "sudo": True, "target": None}],
        })
    elif ntp_synced:
        checks.append({
            "id": "time_sync",
            "category": "Kernel & Logs",
            "name": "NTP Time Synchronization",
            "status": "ok",
            "value": "Clock synchronized via NTP",
            "message": "The system clock is synchronized with an NTP source.",
            "remediation": None,
            "fix": None,
        })

    # 17. Security: recent failed authentication attempts
    # A spike in failed SSH/password attempts usually indicates a brute-force
    # scan in progress and is worth surfacing in the hub.
    auth_log = None
    for candidate in ("/var/log/auth.log", "/var/log/secure"):
        if os.path.exists(candidate):
            auth_log = candidate
            break
    failed_auth = None
    if auth_log:
        try:
            with open(auth_log, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                text = fh.read().decode(errors="replace")
            failed_auth = len(re.findall(r"Failed password", text))
        except Exception:
            failed_auth = None

    if failed_auth is not None:
        if failed_auth >= 100:
            health_score -= 12
            checks.append({
                "id": "security_auth",
                "category": "Security",
                "name": "Failed Login Attempts",
                "status": "critical",
                "value": f"{failed_auth} failed password attempts in recent auth log",
                "message": "A high volume of failed login attempts detected — likely an active brute-force attack against this host.",
                "remediation": "Consider installing fail2ban, tighten 'MaxAuthTries' and 'PermitRootLogin' in '/etc/ssh/sshd_config', and review the source addresses in the auth log. Restrict SSH with firewalld/ufw if exposed to the internet.",
                "action": None,
                "fix": None,
            })
        elif failed_auth >= 20:
            health_score -= 6
            checks.append({
                "id": "security_auth",
                "category": "Security",
                "name": "Failed Login Attempts",
                "status": "warning",
                "value": f"{failed_auth} failed password attempts in recent auth log",
                "message": "An elevated number of failed login attempts has been recorded.",
                "remediation": "Review '/var/log/auth.log' (or '/var/log/secure') for the source addresses. Consider fail2ban to auto-block repeat offenders.",
                "action": None,
                "fix": None,
            })
        else:
            checks.append({
                "id": "security_auth",
                "category": "Security",
                "name": "Failed Login Attempts",
                "status": "ok",
                "value": f"{failed_auth} failed password attempt(s) in recent auth log",
                "message": "No abnormal volume of failed login attempts.",
                "remediation": None,
                "fix": None,
            })

    health_score = max(0, min(100, health_score))

    # Record this scan's score for the trend sparkline (capped ring buffer).
    _HEALTH_HISTORY.append({
        "score": health_score,
        "critical": sum(1 for c in checks if c["status"] == "critical"),
        "warning": sum(1 for c in checks if c["status"] == "warning"),
        "timestamp": datetime.now().isoformat(),
    })
    
    status_summary = {
        "critical": sum(1 for c in checks if c["status"] == "critical"),
        "warning": sum(1 for c in checks if c["status"] == "warning"),
        "ok": sum(1 for c in checks if c["status"] == "ok")
    }

    return {
        "health_score": health_score,
        "summary": status_summary,
        "timestamp": datetime.now().isoformat(),
        "checks": checks
    }


@app.get("/api/troubleshoot/history")
async def troubleshoot_history(limit: int = Query(60, ge=1, le=120)):
    """
    Return the rolling System Health Index history (score over recent scans)
    so the hub can render a trend sparkline and show whether the host is
    improving or degrading over time.
    """
    snapshots = list(_HEALTH_HISTORY)
    return {"history": snapshots[-limit:]}


@app.get("/api/troubleshoot/logs")
async def troubleshoot_logs(
    lines: int = Query(100, ge=1, le=1000),
    level: str = Query("all"),
    service: str = Query(""),
    search: str = Query("")
):
    """
    Enhanced log inspector with level filtering, unit selection, and keyword search.
    Fallback to dmesg if journalctl lacks permissions.
    """
    raw_logs = []
    search = search[:128]
    if service and not SERVICE_NAME_PATTERN.fullmatch(service):
        raise HTTPException(status_code=400, detail="Only valid .service unit names can be inspected.")
    
    cmd = [JOURNALCTL_BIN, "-n", str(lines), "--no-pager"]
    if level == "error":
        cmd.extend(["-p", "3"])
    elif level == "warning":
        cmd.extend(["-p", "4"])
    elif level == "info":
        cmd.extend(["-p", "6"])
    if service:
        cmd.extend(["-u", service])

    try:
        try:
            returncode, stdout, _ = await _run_cmd(cmd, timeout=15.0)
            out_str = stdout.decode().strip()

            if "No journal files were opened due to insufficient permissions" in out_str or not out_str:
                try:
                    returncode_d, d_out, _ = await _run_cmd(["dmesg", "-T"], timeout=10.0)
                    d_lines = [l for l in d_out.decode(errors="replace")[-MAX_DIAGNOSTIC_OUTPUT:].strip().split('\n') if l.strip()]
                    raw_logs = d_lines[-lines:] if d_lines else []
                except Exception:
                    raw_logs = [out_str or "Unable to read system logs due to permissions"]
            else:
                raw_logs = [l for l in out_str.split('\n') if not l.startswith("Hint:")]
        except asyncio.TimeoutError:
            raw_logs = ["Log read timed out after 15s. The journal may be under heavy write load."]

        parsed_logs = []
        search_pattern = None
        if search:
            try:
                search_pattern = re.compile(search, re.IGNORECASE)
            except re.error:
                # A malformed pattern should not make the inspector fail;
                # fall back to a literal, escaped search instead.
                search_pattern = re.compile(re.escape(search), re.IGNORECASE)
        
        for line in raw_logs:
            if not line:
                continue
            if search_pattern and not search_pattern.search(line):
                continue
            
            log_level = "info"
            if re.search(r'error|fail|critical|panic|fatal|oom|corrupt', line, re.I):
                log_level = "error"
            elif re.search(r'warn|alert|denied|timeout|retry', line, re.I):
                log_level = "warning"

            if level == "all" or level == log_level:
                parsed_logs.append({
                    "text": line,
                    "level": log_level
                })

        return {
            "total": len(parsed_logs),
            "lines": lines,
            "level": level,
            "service": service,
            "logs": parsed_logs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/troubleshoot/ping")
async def troubleshoot_ping(req: PingRequest):
    """Run ICMP ping test against specified host"""
    host = req.host.strip()
    if not re.match(r'^[a-zA-Z0-9.-]+$', host):
        raise HTTPException(status_code=400, detail="Invalid hostname or IP address format")
    
    count = req.count
    try:
        returncode, stdout, stderr = await _run_cmd(
            ["ping", "-c", str(count), "-W", "3", host],
            timeout=15.0,
        )
        out = stdout.decode()
        
        loss_match = re.search(r'(\d+)% packet loss', out)
        rtt_match = re.search(r'(rtt|round-trip) min/avg/max/(mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)', out)
        
        return {
            "success": returncode == 0,
            "host": host,
            "raw_output": out or stderr.decode(),
            "packet_loss_percent": float(loss_match.group(1)) if loss_match else (0.0 if returncode == 0 else 100.0),
            "min_rtt": float(rtt_match.group(3)) if rtt_match else None,
            "avg_rtt": float(rtt_match.group(4)) if rtt_match else None,
            "max_rtt": float(rtt_match.group(5)) if rtt_match else None
        }
    except asyncio.TimeoutError:
        return {"success": False, "host": host, "error": "Ping timed out after 15s"}
    except Exception as e:
        return {"success": False, "host": host, "error": str(e)}


@app.post("/api/troubleshoot/port-check")
async def troubleshoot_port_check(req: PortCheckRequest):
    """Test TCP port connectivity"""
    host = req.host.strip()
    port = req.port
    if not re.match(r'^[a-zA-Z0-9.-]+$', host) or not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail="Invalid host or port range")
    
    timeout = req.timeout
    start_time = time.time()
    try:
        conn = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(conn, timeout=timeout)
        latency_ms = round((time.time() - start_time) * 1000, 2)
        writer.close()
        await writer.wait_closed()
        return {
            "host": host,
            "port": port,
            "open": True,
            "latency_ms": latency_ms,
            "message": f"Port {port} on {host} is OPEN ({latency_ms} ms)"
        }
    except asyncio.TimeoutError:
        return {
            "host": host,
            "port": port,
            "open": False,
            "latency_ms": None,
            "message": f"Connection to {host}:{port} timed out after {timeout}s"
        }
    except Exception as e:
        return {
            "host": host,
            "port": port,
            "open": False,
            "latency_ms": None,
            "message": f"Closed / unreachable: {str(e)}"
        }


@app.post("/api/troubleshoot/dns-lookup")
async def troubleshoot_dns_lookup(req: DNSCheckRequest):
    """Test DNS resolution across local resolver and Google Public DNS"""
    domain = req.domain.strip()
    if not re.match(r'^[a-zA-Z0-9.-]+$', domain):
        raise HTTPException(status_code=400, detail="Invalid domain name format")
    
    results = {}
    
    # Local resolution
    start_time = time.time()
    try:
        loop = asyncio.get_running_loop()
        addrs = await loop.getaddrinfo(domain, None)
        ips = list(set([a[4][0] for a in addrs]))
        latency_ms = round((time.time() - start_time) * 1000, 2)
        results["local"] = {"success": True, "ips": ips, "latency_ms": latency_ms}
    except Exception as e:
        results["local"] = {"success": False, "error": str(e)}

    # Google DNS
    try:
        returncode, stdout, _ = await _run_cmd(
            ["dig", "+short", "+time=2", "@8.8.8.8", domain],
            timeout=10.0,
        )
        out = stdout.decode().strip()
        if out:
            results["google_dns"] = {"success": True, "ips": [line.strip() for line in out.split('\n') if line.strip()]}
        else:
            results["google_dns"] = {"success": False, "error": "No response"}
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        results["google_dns"] = {"success": False, "error": "dig tool unavailable"}

    return {"domain": domain, "resolutions": results}


@app.get("/api/troubleshoot/network-ports")
async def troubleshoot_network_ports():
    """List active listening ports with bound address and process mapping"""
    ports = []
    
    try:
        connections = await asyncio.to_thread(_list_inet_connections_sync)
        for conn in connections:
            if conn.status == 'LISTEN':
                ip, port = conn.laddr
                pid = conn.pid
                proc_name = "unknown"
                if pid:
                    try:
                        proc_name = psutil.Process(pid).name()
                    except Exception:
                        pass
                ports.append({
                    "port": port,
                    "protocol": "TCP" if conn.type == socket.SOCK_STREAM else "UDP",
                    "ip": ip,
                    "pid": pid,
                    "process": proc_name
                })
    except Exception:
        try:
            returncode, stdout, _ = await _run_cmd(["ss", "-tulpn"], timeout=10.0)
            lines = stdout.decode().strip().split('\n')
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    proto = parts[0].upper()
                    laddr = parts[4]
                    ip = laddr.rsplit(':', 1)[0] if ':' in laddr else '*'
                    port_str = laddr.rsplit(':', 1)[1] if ':' in laddr else ''
                    if port_str.isdigit():
                        p_name = ""
                        p_id = None
                        if len(parts) >= 7 and 'users:' in parts[6]:
                            match = re.search(r'\(\("([^"]+)",pid=(\d+)', parts[6])
                            if match:
                                p_name = match.group(1)
                                p_id = int(match.group(2))
                        ports.append({
                            "port": int(port_str),
                            "protocol": proto,
                            "ip": ip,
                            "pid": p_id,
                            "process": p_name or "unknown"
                        })
        except (asyncio.TimeoutError, Exception) as e:
            logger.error(f"Error getting listening ports: {e}")
            
    ports.sort(key=lambda x: x["port"])
    return ports


def _prime_cpu_samplers_sync():
    """Blocking walk that initialises psutil's per-process CPU sampler."""
    procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.cpu_percent()  # initialise the per-process sampler
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return procs


def _list_zombies_sync():
    """Blocking walk returning (pid, name, ppid) for every zombie process."""
    zombies = []
    for proc in psutil.process_iter(['pid', 'name', 'ppid', 'status']):
        try:
            status = (proc.info['status'] or '').lower()
            if status in ('zombie', 'defunct'):
                zombies.append((proc.info['pid'], proc.info['name'] or '?', proc.info['ppid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return zombies


def _collect_bottleneck_procs_sync() -> List[Dict[str, Any]]:
    """Blocking full process walk for the bottleneck view."""
    procs: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'num_threads', 'username']):
        try:
            info = proc.info
            procs.append({
                "pid": info['pid'],
                "name": info['name'] or "unknown",
                "cpu_percent": round(info['cpu_percent'] or 0.0, 1),
                "memory_percent": round(info['memory_percent'] or 0.0, 1),
                "memory_mb": round((info['memory_info'].rss / 1024 / 1024) if info['memory_info'] else 0.0, 1),
                "status": info['status'] or "unknown",
                "threads": info['num_threads'] or 1,
                "username": info['username'] or "unknown"
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return procs


@app.get("/api/troubleshoot/bottlenecks")
async def troubleshoot_bottlenecks():
    """
    Identifies top CPU, Memory, and Thread resource bottlenecks
    along with stuck processes.
    """
    procs = await asyncio.to_thread(_collect_bottleneck_procs_sync)

    cpu_hogs = sorted(procs, key=lambda x: x['cpu_percent'], reverse=True)[:5]
    mem_hogs = sorted(procs, key=lambda x: x['memory_mb'], reverse=True)[:5]
    thread_hogs = sorted(procs, key=lambda x: x['threads'], reverse=True)[:5]
    zombie_list = [p for p in procs if p['status'] in ('zombie', 'stopped', 'uninterruptible sleep')]

    return {
        "cpu_hogs": cpu_hogs,
        "memory_hogs": mem_hogs,
        "thread_hogs": thread_hogs,
        "stuck_processes": zombie_list
    }


# ==============================================================================
# AUTO-FIX ENGINE (REMEDIATION REGISTRY)
#
# Every action the Troubleshoot Hub can perform lives in FIX_ACTION_META and
# FIX_EXECUTORS.  The health scanner attaches `fix` / `fixes` metadata to each
# failing check, the UI renders one button per fix, and POST /fix-all executes
# a whole repair plan sequentially.  All executions are audited through
# audit_operation() so the hub can show a full remediation history.
# ==============================================================================

FIX_ACTION_META = {
    "clear_pagecache": {
        "label": "Clear RAM page cache",
        "category": "Memory",
        "level": "warning",
        "sudo": True,
        "description": "Writes 3 to vm.drop_caches so clean page-cache pages are released back to the OS. Cached file data is simply re-read from disk when needed again — this is a safe, reversible operation.",
    },
    "vacuum_journal": {
        "label": "Vacuum systemd journal",
        "category": "Storage",
        "level": "warning",
        "sudo": True,
        "description": "Runs `journalctl --vacuum-time=2d` to remove journal entries older than 2 days and free disk space on the journal partition.",
    },
    "restart_failed_services": {
        "label": "Restart all failed services",
        "category": "Services",
        "level": "critical",
        "sudo": True,
        "description": "Restarts every systemd unit currently in the `failed` state. Only valid .service units are touched.",
    },
    "restart_service": {
        "label": "Restart service",
        "category": "Services",
        "level": "warning",
        "sudo": True,
        "description": "Restarts the target systemd service unit.",
    },
    "start_service": {
        "label": "Start service",
        "category": "Services",
        "level": "warning",
        "sudo": True,
        "description": "Starts the target systemd service unit.",
    },
    "enable_service": {
        "label": "Enable service on boot",
        "category": "Services",
        "level": "info",
        "sudo": True,
        "description": "Enables the target systemd service unit so it starts automatically at boot.",
    },
    "clear_kernel_logs": {
        "label": "Clear kernel error buffer",
        "category": "Kernel & Logs",
        "level": "warning",
        "sudo": True,
        "description": "Runs `dmesg -C` to empty the kernel ring buffer that the scanner flags for recent OOM / panic / error entries.",
    },
    "kill_process": {
        "label": "Terminate process",
        "category": "Processes",
        "level": "critical",
        "sudo": False,
        "description": "Force-terminates the target PID. Only processes owned by the dashboard user can be killed (unless running as root).",
    },
    "kill_top_cpu": {
        "label": "Kill top CPU process",
        "category": "CPU & Load",
        "level": "critical",
        "sudo": False,
        "description": "Terminates the single highest-CPU non-essential process to relieve a load spike. PID 1, the MonitorX process tree, and essential services (systemd, sshd, libvirtd, NetworkManager, ...) are never targeted, and the kill is owner-guarded exactly like a manual kill.",
    },
    "reap_zombies": {
        "label": "Reap zombie processes",
        "category": "Processes",
        "level": "warning",
        "sudo": False,
        "description": "Sends SIGCHLD to the parent of every zombie process so the parent reaps its dead children. Zero risk to the parent itself.",
    },
    "flush_dns": {
        "label": "Flush DNS resolver cache",
        "category": "Network",
        "level": "info",
        "sudo": True,
        "description": "Clears the local DNS cache via resolvectl / systemd-resolve / nscd. Fixes stale-resolution issues; fully reversible.",
    },
    "clean_tmp": {
        "label": "Clean stale temp files",
        "category": "Storage",
        "level": "warning",
        "sudo": True,
        "description": "Deletes regular files under /tmp and /var/tmp not modified for 7+ days (max depth 2). Frees disk space without touching active sessions.",
    },
    "enable_ntp": {
        "label": "Enable NTP time synchronization",
        "category": "System",
        "level": "warning",
        "sudo": True,
        "description": "Runs `timedatectl set-ntp true` so systemd-timesyncd/chronyd keeps the host clock synchronized. Prevents TLS, Kerberos, authentication, and log-correlation failures caused by clock drift.",
    },
    "clean_package_cache": {
        "label": "Clean package manager cache",
        "category": "Storage",
        "level": "warning",
        "sudo": True,
        "description": "Runs the host package manager's safe cache cleanup (`apt-get clean`, `dnf clean all`, `yum clean all`, or `zypper clean`). Removes downloaded installer archives without uninstalling packages.",
    },
    "autoremove_packages": {
        "label": "Auto-remove orphan packages",
        "category": "Storage",
        "level": "info",
        "sudo": True,
        "description": "Runs `apt-get autoremove --purge -y` (or the dnf/yum equivalent) to remove unused dependencies and old kernels. This is a system-wide cleanup action and is shown separately so it is never applied accidentally.",
    },
    "trim_journal": {
        "label": "Compact journal to 200 MB",
        "category": "Storage",
        "level": "warning",
        "sudo": True,
        "description": "Runs `journalctl --vacuum-size=200M` when journal disk usage is large. Complements time-based vacuuming by immediately capping retained logs on space-constrained hosts.",
    },
    "rotate_logs": {
        "label": "Force log rotation",
        "category": "Storage",
        "level": "info",
        "sudo": True,
        "description": "Runs `logrotate --force /etc/logrotate.conf` to rotate and compress oversized application logs. Useful when active logs, rather than the journal, are consuming root filesystem space.",
    },
    "reset_network_manager": {
        "label": "Restart network manager",
        "category": "Network",
        "level": "critical",
        "sudo": True,
        "description": "Restarts NetworkManager (or systemd-networkd) to recover stale device state, stuck DHCP leases, and resolver problems after flushing DNS. Brief connectivity interruption is expected.",
    },
    "renew_dhcp_lease": {
        "label": "Renew DHCP leases",
        "category": "Network",
        "level": "warning",
        "sudo": True,
        "description": "Uses NetworkManager or dhclient to renew IPv4 DHCP leases on active non-loopback interfaces. Helps recover expired addresses, bad DNS assignments, and stale default routes.",
    },
    "system_cleanup": {
        "label": "Run full system cleanup",
        "category": "System",
        "level": "warning",
        "sudo": True,
        "description": "Runs a conservative whole-system cleanup in one action: vacuums journal logs, cleans stale temp files, cleans the package manager cache, forces log rotation, and drops clean RAM caches. It never uninstalls packages or restarts workloads.",
    },
}

# Aliases accepted by the remediate endpoint for backwards compatibility.
FIX_ACTION_ALIASES = {
    "clear_dmesg": "clear_kernel_logs",
    "clear_logs": "clear_kernel_logs",
    "clear_kernel_buffer": "clear_kernel_logs",
}


async def _fix_clear_pagecache(target: Optional[str] = None) -> Dict[str, Any]:
    cmd = [SYSCTL_BIN, "-w", "vm.drop_caches=3"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        returncode, stdout, stderr = await _run_cmd(cmd, timeout=15.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Page cache clear timed out"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": "RAM page cache cleared successfully!"}
    return {"success": False, "message": f"Sudo permissions required: {stderr.decode().strip() or 'Access denied'}"}


async def _fix_vacuum_journal(target: Optional[str] = None) -> Dict[str, Any]:
    days = target if target and target.isdigit() and 1 <= int(target) <= 30 else 2
    cmd = [JOURNALCTL_BIN, f"--vacuum-time={days}d"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        returncode, stdout, stderr = await _run_cmd(cmd, timeout=60.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Journal vacuum timed out after 60s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    return {"success": returncode == 0, "message": stdout.decode().strip() or stderr.decode().strip()}


async def _fix_restart_failed_services(target: Optional[str] = None) -> Dict[str, Any]:
    try:
        returncode, stdout, _ = await _run_cmd(
            [SYSTEMCTL_BIN, "list-units", "--state=failed", "--no-pager", "--no-legend"],
            timeout=10.0,
        )
    except (asyncio.TimeoutError, Exception):
        return {"success": False, "message": "Failed-service scan could not run (systemd unavailable?)."}
    failed_units = [line.split()[0] for line in stdout.decode().strip().split('\n') if line.strip()]

    if not failed_units:
        return {"success": True, "message": "No failed services to restart."}

    restarted, failed_restarts, errors = [], [], []
    for unit in failed_units:
        if not SERVICE_NAME_PATTERN.fullmatch(unit):
            failed_restarts.append(unit)
            errors.append(f"{unit}: not a controllable .service unit")
            continue
        _, error = await run_service_action("restart", unit)
        if error:
            failed_restarts.append(unit)
            errors.append(f"{unit}: {error}")
        else:
            restarted.append(unit)

    return {
        "success": not failed_restarts,
        "message": f"Attempted restart of {len(failed_units)} unit(s). Success: {len(restarted)}",
        "restarted": restarted,
        "failed": failed_restarts,
        "errors": errors,
    }


async def _fix_service_action(action: str, target: Optional[str] = None) -> Dict[str, Any]:
    if not target or not SERVICE_NAME_PATTERN.fullmatch(target):
        return {"success": False, "message": f"Invalid service target: {target or '(none)'}"}
    result, error = await run_service_action(action, target)
    if error:
        return {"success": False, "message": error}
    return {"success": True, "message": f"Service {target} {service_action_label(action)}."}


async def _fix_clear_kernel_logs(target: Optional[str] = None) -> Dict[str, Any]:
    cmd = ["sudo", "-n", DMESG_BIN, "-C"] if os.geteuid() != 0 else [DMESG_BIN, "-C"]
    try:
        returncode, stdout, stderr = await _run_cmd(cmd, timeout=10.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Kernel log clear timed out"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": "Kernel error buffer and logs cleared successfully!"}
    try:
        fb_rc, _, _ = await _run_cmd([DMESG_BIN, "-C"], timeout=10.0)
    except asyncio.TimeoutError:
        fb_rc = 1
    if fb_rc == 0:
        return {"success": True, "message": "Kernel error buffer and logs cleared successfully!"}
    return {"success": False, "message": f"Sudo permissions required: {stderr.decode().strip() or 'Access denied'}"}


async def _fix_kill_process(target: Optional[str] = None) -> Dict[str, Any]:
    if not target or not target.isdigit():
        return {"success": False, "message": "Target PID required"}
    pid = int(target)
    try:
        proc = psutil.Process(pid)
        # Multi-user safety: never kill a process owned by another user
        # unless the dashboard itself runs as root.
        if os.geteuid() != 0:
            try:
                if proc.uids().effective != os.geteuid():
                    return {"success": False, "message": f"PID {pid} belongs to another user; termination denied."}
            except psutil.AccessDenied:
                return {"success": False, "message": f"Cannot verify ownership of PID {pid}; termination denied."}
        pname = proc.name()
        proc.kill()
        return {"success": True, "message": f"Terminated process {pid} ({pname})"}
    except psutil.NoSuchProcess:
        return {"success": False, "message": f"Process {pid} no longer active"}
    except psutil.AccessDenied:
        return {"success": False, "message": f"Permission denied to terminate PID {pid}"}


async def _fix_kill_top_cpu(target: Optional[str] = None) -> Dict[str, Any]:
    """Remediate a CPU/load spike by terminating the single highest-CPU
    non-essential process owned by the dashboard user (or any process when
    running as root).

    Safety mirrors a manual kill: PID 1, the MonitorX process tree, and a
    small essential-services blocklist are never targeted, and the actual
    kill re-uses the owner-guarded ``_fix_kill_process`` executor. CPU usage
    is sampled with two passes over a single short sleep so the scan stays
    bounded regardless of how many processes are running.
    """
    ESSENTIAL_NAMES = {
        "systemd", "init", "kthreadd", "kernel",
        "libvirtd", "sshd", "dbus-daemon", "dbus-broker", "udevd",
        "systemd-journald", "systemd-udevd", "cron", "rsyslogd", "rsyslog",
        "chronyd", "systemd-resolved", "NetworkManager", "networkmanager",
    }
    my_pid = os.getpid()
    my_uid = os.geteuid()

    procs = await asyncio.to_thread(_prime_cpu_samplers_sync)

    # One shared measurement window for every process.
    await asyncio.sleep(0.25)

    candidates = []
    for proc in procs:
        try:
            cpu = proc.cpu_percent()
            pid = proc.pid
            if pid == 1 or pid == my_pid:
                continue
            # Never target MonitorX's own child/worker processes.
            try:
                if proc.ppid() == my_pid:
                    continue
            except Exception:
                pass
            if (proc.info.get("name") or "").lower() in ESSENTIAL_NAMES:
                continue
            # CRITICAL FIX: Re-validate ownership *immediately before kill*
            # (root cause: race between sampling and termination)
            if my_uid != 0:
                try:
                    if proc.uids().effective != my_uid:
                        continue
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
            candidates.append((pid, proc.info.get("name") or "?", cpu))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not candidates:
        return {"success": True, "message": "No user-owned, non-essential process to throttle."}

    candidates.sort(key=lambda x: x[2], reverse=True)
    top_pid, top_name, top_cpu = candidates[0]
    result = await _fix_kill_process(str(top_pid))
    if result.get("success"):
        result["message"] = f"Terminated top CPU process {top_pid} ({top_name}, {top_cpu:.1f}% CPU)."
    return result


async def _fix_reap_zombies(target: Optional[str] = None) -> Dict[str, Any]:
    """Prompt zombie parents to reap their dead children with SIGCHLD.

    Zombies cannot be killed directly — only their parent can reap them.  The
    safe, standard nudge is SIGCHLD to the parent, which asks it to re-run its
    wait() loop.  Only parents owned by the dashboard user (or any parent when
    running as root) are nudged.
    """
    zombies = await asyncio.to_thread(_list_zombies_sync)
    if not zombies:
        return {"success": True, "message": "No zombie processes found to reap."}

    nudged, denied = 0, 0
    for pid, name, ppid in zombies:
        try:
            parent = psutil.Process(ppid)
            if os.geteuid() != 0 and parent.uids().effective != os.geteuid():
                denied += 1
                continue
            os.kill(ppid, signal.SIGCHLD)
            nudged += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError, PermissionError):
            denied += 1

    await asyncio.sleep(0.6)

    remaining = len(await asyncio.to_thread(_list_zombies_sync))

    msg = f"SIGCHLD sent to {nudged} parent(s); {len(zombies)} zombie(s) found, {remaining} still present."
    if denied:
        msg += f" {denied} parent(s) not owned by the dashboard user were skipped."
    return {"success": remaining == 0, "message": msg}


async def _fix_flush_dns(target: Optional[str] = None) -> Dict[str, Any]:
    candidates = []
    for bin_name, args in (("resolvectl", ["flush-caches"]),
                           ("systemd-resolve", ["--flush-caches"]),
                           ("nscd", ["-i", "hosts"])):
        path = shutil.which(bin_name)
        if path:
            candidates.append((bin_name, [path, *args]))
    if not candidates:
        return {"success": False, "message": "No supported DNS cache manager found (resolvectl / systemd-resolve / nscd)."}

    last_error = ""
    for name, cmd in candidates:
        full = cmd
        if os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                last_error = "sudo is required but not installed."
                continue
            full = [sudo, "-n", *cmd]
        try:
            returncode, stdout, stderr = await _run_cmd(full, timeout=15.0)
        except (asyncio.TimeoutError, OSError) as e:
            last_error = f"{name}: {e}"
            continue
        if returncode == 0:
            return {"success": True, "message": f"DNS resolver cache flushed via {name}."}
        last_error = f"{name}: {stderr.decode().strip() or 'failed'}"
    return {"success": False, "message": f"DNS cache flush failed — {last_error}"}


async def _fix_clean_tmp(target: Optional[str] = None) -> Dict[str, Any]:
    find = shutil.which("find")
    if not find:
        return {"success": False, "message": "find(1) is not available on this host."}
    cmd = [find, "/tmp", "/var/tmp", "-maxdepth", "2", "-type", "f", "-mtime", "+7", "-print", "-delete"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        returncode, stdout, stderr = await _run_cmd(cmd, timeout=90.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Temp-file cleanup timed out after 90s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    deleted = len([l for l in stdout.decode(errors="replace").splitlines() if l.strip()])
    if returncode == 0:
        return {"success": True, "message": f"Deleted {deleted} stale temp file(s) from /tmp and /var/tmp."}
    return {"success": False, "message": stderr.decode(errors="replace").strip() or f"Cleanup exited with code {returncode}"}


async def _sudo_cmd(base_cmd: List[str], timeout: float = 30.0) -> tuple:
    """Run a command, prefixing non-interactive sudo when not root."""
    cmd = list(base_cmd)
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return 127, b"", b"sudo is required but not installed."
        cmd = [sudo, "-n", *cmd]
    return await _run_cmd(cmd, timeout=timeout)


async def _fix_enable_ntp(target: Optional[str] = None) -> Dict[str, Any]:
    timedatectl = shutil.which("timedatectl")
    if not timedatectl:
        return {"success": False, "message": "timedatectl is not available on this host."}
    try:
        returncode, stdout, stderr = await _sudo_cmd([timedatectl, "set-ntp", "true"], timeout=20.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Enabling NTP timed out after 20s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode != 0:
        return {"success": False, "message": stderr.decode(errors="replace").strip() or "Could not enable NTP synchronization."}

    # Give timesyncd a moment and report sync state for operator confidence.
    await asyncio.sleep(1.0)
    try:
        rc, out, _ = await _run_cmd([timedatectl, "show", "-p", "NTPSynchronized", "-p", "NTP"], timeout=8.0)
        if rc == 0:
            state = out.decode(errors="replace").strip().replace("\n", ", ")
            return {"success": True, "message": f"NTP synchronization enabled ({state})."}
    except Exception:
        pass
    return {"success": True, "message": "NTP synchronization enabled."}


PACKAGE_CLEANERS = [
    ("apt-get", ["apt-get", "clean"], "APT package cache cleaned."),
    ("dnf", ["dnf", "clean", "all"], "DNF package cache cleaned."),
    ("yum", ["yum", "clean", "all"], "YUM package cache cleaned."),
    ("zypper", ["zypper", "--non-interactive", "clean"], "Zypper package cache cleaned."),
]
PACKAGE_AUTOREMOVE = [
    ("apt-get", ["apt-get", "autoremove", "--purge", "-y"], "Unused APT dependencies removed."),
    ("dnf", ["dnf", "autoremove", "-y"], "Unused DNF dependencies removed."),
    ("yum", ["yum", "autoremove", "-y"], "Unused YUM dependencies removed."),
]


async def _run_first_available_package_cmd(candidates: List[tuple], timeout: float = 180.0) -> Dict[str, Any]:
    for binary, args, success_message in candidates:
        path = shutil.which(binary)
        if not path:
            continue
        try:
            returncode, stdout, stderr = await _sudo_cmd([path, *args[1:]], timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "message": f"{binary} operation timed out after {int(timeout)}s"}
        except Exception as e:
            return {"success": False, "message": str(e)}
        if returncode == 0:
            detail = stdout.decode(errors="replace").strip()
            return {"success": True, "message": success_message + (f" {detail}" if detail else "")}
        return {"success": False, "message": stderr.decode(errors="replace").strip() or f"{binary} exited with code {returncode}"}
    return {"success": False, "message": "No supported package manager found (apt-get, dnf, yum, or zypper required)."}


async def _fix_clean_package_cache(target: Optional[str] = None) -> Dict[str, Any]:
    return await _run_first_available_package_cmd(PACKAGE_CLEANERS, timeout=120.0)


async def _fix_autoremove_packages(target: Optional[str] = None) -> Dict[str, Any]:
    return await _run_first_available_package_cmd(PACKAGE_AUTOREMOVE, timeout=300.0)


async def _fix_trim_journal(target: Optional[str] = None) -> Dict[str, Any]:
    size = "200M"
    if target and re.fullmatch(r"\d+[KMGT]?", target, re.IGNORECASE):
        size = target.upper()
    cmd = [JOURNALCTL_BIN, f"--vacuum-size={size}"]
    try:
        returncode, stdout, stderr = await _sudo_cmd(cmd, timeout=90.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Journal compaction timed out after 90s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": f"Systemd journal compacted to {size}.", "detail": stdout.decode(errors="replace").strip()}
    return {"success": False, "message": stderr.decode(errors="replace").strip() or f"Journal vacuum exited with code {returncode}"}


async def _fix_rotate_logs(target: Optional[str] = None) -> Dict[str, Any]:
    logrotate = shutil.which("logrotate")
    if not logrotate:
        return {"success": False, "message": "logrotate is not installed on this host."}
    conf = target if target and target.startswith("/") and ".." not in target and os.path.isfile(target) else "/etc/logrotate.conf"
    try:
        returncode, stdout, stderr = await _sudo_cmd([logrotate, "--force", conf], timeout=90.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": "Log rotation timed out after 90s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": f"Log rotation forced using {conf}."}
    return {"success": False, "message": stderr.decode(errors="replace").strip() or f"logrotate exited with code {returncode}"}


async def _active_non_loopback_interfaces() -> List[str]:
    """Return active, non-loopback interface names for network remediation."""
    interfaces = []
    try:
        for name, stats in psutil.net_if_stats().items():
            if name != "lo" and getattr(stats, "isup", False):
                interfaces.append(name)
    except Exception:
        pass
    return interfaces


async def _network_manager_unit() -> Optional[str]:
    """Identify the network manager service available on this host."""
    for unit in ("NetworkManager.service", "systemd-networkd.service"):
        path = SYSTEMCTL_BIN
        try:
            rc, _, _ = await _sudo_cmd([path, "list-unit-files", unit, "--no-pager"], timeout=8.0)
            # list-unit-files returns 0 when the unit file exists on systemd.
            if rc == 0:
                return unit
        except Exception:
            continue
    # Non-systemd hosts (containers) usually lack either service; leave unset.
    return None


async def _fix_reset_network_manager(target: Optional[str] = None) -> Dict[str, Any]:
    unit = target if target and SERVICE_NAME_PATTERN.fullmatch(target) else await _network_manager_unit()
    if not unit:
        return {"success": False, "message": "NetworkManager/systemd-networkd is not available on this host."}

    # Prefer the existing service-control sudo policy; fall back to a direct
    # restart because the default MonitorX policy only permits service probes.
    result, error = await run_service_action("restart", unit)
    if not error:
        return {"success": True, "message": f"Network service {unit} restarted. Brief connectivity interruption is normal."}

    try:
        returncode, _, stderr = await _sudo_cmd(
            [SYSTEMCTL_BIN, "--no-ask-password", "restart", unit], timeout=30.0
        )
    except asyncio.TimeoutError:
        return {"success": False, "message": f"Restarting {unit} timed out after 30s."}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": f"Network service {unit} restarted. Brief connectivity interruption is normal."}
    detail = stderr.decode(errors="replace").strip() or error
    return {"success": False, "message": f"Could not restart {unit}: {detail}"}


async def _fix_renew_dhcp_lease(target: Optional[str] = None) -> Dict[str, Any]:
    interfaces = [target] if target else await _active_non_loopback_interfaces()
    if not interfaces:
        return {"success": False, "message": "No active non-loopback interface found for DHCP renewal."}

    nmcli = shutil.which("nmcli")
    renewed, failures = [], []
    if nmcli:
        for iface in interfaces:
            try:
                rc, out, err = await _sudo_cmd([nmcli, "device", "reapply", iface], timeout=20.0)
                if rc == 0:
                    renewed.append(iface)
                    continue
                # Fall back to a fresh DHCP lease when reapply is unsupported.
                rc, out, err = await _sudo_cmd(
                    [nmcli, "connection", "up", "ifname", iface], timeout=30.0
                )
                if rc == 0:
                    renewed.append(iface)
                else:
                    failures.append(f"{iface}: {err.decode(errors='replace').strip() or out.decode(errors='replace').strip()}")
            except Exception as e:
                failures.append(f"{iface}: {e}")
    else:
        for iface in interfaces:
            for lease_tool, args in (("dhclient", ["-r", iface]), ("dhclient", [iface])):
                path = shutil.which(lease_tool)
                if not path:
                    continue
                try:
                    await _sudo_cmd([path, *args[1:]], timeout=25.0)
                except Exception:
                    pass
            renewed.append(iface)

    if renewed and not failures:
        return {"success": True, "message": f"Renewed DHCP lease(s) on: {', '.join(renewed)}."}
    if renewed:
        return {"success": True, "message": f"Renewed {', '.join(renewed)}; failed: {'; '.join(failures)}"}
    return {"success": False, "message": "Could not renew DHCP leases. " + "; ".join(failures[:3])}


async def _fix_system_cleanup(target: Optional[str] = None) -> Dict[str, Any]:
    """Conservative whole-system cleanup sequence.

    Intentionally excludes package autoremove and service restarts because those
    can alter workload capacity or remove kernels; they remain separate,
    deliberately selected fixes.
    """
    steps = [
        ("vacuum_journal", _fix_vacuum_journal, None),
        ("clean_tmp", _fix_clean_tmp, None),
        ("clean_package_cache", _fix_clean_package_cache, None),
        ("rotate_logs", _fix_rotate_logs, None),
        ("clear_pagecache", _fix_clear_pagecache, None),
    ]
    results = []
    success_count = 0
    for name, executor, step_target in steps:
        result = await executor(step_target)
        ok = bool(result.get("success"))
        success_count += int(ok)
        results.append({
            "action": name,
            "success": ok,
            "message": result.get("message") or ("completed" if ok else "failed"),
        })

    return {
        "success": success_count > 0,
        "message": f"Full system cleanup completed: {success_count}/{len(steps)} steps succeeded.",
        "results": results,
    }


FIX_EXECUTORS = {
    "clear_pagecache": _fix_clear_pagecache,
    "vacuum_journal": _fix_vacuum_journal,
    "restart_failed_services": _fix_restart_failed_services,
    "restart_service": lambda t: _fix_service_action("restart", t),
    "start_service": lambda t: _fix_service_action("start", t),
    "enable_service": lambda t: _fix_service_action("enable", t),
    "clear_kernel_logs": _fix_clear_kernel_logs,
    "kill_process": _fix_kill_process,
    "kill_top_cpu": _fix_kill_top_cpu,
    "reap_zombies": _fix_reap_zombies,
    "flush_dns": _fix_flush_dns,
    "clean_tmp": _fix_clean_tmp,
    "enable_ntp": _fix_enable_ntp,
    "clean_package_cache": _fix_clean_package_cache,
    "autoremove_packages": _fix_autoremove_packages,
    "trim_journal": _fix_trim_journal,
    "rotate_logs": _fix_rotate_logs,
    "reset_network_manager": _fix_reset_network_manager,
    "renew_dhcp_lease": _fix_renew_dhcp_lease,
    "system_cleanup": _fix_system_cleanup,
}


async def run_remediation(action: str, target: Optional[str] = None) -> Dict[str, Any]:
    """Execute one remediation action with audit logging.

    Every execution — success or failure — is recorded through the operations
    audit store so the hub can render a remediation history.
    """
    canonical = FIX_ACTION_ALIASES.get(action, action)
    executor = FIX_EXECUTORS.get(canonical)
    if not executor:
        raise HTTPException(status_code=400, detail=f"Unsupported remediation action: {action}")

    started = time.monotonic()
    try:
        result = await executor(target)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Remediation %s failed", canonical)
        result = {"success": False, "message": str(e)}
    result["action"] = action
    result["target"] = target
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    result["timestamp"] = datetime.now().isoformat()
    try:
        audit_operation(
            f"remediate:{canonical}",
            target or "",
            "success" if result.get("success") else "failed",
            str(result.get("message") or "")[:400],
        )
    except Exception as exc:
        logger.warning("Remediation audit failed: %s", exc)
    return result


@app.post("/api/troubleshoot/remediate")
async def perform_remediation(req: RemediateRequest):
    """
    Executes automated safe fix and remediation actions.
    """
    return await run_remediation(req.action, req.target)





@app.get("/api/troubleshoot/fix-capabilities")
async def troubleshoot_fix_capabilities():
    """
    Report which auto-fix actions this dashboard is actually able to execute,
    so the hub can disable buttons that would fail (e.g. no sudo policy).
    """
    euid = os.geteuid()
    is_root = euid == 0
    sudo = shutil.which("sudo")
    has_sudo = False
    if not is_root and sudo:
        try:
            returncode, _, _ = await _run_cmd([sudo, "-n", "-l"], timeout=5.0)
            has_sudo = returncode == 0
        except (asyncio.TimeoutError, OSError):
            has_sudo = False

    def have(bin_name: str) -> bool:
        return shutil.which(bin_name) is not None

    bins = {
        "sysctl": have("sysctl") or os.path.exists(SYSCTL_BIN),
        "journalctl": have("journalctl") or os.path.exists(JOURNALCTL_BIN),
        "dmesg": have("dmesg") or os.path.exists(DMESG_BIN),
        "systemctl": have("systemctl") or os.path.exists(SYSTEMCTL_BIN),
        "resolvectl": have("resolvectl"),
        "systemd_resolve": have("systemd-resolve"),
        "nscd": have("nscd"),
        "find": have("find"),
        "timedatectl": have("timedatectl"),
        "logrotate": have("logrotate"),
        "apt_get": have("apt-get"),
        "dnf": have("dnf"),
        "yum": have("yum"),
        "zypper": have("zypper"),
        "nmcli": have("nmcli"),
        "dhclient": have("dhclient"),
        "sudo": bool(sudo),
    }
    elevated = is_root or has_sudo
    service_policy = await _service_sudo_allowed()
    package_manager_available = bins["apt_get"] or bins["dnf"] or bins["yum"] or bins["zypper"]
    dhcp_available = bins["nmcli"] or bins["dhclient"]

    available = {
        "clear_pagecache": elevated and bins["sysctl"],
        "vacuum_journal": elevated and bins["journalctl"],
        "restart_failed_services": service_policy and bins["systemctl"],
        "restart_service": service_policy and bins["systemctl"],
        "start_service": service_policy and bins["systemctl"],
        "enable_service": service_policy and bins["systemctl"],
        "clear_kernel_logs": elevated and bins["dmesg"],
        "kill_process": True,
        "kill_top_cpu": True,
        "reap_zombies": True,
        "flush_dns": (bins["resolvectl"] or bins["systemd_resolve"] or bins["nscd"]),
        "clean_tmp": elevated and bins["find"],
        "enable_ntp": elevated and bins["timedatectl"],
        "clean_package_cache": elevated and package_manager_available,
        "autoremove_packages": elevated and package_manager_available,
        "trim_journal": elevated and bins["journalctl"],
        "rotate_logs": elevated and bins["logrotate"],
        "reset_network_manager": service_policy and bins["systemctl"],
        "renew_dhcp_lease": elevated and dhcp_available,
        "system_cleanup": elevated and (bins["journalctl"] or bins["find"] or package_manager_available or bins["logrotate"] or bins["sysctl"]),
    }
    return {
        "is_root": is_root,
        "euid": euid,
        "sudo": has_sudo,
        "bins": bins,
        "available_actions": available,
        "fix_actions": {
            k: {
                "label": v["label"],
                "level": v["level"],
                "category": v.get("category", "System"),
                "sudo": v.get("sudo", False),
                "description": v.get("description", ""),
            }
            for k, v in FIX_ACTION_META.items()
        },
    }


@app.delete("/api/troubleshoot/fix-history")
async def clear_troubleshoot_fix_history():
    """Delete remediation entries without touching alert history."""
    with _ops_conn() as conn:
        conn.execute("DELETE FROM operations_audit WHERE action LIKE 'remediate:%'")
    return {"success": True}


@app.get("/api/troubleshoot/fix-history")
async def troubleshoot_fix_history(limit: int = Query(30, ge=1, le=200)):
    """
    Recent remediation executions, newest first, from the operations audit store.
    """
    try:
        with _ops_conn() as conn:
            rows = conn.execute(
                "SELECT timestamp, action, target, outcome, detail FROM operations_audit "
                "WHERE action LIKE 'remediate:%' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"entries": [dict(r) for r in rows]}
    except Exception:
        return {"entries": []}


SAFE_DIAGNOSTIC_COMMANDS = {
    "df -h": ("df", "-h"),
    "free -h": ("free", "-h"),
    "ss -tulpn": ("ss", "-tulpn"),
    "systemctl --failed": (SYSTEMCTL_BIN, "--no-ask-password", "--failed", "--no-pager"),
    "uname -a": ("uname", "-a"),
}


@app.post("/api/commands/run")
async def run_command(request: Request):
    """Run one of the dashboard's explicitly approved, read-only diagnostics.

    Authentication is optional (it is only enforced when MONITORX_AUTH_TOKEN is
    set), so this endpoint must assume it is reachable by anyone who can reach
    the port. Accepting an arbitrary shell command would therefore be remote
    code execution; only the preset allowlist below is ever executed.
    """
    try:
        body = await request.json()
        command = str(body.get("command", "")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if command == "dmesg -T | tail -n 25":
        args = ("dmesg", "-T")
        tail_lines = 25
    else:
        args = SAFE_DIAGNOSTIC_COMMANDS.get(command)
        tail_lines = None
    if not args:
        raise HTTPException(
            status_code=403,
            detail="Only the dashboard's approved diagnostic presets can be run. Arbitrary shell commands are disabled."
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode(errors="replace")[:MAX_DIAGNOSTIC_OUTPUT]
        if tail_lines:
            output = "\n".join(output.splitlines()[-tail_lines:])
        return {"output": output, "error": stderr.decode(errors="replace"), "returncode": proc.returncode}
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except ProcessLookupError:
            pass
        raise HTTPException(status_code=504, detail="Diagnostic command timed out after 15 seconds.")
    except FileNotFoundError:
        raise HTTPException(status_code=501, detail=f"Diagnostic command is unavailable: {args[0]}")
    except Exception:
        logger.exception("Diagnostic command failed")
        raise HTTPException(status_code=500, detail="Diagnostic command could not be executed.")




if __name__ == "__main__":
    import uvicorn
    # Secure default: localhost only. Set MONITORX_HOST=0.0.0.0 explicitly
    # when a reverse proxy/authenticated preview needs a network bind.
    if MONITORX_HOST != "127.0.0.1" and not MONITORX_AUTH_TOKEN:
        logger.warning("⚠️ Binding to %s without MONITORX_AUTH_TOKEN is insecure!", MONITORX_HOST)
    uvicorn.run(app, host=MONITORX_HOST, port=int(os.environ.get("MONITORX_PORT", "8080")))
