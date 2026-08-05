"""
Monitoring Dashboard Backend - FastAPI Application
Provides real-time system monitoring via WebSocket and REST API
"""
import asyncio
import concurrent.futures
import json
import logging
import os
import platform
import re
import signal
import socket
import subprocess
import sqlite3
import shutil
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
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

# Global state tracking for rate calculations
# Network/disk rates are derived from a previous sample; serialize snapshots.
stats_lock = asyncio.Lock()

last_net_io = None
last_net_time = None
last_disk_io = None
last_disk_time = None

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


async def _libvirt_conn_alive_async():
    """Check if the read-only libvirt connection is alive (async safe)."""
    return await _conn_alive_async(libvirt_conn)


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

app = FastAPI(
    title="System Monitoring Dashboard",
    description="Real-time system monitoring dashboard with WebSocket support and Troubleshoot Suite",
    version="2.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
templates = Jinja2Templates(directory=str(FRONTEND_DIR))


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
    containers: Optional[List[Dict[str, Any]]] = None
    pods: Optional[List[Dict[str, Any]]] = None
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
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


async def get_cpu_stats() -> Dict[str, Any]:
    """Get CPU statistics without blocking interval"""
    cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    load_avg = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
    
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
        "times": dict(psutil.cpu_times()._asdict()) if psutil.cpu_times() else {}
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
    """Get disk statistics and transfer rate"""
    global last_disk_io, last_disk_time
    
    partitions = psutil.disk_partitions()
    disks = []
    
    for partition in partitions:
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            inode_percent = 0.0
            try:
                st = os.statvfs(partition.mountpoint)
                if st.f_files > 0:
                    inode_percent = round(((st.f_files - st.f_ffree) / st.f_files) * 100, 1)
            except Exception:
                pass

            disks.append({
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "fstype": partition.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": (usage.used / usage.total * 100) if usage.total > 0 else 0,
                "inode_percent": inode_percent
            })
        except (PermissionError, FileNotFoundError):
            continue
    
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
        "partitions": disks,
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
    
    connections_count = 0
    try:
        connections_count = len(psutil.net_connections(kind='inet'))
    except Exception:
        pass

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


async def get_process_stats(limit: int = 30) -> List[Dict[str, Any]]:
    """Get processes sorted by resource usage"""
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'username', 'create_time', 'num_threads']):
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
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
    return processes[:limit]


async def _scan_process_states() -> Dict[str, int]:
    """Count processes by state across ALL processes (not a top-N subset).

    ``get_process_stats(limit=N)`` sorts by CPU and truncates, so zombies and
    D-state processes — which consume ~0% CPU — routinely fall off the end of
    the list on busy hosts. Diagnostic scans must therefore never reuse the
    top-N view for state counting.
    """
    counts: Dict[str, int] = {}
    for proc in psutil.process_iter(['status']):
        try:
            status = (proc.info['status'] or "unknown").lower()
            counts[status] = counts.get(status, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return counts


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
# DOCKER CONTAINER & KUBERNETES POD MONITORING
# =============================================================================

async def get_docker_containers() -> Optional[List[Dict[str, Any]]]:
    """List all Docker containers on the host using the docker CLI."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--no-trunc",
            "--format", "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        containers = []
        for line in stdout.decode(errors="replace").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                containers.append({
                    "id": raw.get("ID", "")[:12],
                    "name": raw.get("Name", ""),
                    "image": raw.get("Image", ""),
                    "status": raw.get("Status", ""),
                    "state": raw.get("State", ""),
                    "ports": raw.get("Ports", ""),
                    "created": raw.get("CreatedAt", ""),
                    "size": raw.get("Size", ""),
                    "running": raw.get("State", "").lower() == "running",
                })
            except json.JSONDecodeError:
                continue
        return containers if containers else []
    except FileNotFoundError:
        return None
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.warning("Error listing Docker containers: %s", e)
        return None


async def get_docker_container_logs(container_id: str, lines: int = 100) -> Optional[str]:
    """Fetch recent logs from a Docker container."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "--tail", str(lines), container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        output = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        return output + ("\n" + err if err else "")
    except Exception:
        return None


async def get_docker_container_stats() -> Optional[List[Dict[str, Any]]]:
    """Get live resource usage for running Docker containers."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream",
            "--format", "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None
        stats = []
        for line in stdout.decode(errors="replace").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                cpu_str = raw.get("CPUPerc", "0%").replace("%", "").strip()
                stats.append({
                    "id": raw.get("ID", "")[:12],
                    "name": raw.get("Name", ""),
                    "cpu_percent": float(cpu_str) if cpu_str else 0.0,
                    "mem_usage": raw.get("MemUsage", ""),
                    "net_io": raw.get("NetIO", ""),
                    "block_io": raw.get("BlockIO", ""),
                    "pids": raw.get("PIDs", "0"),
                })
            except (json.JSONDecodeError, ValueError):
                continue
        return stats if stats else []
    except FileNotFoundError:
        return None
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.warning("Error getting Docker container stats: %s", e)
        return None


async def get_kubernetes_pods() -> Optional[List[Dict[str, Any]]]:
    """List Kubernetes pods if kubectl is available and configured."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "get", "pods", "-A", "-o", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode(errors="replace"))
        pods = []
        for item in data.get("items", []):
            metadata = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            container_statuses = status.get("containerStatuses", [])
            total_restarts = sum(c.get("restartCount", 0) for c in container_statuses)
            pod_phase = status.get("phase", "Unknown")
            containers = [c.get("name", "") for c in spec.get("containers", [])]
            restart_reasons = []
            for cs in container_statuses:
                state = cs.get("state", {})
                if "waiting" in state:
                    reason = state["waiting"].get("reason", "")
                    if reason:
                        restart_reasons.append(f"{cs.get('name','')}: {reason}")
            pods.append({
                "name": metadata.get("name", ""),
                "namespace": metadata.get("namespace", "default"),
                "status": pod_phase,
                "restarts": total_restarts,
                "node": spec.get("nodeName", ""),
                "pod_ip": status.get("podIP", ""),
                "containers": containers,
                "container_count": len(containers),
                "age": metadata.get("creationTimestamp", ""),
                "waiting_reasons": restart_reasons,
            })
        return pods if pods else []
    except FileNotFoundError:
        return None
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        logger.warning("Error listing Kubernetes pods: %s", e)
        return None


def _virsh_present() -> bool:
    """True when a virsh binary is actually available to execute."""
    return bool(shutil.which(VIRSH_BIN) or Path(VIRSH_BIN).exists())


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
    """
    if not _virsh_present():
        return False
    if os.geteuid() == 0:
        return True

    sudo = shutil.which("sudo")
    if not sudo:
        return False
    probe = _virsh_command("start", "monitorx-capability-probe")
    if not probe:
        return False
    # probe[0] is the sudo binary and probe[1] is "-n"; validate the rest.
    try:
        proc = await asyncio.create_subprocess_exec(
            sudo, "-n", "-l", "--", *probe[2:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError):
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
      * ``--no-pkttyagent`` MUST stay in the argv: the sudoers policy shipped
        by systemd/install-service.sh whitelists exactly
        ``virsh --quiet --no-pkttyagent --connect <URI> <verb> -- <domain>``
        and sudo matches the full command line. Omitting it makes sudo reject
        every control command with "not allowed to execute" — the same class
        of mismatch that silently broke VM controls in earlier releases.
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

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return f"Could not execute {command[0]}: file not found."
    except PermissionError:
        return f"Could not execute {command[0]}: permission denied."

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=VM_ACTION_TIMEOUT
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except (ProcessLookupError, Exception):
            pass
        return (f"virsh {action} timed out after {int(VM_ACTION_TIMEOUT)}s. "
                f"The guest may be unresponsive; try Poweroff to force-stop it.")

    err = (stderr.decode(errors="replace").strip()
           or stdout.decode(errors="replace").strip())
    if proc.returncode != 0:
        return _humanize_vm_error(err, action, proc.returncode)
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

    For both running and stopped VMs the endpoint first increases the
    maximum (if the requested value exceeds it) and then sets the current
    allocation so the change takes effect immediately on next boot or live.
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

    # Determine whether the VM is running so we pick the right affect flag.
    is_running = False
    try:
        info = await _run_libvirt(domain.info, timeout=5.0)
        is_running = info[0] == libvirt.VIR_DOMAIN_RUNNING
    except Exception:
        pass

    # For a running VM we modify live state; for a stopped one we modify the
    # persistent config so the change takes effect on next start.
    affect_flag = (libvirt.VIR_DOMAIN_AFFECT_LIVE if is_running
                   else libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    messages = []
    errors: List[str] = []

    # ------------------------------------------------------------------
    # vCPUs
    # ------------------------------------------------------------------
    if payload.vcpus is not None:
        # Step 1: increase max vcpus if needed
        try:
            conn, _ = await _ensure_libvirt_rw_conn()
            tgt_domain = domain
            if conn:
                d, _ = await _resolve_domain(vm_id, conn=conn)
                if d:
                    tgt_domain = d
            # Read current max
            info = await _run_libvirt(tgt_domain.info, timeout=5.0)
            current_max = info[3]  # nrVirtCpu (max)
            if payload.vcpus > current_max:
                # Increase max via persistent config first
                await _run_libvirt(
                    lambda: tgt_domain.setVcpusFlags(
                        payload.vcpus,
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_AFFECT_MAXIMUM,
                    ),
                    timeout=10.0,
                )
                # If running, also increase live max so the next step succeeds
                if is_running:
                    try:
                        await _run_libvirt(
                            lambda: tgt_domain.setVcpusFlags(
                                payload.vcpus,
                                libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_MAXIMUM,
                            ),
                            timeout=10.0,
                        )
                    except Exception:
                        pass  # some hypervisors don't support live max increase
        except Exception as exc:
            # If native fails, try virsh setvcpus --maximum
            try:
                max_args = ["--maximum", "--config"]
                cmd = _build_virsh_modify_command(
                    "setvcpus", vm_id, str(payload.vcpus), *max_args,
                )
                if cmd:
                    err = await _run_virsh_modify(cmd)
                    if err:
                        errors.append(f"vCPU max: {err}")
                else:
                    errors.append("vCPU max: virsh fallback unavailable")
            except Exception:
                errors.append(f"vCPU max increase failed: {exc}")

        # Step 2: set current vcpus
        # If the VM is running, try to set live first. If that fails (because
        # the hypervisor doesn't support live max increase), fall back to
        # persistent config and tell the user it applies on next boot.
        live_failed = False
        try:
            conn, _ = await _ensure_libvirt_rw_conn()
            tgt_domain = domain
            if conn:
                d, _ = await _resolve_domain(vm_id, conn=conn)
                if d:
                    tgt_domain = d
            if is_running:
                try:
                    await _run_libvirt(
                        lambda: tgt_domain.setVcpusFlags(payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_LIVE),
                        timeout=10.0,
                    )
                    messages.append(f"vCPUs set to {payload.vcpus}")
                except Exception:
                    live_failed = True
                    raise  # fall through to config fallback below
            if not is_running or live_failed:
                await _run_libvirt(
                    lambda: tgt_domain.setVcpusFlags(payload.vcpus, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                    timeout=10.0,
                )
                if live_failed:
                    messages.append(
                        f"vCPUs set to {payload.vcpus} (persistent — will take effect after reboot; "
                        f"live max is capped at the current hardware limit)")
                else:
                    messages.append(f"vCPUs set to {payload.vcpus}")
        except libvirt.libvirtError as exc:
            msg = str(exc)
            low = msg.lower()
            if ("read only" in low or "denied" in low
                    or "greater than max" in low or "max allowable" in low):
                extra_args = ["--config"] if (not is_running or live_failed) else []
                cmd = _build_virsh_modify_command("setvcpus", vm_id, str(payload.vcpus), *extra_args)
                if cmd:
                    err = await _run_virsh_modify(cmd)
                    if err:
                        errors.append(f"vCPUs: {err}")
                    else:
                        messages.append(f"vCPUs set to {payload.vcpus} (via virsh)")
                else:
                    errors.append("vCPUs: permission denied and virsh unavailable")
            else:
                errors.append(f"vCPUs: {msg}")
        except Exception as exc:
            errors.append(f"vCPUs: {exc}")

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    if payload.memory_mb is not None:
        mem_kib = payload.memory_mb * 1024

        # Step 1: increase max memory if needed
        try:
            conn, _ = await _ensure_libvirt_rw_conn()
            tgt_domain = domain
            if conn:
                d, _ = await _resolve_domain(vm_id, conn=conn)
                if d:
                    tgt_domain = d
            info = await _run_libvirt(tgt_domain.info, timeout=5.0)
            current_max_mem = info[2]  # maxMem in KiB
            if mem_kib > current_max_mem:
                await _run_libvirt(
                    lambda: tgt_domain.setMemoryFlags(
                        mem_kib,
                        libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_AFFECT_MAXIMUM,
                    ),
                    timeout=10.0,
                )
                if is_running:
                    try:
                        await _run_libvirt(
                            lambda: tgt_domain.setMemoryFlags(
                                mem_kib,
                                libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_MAXIMUM,
                            ),
                            timeout=10.0,
                        )
                    except Exception:
                        pass
        except Exception:
            # Virsh fallback: setmaxmem --config for stopped, plain for running
            try:
                max_args = ["--config"]
                cmd = _build_virsh_modify_command(
                    "setmaxmem", vm_id, str(mem_kib), *max_args,
                )
                if cmd:
                    err = await _run_virsh_modify(cmd)
                    if err:
                        errors.append(f"Memory max: {err}")
                else:
                    errors.append("Memory max: virsh fallback unavailable")
            except Exception as exc:
                errors.append(f"Memory max increase failed: {exc}")

        # Step 2: set current memory
        mem_live_failed = False
        try:
            conn, _ = await _ensure_libvirt_rw_conn()
            tgt_domain = domain
            if conn:
                d, _ = await _resolve_domain(vm_id, conn=conn)
                if d:
                    tgt_domain = d
            if is_running:
                try:
                    await _run_libvirt(
                        lambda: tgt_domain.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_LIVE),
                        timeout=10.0,
                    )
                    messages.append(f"Memory set to {payload.memory_mb} MiB")
                except Exception:
                    mem_live_failed = True
                    raise
            if not is_running or mem_live_failed:
                await _run_libvirt(
                    lambda: tgt_domain.setMemoryFlags(mem_kib, libvirt.VIR_DOMAIN_AFFECT_CONFIG),
                    timeout=10.0,
                )
                if mem_live_failed:
                    messages.append(
                        f"Memory set to {payload.memory_mb} MiB (persistent — will take effect after reboot)")
                else:
                    messages.append(f"Memory set to {payload.memory_mb} MiB")
        except libvirt.libvirtError as exc:
            msg = str(exc)
            low = msg.lower()
            if ("read only" in low or "denied" in low
                    or "greater than max" in low or "max allowable" in low):
                extra_args = ["--config"] if (not is_running or mem_live_failed) else []
                cmd = _build_virsh_modify_command("setmem", vm_id, str(mem_kib), *extra_args)
                if cmd:
                    err = await _run_virsh_modify(cmd)
                    if err:
                        errors.append(f"Memory: {err}")
                    else:
                        messages.append(f"Memory set to {payload.memory_mb} MiB (via virsh)")
                else:
                    errors.append("Memory: permission denied and virsh unavailable")
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
    state_names = {
        libvirt.VIR_DOMAIN_NOSTATE: "no_state", libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "blocked", libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown", libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
        libvirt.VIR_DOMAIN_CRASHED: "crashed", libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
    }
    try:
        domain, error = await _resolve_domain(vm_id)
        if error or domain is None:
            return None
        info = await _run_libvirt(domain.info, timeout=5.0)
        return state_names.get(info[0], "unknown")
    except Exception:
        return None


@app.get("/api/vms/log")
async def vm_action_log(limit: int = Query(20, ge=1, le=_VM_ACTION_LOG_LIMIT)):
    """Return the most recent VM control actions, newest first."""
    async with _vm_action_log_lock:
        recent = list(reversed(_vm_action_log[-limit:]))
    return {"entries": recent, "total": len(_vm_action_log)}


def _build_virsh_modify_command(subcmd: str, vm_id: str, *args) -> List[str]:
    """Build a virsh command for domain modification via the fallback path.

    The argv shape must stay in sync with the sudoers policy installed by
    systemd/install-service.sh (``virsh --quiet --no-pkttyagent --connect
    <URI> <subcmd> <domain> …``); in particular ``--no-pkttyagent`` is part
    of the whitelisted command, not an optional extra.
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
    """Run a virsh modify command and return error string or None on success."""
    if not command:
        return "sudo/virsh not available"
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            return err or f"virsh command failed (exit code {proc.returncode})"
        return None
    except asyncio.TimeoutError:
        return "virsh command timed out"
    except Exception as e:
        return str(e)



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

    state_map = {
        libvirt.VIR_DOMAIN_NOSTATE: "no_state", libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "blocked", libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown", libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
        libvirt.VIR_DOMAIN_CRASHED: "crashed", libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
    }

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
                    lambda d=domain: _collect_domain_snapshot(d, state_map),
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


async def collect_all_stats() -> SystemStats:
    """Collect a consistent stats snapshot.

    Disk and network rates use previous samples, so serializing collection avoids
    concurrent REST/WebSocket requests corrupting those calculations.

    Core telemetry (CPU/memory/disk/network/processes/system) is always gathered
    first because it is fast and never optional. The peripheral subsystems
    (GPU, libvirt VMs, Docker containers, Kubernetes pods) are then collected
    concurrently, each with its own hard timeout and failure isolation. A hung
    docker daemon, unreachable kubectl, or wedged libvirtd therefore degrades
    only its own panel on the dashboard instead of stalling the broadcast for
    every client.
    """
    async with stats_lock:
        cpu = await get_cpu_stats()
        memory = await get_memory_stats()
        disk = await get_disk_stats()
        network = await get_network_stats()
        processes = await get_process_stats()
        system = await get_system_info()

        async def _optional(coro):
            """Run one peripheral collector, bounded and failure-isolated."""
            try:
                return await asyncio.wait_for(coro, timeout=20)
            except asyncio.TimeoutError:
                logger.warning("Peripheral stats collector timed out")
                return None
            except Exception as exc:
                logger.warning("Peripheral stats collector failed: %s", exc)
                return None

        gpu, vms, containers, pods, thermal = await asyncio.gather(
            _optional(get_gpu_stats()),
            _optional(get_vm_stats()),
            _optional(get_docker_containers()),
            _optional(get_kubernetes_pods()),
            _optional(get_thermal_stats()),
        )
        # SQLite persistence (P2 deferred)
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("INSERT OR IGNORE INTO metrics (ts, cpu, mem, disk, net) VALUES (?, ?, ?, ?, ?)",
                         (time.time(), cpu.get("percent", 0) if isinstance(cpu, dict) else 0, memory.get("percent", 0) if isinstance(memory, dict) else 0, disk.get("percent", 0) if isinstance(disk, dict) else 0, network.get("bytes_recv", 0) if isinstance(network, dict) else 0))
            conn.execute("DELETE FROM metrics WHERE ts < ?", (time.time() - 86400*7,))
            conn.commit(); conn.close()
        except Exception:
            pass
        return SystemStats(
            timestamp=datetime.now().isoformat(), cpu=cpu, memory=memory, disk=disk,
            network=network, gpu=gpu, processes=processes, system=system, vms=vms,
            containers=containers, pods=pods, thermal=thermal
        )


async def broadcast_stats():
    """Background task to broadcast stats to all connected clients"""
    while True:
        try:
            stats = await collect_all_stats()
            persist_snapshot_and_evaluate_alerts(stats)
            await manager.broadcast(stats.model_dump())
        except Exception as e:
            logger.error(f"Error broadcasting stats: {e}")
        await asyncio.sleep(2)


# =============================================================================
# Operations center: local history, alert rules, incident timeline and webhooks
# =============================================================================
OPERATIONS_DB = Path(os.environ.get("MONITORX_OPERATIONS_DB", str(BASE_DIR / "monitorx-operations.db")))
DEFAULT_ALERT_RULES = [
    {"id": "cpu-high", "name": "CPU usage high", "metric": "cpu", "operator": ">=", "threshold": 90, "cooldown_minutes": 15, "enabled": True},
    {"id": "memory-high", "name": "Memory pressure", "metric": "memory", "operator": ">=", "threshold": 90, "cooldown_minutes": 15, "enabled": True},
    {"id": "disk-high", "name": "Disk capacity low", "metric": "disk", "operator": ">=", "threshold": 90, "cooldown_minutes": 30, "enabled": True},
]

def _ops_conn():
    conn = sqlite3.connect(str(OPERATIONS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    # The broadcast task and the REST API write concurrently; WAL + a busy
    # timeout keep those writers from tripping over each other.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_operations_store():
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
async def operations_overview(range: str = Query('1h', pattern='^(1h|6h|24h|7d)$')):
    hours = {'1h': 1, '6h': 6, '24h': 24, '7d': 168}[range]
    with _ops_conn() as conn:
        rows = conn.execute("SELECT * FROM metric_history WHERE timestamp >= datetime('now', ?) ORDER BY timestamp", (f'-{hours} hours',)).fetchall()
        incidents = conn.execute("SELECT * FROM incidents WHERE status='open' OR timestamp >= datetime('now','-24 hours') ORDER BY id DESC LIMIT 30").fetchall()
    return {'range': range, 'history': [dict(x) for x in rows], 'incidents': [dict(x) for x in incidents]}

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
        if update.name is not None:
            sets.append("name=?"); params.append(update.name)
        if update.metric is not None:
            sets.append("metric=?"); params.append(update.metric)
        if update.threshold is not None:
            sets.append("threshold=?"); params.append(update.threshold)
        if update.cooldown_minutes is not None:
            sets.append("cooldown_minutes=?"); params.append(update.cooldown_minutes)
        if update.enabled is not None:
            sets.append("enabled=?"); params.append(int(update.enabled))
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


@app.post('/api/operations/webhook')
async def set_webhook_config(cfg: WebhookConfigRequest):
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

# Lightweight SQLite persistence (24h) — P2 deferred
DB_PATH = Path("/tmp/monitorx_metrics.db")

def init_db():
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS metrics (ts REAL PRIMARY KEY, cpu REAL, mem REAL, disk REAL, net REAL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")
        conn.commit(); conn.close()
    except Exception:
        pass

init_db()

@app.get("/api/historical")
async def historical(hours: int = Query(24, ge=1, le=168)):
    rows = []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cutoff = time.time() - (hours * 3600)
        cur = conn.execute("SELECT ts, cpu, mem, disk, net FROM metrics WHERE ts > ? ORDER BY ts ASC", (cutoff,))
        for r in cur.fetchall():
            rows.append({"ts": r[0], "cpu": r[1], "mem": r[2], "disk": r[3], "net": r[4]})
        conn.close()
    except Exception:
        pass
    return {"count": len(rows), "data": rows}

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


@app.get("/api/auth/status")
async def auth_status():
    """Report VM/service authorization state for UI visibility (P1)."""
    status = {
        "libvirt_available": LIBVIRT_AVAILABLE,
        "libvirt_rw": False,
        "virsh_policy_available": False,
        "user_uid": os.getuid(),
    }
    if LIBVIRT_AVAILABLE:
        try:
            conn = libvirt.open("qemu:///system")
            status["libvirt_rw"] = conn.isAlive() and conn.getURI() == "qemu:///system"
            conn.close()
        except Exception:
            pass
    # Check sudo policy without running privileged commands
    try:
        import subprocess
        result = subprocess.run(["sudo", "-l", "-U", str(os.getpwuid(os.getuid()).pw_name)], capture_output=True, text=True, timeout=3)
        status["virsh_policy_available"] = "monitorx-virsh" in result.stdout or "monitorx" in result.stdout
    except Exception:
        pass
    return status


# P2 deferred: Diagnostic / Process audit endpoint
AUDIT_LOG = Path("/tmp/monitorx-audit.log")

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
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(f"{datetime.now().isoformat()} | {os.getuid()} | {action} | {detail}\n")
    except Exception:
        pass
    return {"logged": True}


@app.get("/api/stats", response_model=SystemStats)
async def get_stats():
    return await collect_all_stats()


@app.get("/api/stats/cpu")
async def get_cpu():
    return await get_cpu_stats()


@app.get("/api/stats/memory")
async def get_memory():
    return await get_memory_stats()


@app.get("/api/stats/disk")
async def get_disk():
    return await get_disk_stats()


@app.get("/api/stats/network")
async def get_network():
    return await get_network_stats()


@app.get("/api/stats/gpu")
async def get_gpu():
    gpu = await get_gpu_stats()
    if gpu is None:
        raise HTTPException(status_code=404, detail="GPU monitoring not available")
    return gpu


@app.get("/api/stats/processes")
async def get_processes(limit: int = Query(30, ge=1, le=500)):
    return await get_process_stats(limit)


@app.get("/api/stats/system")
async def get_system():
    return await get_system_info()


@app.get("/api/stats/thermal")
async def get_thermal():
    return await get_thermal_stats()


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
async def report_export(format: str = Query("json", pattern="^(json|markdown)$")):
    data = await _collect_report_data()
    if format == "markdown":
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
    """WebSocket proxy for VM console access.

    Tries VNC first (graphical), then falls back to serial console via virsh.
    The frontend connects with xterm.js or noVNC.
    """
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

    # Try VNC console first
    vnc_available = False
    try:
        xml_desc = await _run_libvirt(domain.XMLDesc, timeout=5.0)
        root = ET.fromstring(xml_desc)
        graphics = root.find("./devices/graphics[@type='vnc']")
        if graphics is not None:
            # A port of -1/0 means the display uses autoport and libvirtd has
            # not assigned a concrete port yet — nothing to proxy to. Fall
            # through to the serial console instead of erroring on port -1.
            vnc_port = int(graphics.get("port", -1) or -1)
            vnc_host = graphics.get("listen", "127.0.0.1")
            if vnc_host in ("0.0.0.0", ""):
                vnc_host = "127.0.0.1"
            vnc_available = vnc_port > 0 and vnc_port <= 65535

            if vnc_available:
                # Probe the TCP connection BEFORE telling the client anything;
                # a dead listener falls through to the serial console instead
                # of leaving the terminal stuck on a connection error.
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(vnc_host, vnc_port),
                        timeout=5.0,
                    )
                except Exception:
                    logger.warning("VNC port %s not reachable; falling back to serial console", vnc_port)
                    vnc_available = False

            if vnc_available:
                # Send VNC connection info to the client, then proxy raw VNC
                # bytes between WebSocket and TCP.
                await websocket.send_json({
                    "type": "vnc",
                    "host": vnc_host,
                    "port": vnc_port,
                })

                async def ws_to_vnc():
                    try:
                        while True:
                            data = await websocket.receive_bytes()
                            writer.write(data)
                            await writer.drain()
                    except Exception:
                        try:
                            writer.close()
                        except Exception:
                            pass

                async def vnc_to_ws():
                    try:
                        while True:
                            data = await reader.read(65536)
                            if not data:
                                break
                            await websocket.send_bytes(data)
                    except Exception:
                        pass

                try:
                    await asyncio.gather(ws_to_vnc(), vnc_to_ws())
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass
                return
    except Exception as exc:
        logger.warning("VNC console path failed: %s", exc)

    # Fallback: serial console via `virsh console`.
    #
    # The argv mirrors the sudoers policy from systemd/install-service.sh
    # (virsh --quiet --no-pkttyagent --connect <URI> console -- <domain>), so
    # the command is authorized on unprivileged installs. virsh console
    # demands a real TTY, so the subprocess runs on a pty allocated here and
    # the pty master is bridged to the WebSocket; without the pty virsh fails
    # with "unable to open a pseudo-terminal" under pipes.
    await websocket.send_json({"type": "serial"})

    cmd = [VIRSH_BIN, "--quiet", "--no-pkttyagent", "--connect", LIBVIRT_URI, "console", "--", vm_id]
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
async def kill_process(pid: int, signal: int = Query(15)):
    """Terminate a process with SIGTERM (15) or SIGKILL (9).

    A SIGTERM request gets a 5-second grace period for the process to exit
    cleanly; only then is it escalated to SIGKILL (and the response says so).
    Previously the escalation happened after 0.5s, which made SIGTERM requests
    effectively indistinguishable from SIGKILL.
    """
    if signal not in (9, 15):
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
        # Audit log (P2)
        try:
            with open("/tmp/monitorx-audit.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} | {os.getuid()} | kill | pid={pid} signal={signal}\n")
        except Exception:
            pass
        proc.send_signal(signal)
        if signal == 9:
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


# Power actions are intentionally not exposed to unauthenticated dashboard clients.
# Service-level actions are available through the constrained service-control API below.
@app.post("/api/system/reboot", status_code=403)
async def reboot_system():
    raise HTTPException(status_code=403, detail="Reboot is disabled from the unauthenticated dashboard.")


@app.post("/api/system/shutdown", status_code=403)
async def shutdown_system():
    raise HTTPException(status_code=403, detail="Shutdown is disabled from the unauthenticated dashboard.")


SYSTEMCTL_BIN = shutil.which("systemctl") or "/usr/bin/systemctl"
SYSCTL_BIN = shutil.which("sysctl") or "/usr/sbin/sysctl"
JOURNALCTL_BIN = shutil.which("journalctl") or "/usr/bin/journalctl"
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


@app.get("/api/services/capabilities")
async def service_capabilities():
    """Expose whether the running dashboard can execute service controls."""
    if os.geteuid() == 0:
        return {"can_control": True, "mode": "root", "message": "Service controls are available."}
    sudo = shutil.which("sudo")
    if not sudo:
        return {"can_control": False, "mode": "unconfigured", "message": "sudo is unavailable; run the MonitorX installer."}
    try:
        returncode, _, _ = await _run_cmd([sudo, "-n", "-l"], timeout=10.0)
    except (asyncio.TimeoutError, OSError):
        returncode = 1
    available = returncode == 0
    return {
        "can_control": available,
        "mode": "sudo" if available else "unconfigured",
        "message": "Service controls are available." if available else
                   "Controls need the MonitorX sudo policy. Run systemd/install-service.sh and restart MonitorX."
    }


@app.get("/api/services")
async def list_services():
    """List systemd services. Read-only systemctl access needs no elevated policy."""
    try:
        returncode, stdout, stderr = await _run_cmd(
            [SYSTEMCTL_BIN, "list-units", "--type=service", "--no-pager", "--no-legend", "--all"],
            timeout=15.0,
        )
        if returncode:
            raise HTTPException(status_code=503, detail=stderr.decode().strip() or "systemd is unavailable")
        services = []
        for line in stdout.decode().strip().split('\n'):
            parts = line.split()
            if len(parts) >= 4:
                services.append({"name": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                                 "description": " ".join(parts[4:]) if len(parts) > 4 else ""})
        return services
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
    
    if mem_pct > 90.0 or avail_mb < 500:
        health_score -= 20
        checks.append({
            "id": "memory_swap",
            "category": "Memory",
            "name": "RAM & Swap Exhaustion",
            "status": "critical",
            "value": f"{mem_pct}% RAM used ({avail_mb:.0f} MB free), {swap_pct}% Swap",
            "message": f"Memory critically low! Risk of OOM (Out Of Memory) process kills.",
            "remediation": "Clear clean page caches using the 'Clear RAM Cache' button. If usage remains high, identify top memory-consuming processes in the Processes tab and restart/terminate them, or allocate more swap space/physical RAM.",
            "fix": {"action": "clear_pagecache", "label": "⚡ Clear RAM Cache", "level": "warning", "sudo": True, "target": None},
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
            "remediation": "Drop system page caches using the 'Clear RAM Cache' button below, or adjust the swap space and swapiness ('sysctl vm.swappiness=10') to prioritize RAM usage.",
            "fix": {"action": "clear_pagecache", "label": "⚡ Clear RAM Cache", "level": "warning", "sudo": True, "target": None},
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

    # 3. Disk Space & Inodes
    disk = await get_disk_stats()
    disk_critical = False
    disk_warning = False
    disk_details = []
    
    for p in disk["partitions"]:
        if p["percent"] > 90.0 or p["inode_percent"] > 90.0:
            disk_critical = True
            disk_details.append(f"{p['mountpoint']} ({p['percent']:.1f}% space, {p['inode_percent']}% inodes)")
        elif p["percent"] > 80.0 or p["inode_percent"] > 80.0:
            disk_warning = True
            disk_details.append(f"{p['mountpoint']} ({p['percent']:.1f}% space)")

    if disk_critical:
        health_score -= 20
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "critical",
            "value": ", ".join(disk_details) or "High usage",
            "message": "Disk space or inodes nearly full on partition(s)!",
            "remediation": "Vacuum systemd journal logs to free space immediately, or clean stale temp files. Run 'sudo apt-get clean' or 'sudo yum clean all' to clear package manager cache. Run 'du -sh /* | sort -h' to find large space-consuming folders.",
            "fix": {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
            "fixes": [
                {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_tmp", "label": "🧹 Clean Stale Temp Files", "level": "warning", "sudo": True, "target": None},
            ],
        })
    elif disk_warning:
        health_score -= 8
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "warning",
            "value": ", ".join(disk_details),
            "message": "Disk usage high (>80%) on partition(s).",
            "remediation": "Vacuum journal logs using the button below or archive old application log files. Consider setting up automatic log rotation under '/etc/logrotate.d/' to prevent partition bloat.",
            "fix": {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
            "fixes": [
                {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
                {"action": "clean_tmp", "label": "🧹 Clean Stale Temp Files", "level": "warning", "sudo": True, "target": None},
            ],
        })
    else:
        checks.append({
            "id": "disk_inodes",
            "category": "Storage",
            "name": "Disk Space & Inodes",
            "status": "ok",
            "value": f"All {len(disk['partitions'])} partition(s) healthy",
            "message": "Sufficient storage and inode availability.",
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
        # DNS-only breakage is auto-fixable (flush the resolver cache); a
        # total connectivity failure needs the network suite instead.
        dns_fix = None
        if ping_ok and not dns_ok:
            dns_fix = {"action": "flush_dns", "label": "🌀 Flush DNS Cache", "level": "warning", "sudo": True, "target": None}
        
        if not ping_ok:
            remediation = "Ping failed. Verify your network interface state using 'ip link' and physical/virtual network cables. Check local router settings, or restart the system's network service with 'sudo systemctl restart NetworkManager' or 'sudo systemctl restart systemd-networkd'."
        else:
            remediation = "DNS failed but ping succeeded. Try flushing your local DNS resolver cache using the 'Flush DNS Cache' button below, or verify server settings in '/etc/resolv.conf'."

        checks.append({
            "id": "net_connectivity",
            "category": "Network",
            "name": "Network & DNS Connectivity",
            "status": "warning",
            "value": f"Ping: {'OK' if ping_ok else 'FAIL'}, DNS: {'OK' if dns_ok else 'FAIL'}",
            "message": "Network ping test or DNS resolution failed.",
            "remediation": remediation,
            "action": "run_net_diag" if not dns_fix else None,
            "fix": dns_fix,
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
            "remediation": "Vacuum systemd journal immediately using the 'Vacuum Journal Logs' button. To permanently restrict journal growth, set 'SystemMaxUse=500M' in '/etc/systemd/journald.conf' and restart the service via 'sudo systemctl restart systemd-journald'.",
            "fix": {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
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
            "remediation": "Consider vacuuming journal entries older than 2 days. Setting a hard limit on systemd-journal storage in '/etc/systemd/journald.conf' is highly recommended to protect server disk space.",
            "fix": {"action": "vacuum_journal", "label": "⚡ Vacuum Journal Logs", "level": "warning", "sudo": True, "target": None},
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

    # 10. Exited / crashed Docker containers (only when the CLI exists)
    docker_bin = shutil.which("docker")
    if docker_bin:
        exited_containers = []
        try:
            returncode, stdout, _ = await _run_cmd(
                [docker_bin, "ps", "-a", "--filter", "status=exited", "--filter", "status=dead",
                 "--format", "{{.Names}}\t{{.Status}}"],
                timeout=10.0,
            )
            if returncode == 0:
                for line in stdout.decode(errors="replace").splitlines():
                    parts = line.split("\t")
                    if parts and parts[0].strip():
                        exited_containers.append({"name": parts[0].strip(), "status": parts[1].strip() if len(parts) > 1 else "exited"})
        except Exception:
            pass

        if exited_containers:
            health_score -= 8
            container_fixes = []
            for c in exited_containers[:5]:
                container_fixes.append({
                    "action": "restart_docker_container",
                    "label": f"↻ Restart {c['name']}",
                    "level": "warning",
                    "sudo": False,
                    "target": c["name"],
                })
            checks.append({
                "id": "docker_containers",
                "category": "Containers",
                "name": "Exited Docker Containers",
                "status": "warning",
                "value": f"{len(exited_containers)} exited/dead container(s): " + ", ".join(c["name"] for c in exited_containers[:3]),
                "message": f"Containers are not running: {', '.join(c['name'] + ' (' + c['status'] + ')' for c in exited_containers[:5])}",
                "remediation": "Restart stopped container(s) using the buttons below, or run 'docker start <container_name>'. Check crash logs by executing 'docker logs <container_name>' to diagnose the failure.",
                "fix": container_fixes[0] if container_fixes else None,
                "fixes": container_fixes,
            })
        else:
            checks.append({
                "id": "docker_containers",
                "category": "Containers",
                "name": "Exited Docker Containers",
                "status": "ok",
                "value": "0 exited/dead containers",
                "message": "All detected containers are in a running state.",
                "remediation": None,
                "fix": None,
            })

    # 11. File descriptor pressure (informational; no automated fix)
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

    health_score = max(0, min(100, health_score))
    
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
    
    cmd = ["journalctl", "-n", str(lines), "--no-pager"]
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
                    d_lines = [l for l in d_out.decode().strip().split('\n') if l.strip()]
                    raw_logs = d_lines[-lines:] if d_lines else []
                except Exception:
                    raw_logs = [out_str or "Unable to read system logs due to permissions"]
            else:
                raw_logs = [l for l in out_str.split('\n') if not l.startswith("Hint:")]
        except asyncio.TimeoutError:
            raw_logs = ["Log read timed out after 15s. The journal may be under heavy write load."]

        parsed_logs = []
        search_lower = search.lower()
        
        for line in raw_logs:
            if not line:
                continue
            if search_lower and search_lower not in line.lower():
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
        connections = psutil.net_connections(kind='inet')
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


@app.get("/api/troubleshoot/bottlenecks")
async def troubleshoot_bottlenecks():
    """
    Identifies top CPU, Memory, and Thread resource bottlenecks
    along with stuck processes.
    """
    procs = []
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
        "description": "Terminates the single highest-CPU non-essential process to relieve a load spike. PID 1, the MonitorX process tree, and essential services (systemd, sshd, containerd, libvirtd, NetworkManager, ...) are never targeted, and the kill is owner-guarded exactly like a manual kill.",
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
    "restart_docker_container": {
        "label": "Restart container",
        "category": "Containers",
        "level": "warning",
        "sudo": False,
        "description": "Runs `docker restart` on the target container so a crashed / exited workload comes back up.",
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
        "systemd", "init", "kthreadd", "kernel", "containerd", "dockerd",
        "libvirtd", "sshd", "dbus-daemon", "dbus-broker", "udevd",
        "systemd-journald", "systemd-udevd", "cron", "rsyslogd", "rsyslog",
        "chronyd", "systemd-resolved", "NetworkManager", "networkmanager",
    }
    my_pid = os.getpid()
    my_uid = os.geteuid()

    procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.cpu_percent()  # initialise the per-process sampler
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

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
            # Owner guard: only kill processes we own unless running as root.
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
    zombies = []
    for proc in psutil.process_iter(['pid', 'name', 'ppid', 'status']):
        try:
            status = (proc.info['status'] or '').lower()
            if status in ('zombie', 'defunct'):
                zombies.append((proc.info['pid'], proc.info['name'] or '?', proc.info['ppid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
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

    remaining = 0
    for proc in psutil.process_iter(['status']):
        try:
            if (proc.info['status'] or '').lower() in ('zombie', 'defunct'):
                remaining += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

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


async def _fix_restart_docker_container(target: Optional[str] = None) -> Dict[str, Any]:
    docker = shutil.which("docker")
    if not docker:
        return {"success": False, "message": "Docker CLI is not available on this host."}
    if not target:
        return {"success": False, "message": "Container name required"}
    try:
        returncode, stdout, stderr = await _run_cmd([docker, "restart", target], timeout=90.0)
    except asyncio.TimeoutError:
        return {"success": False, "message": f"docker restart {target} timed out after 90s"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    if returncode == 0:
        return {"success": True, "message": stdout.decode().strip() or f"Container {target} restarted."}
    return {"success": False, "message": stderr.decode(errors="replace").strip() or f"docker restart {target} failed"}


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
    "restart_docker_container": _fix_restart_docker_container,
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


class FixAllRequest(BaseModel):
    """Batch auto-fix plan: list of {action, target} entries to execute."""
    actions: List[Dict[str, Any]] = Field(default_factory=list, max_length=40)
    confirm: bool = Field(default=False)


@app.post("/api/troubleshoot/fix-all")
async def troubleshoot_fix_all(req: FixAllRequest):
    """
    Execute a whole repair plan sequentially — the hub's one-click "Fix All".

    When `actions` is empty the plan is rebuilt automatically from a fresh
    health scan (every fixable failing check).  `confirm` must be true.
    Returns one result entry per action with per-fix duration and an overall
    summary.
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required for batch remediation.")

    plan = list(req.actions)
    if not plan:
        scan = await troubleshoot_health_check()
        for check in scan.get("checks", []):
            fixes = check.get("fixes") or ([check["fix"]] if check.get("fix") else [])
            for fix in fixes:
                if fix.get("action") not in FIX_EXECUTORS and fix.get("action") not in FIX_ACTION_ALIASES:
                    continue
                plan.append({"action": fix["action"], "target": fix.get("target")})
        if not plan:
            return {"results": [], "summary": {"total": 0, "success": 0, "failed": 0},
                    "message": "Scan found no fixable issues."}

    results = []
    for item in plan[:40]:
        action = str(item.get("action", ""))
        target = item.get("target")
        meta = FIX_ACTION_META.get(FIX_ACTION_ALIASES.get(action, action), {})
        try:
            result = await run_remediation(action, target)
        except HTTPException as e:
            result = {"success": False, "message": e.detail, "action": action, "target": target,
                      "duration_ms": 0, "timestamp": datetime.now().isoformat()}
        result.setdefault("label", meta.get("label", action))
        results.append(result)

    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
    }
    message = (f"Applied {summary['success']} of {summary['total']} fixes successfully."
               if summary["failed"] == 0 else
               f"{summary['success']}/{summary['total']} fixes applied; {summary['failed']} need attention.")
    return {"results": results, "summary": summary, "message": message}


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
        "docker": have("docker"),
        "find": have("find"),
        "sudo": bool(sudo),
    }
    elevated = is_root or has_sudo

    available = {
        "clear_pagecache": elevated and bins["sysctl"],
        "vacuum_journal": elevated and bins["journalctl"],
        "restart_failed_services": bins["systemctl"],
        "restart_service": bins["systemctl"],
        "start_service": bins["systemctl"],
        "enable_service": bins["systemctl"],
        "clear_kernel_logs": elevated and bins["dmesg"],
        "kill_process": True,
        "kill_top_cpu": True,
        "reap_zombies": True,
        "flush_dns": (bins["resolvectl"] or bins["systemd_resolve"] or bins["nscd"]),
        "clean_tmp": elevated and bins["find"],
        "restart_docker_container": bins["docker"],
    }
    return {
        "is_root": is_root,
        "euid": euid,
        "sudo": has_sudo,
        "bins": bins,
        "available_actions": available,
        "fix_actions": {k: {"label": v["label"], "level": v["level"]} for k, v in FIX_ACTION_META.items()},
    }


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

    This endpoint is reachable from the browser and MonitorX has no authentication,
    so accepting an arbitrary shell command would be remote code execution.
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
        output = stdout.decode(errors="replace")
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
    except Exception as e:
        logger.exception("Diagnostic command failed")
        raise HTTPException(status_code=500, detail="Diagnostic command could not be executed.")


# =============================================================================
# DOCKER & CONTAINER REST API ENDPOINTS
# =============================================================================

@app.get("/api/stats/containers")
async def get_containers():
    """List all Docker containers on the host."""
    containers = await get_docker_containers()
    if containers is None:
        raise HTTPException(status_code=404,
                            detail="Docker is not installed or not running on this host.")
    return containers


@app.get("/api/stats/containers/stats")
async def get_container_stats():
    """Get live resource usage for running Docker containers."""
    stats = await get_docker_container_stats()
    if stats is None:
        raise HTTPException(status_code=404,
                            detail="Docker stats unavailable.")
    return stats


@app.get("/api/stats/containers/{container_id}/logs")
async def get_container_logs(container_id: str, lines: int = Query(100, ge=1, le=5000)):
    """Fetch recent logs from a Docker container."""
    logs = await get_docker_container_logs(container_id, lines)
    if logs is None:
        raise HTTPException(status_code=404,
                            detail=f"Cannot fetch logs for container '{container_id}'.")
    return {"container_id": container_id, "lines": lines, "logs": logs}


@app.get("/api/stats/pods")
async def get_pods():
    """List Kubernetes pods if kubectl is available."""
    pods = await get_kubernetes_pods()
    if pods is None:
        raise HTTPException(status_code=404,
                            detail="kubectl is not installed or not configured on this host.")
    return pods


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("MONITORX_HOST", "127.0.0.1"), port=8080)
