"""
Monitoring Dashboard Backend - FastAPI Application
Provides real-time system monitoring via WebSocket and REST API
"""
import asyncio
import logging
import os
import platform
import re
import socket
import shutil
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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

# Initialize libvirt if available
libvirt_conn = None
if LIBVIRT_AVAILABLE:
    try:
        libvirt_conn = libvirt.openReadOnly("qemu:///system")
        logger.info("Libvirt connected successfully")
    except Exception as e:
        logger.warning(f"Libvirt connection failed: {e}")
        LIBVIRT_AVAILABLE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    asyncio.create_task(broadcast_stats())
    logger.info("Monitoring Dashboard started")
    yield
    # Shutdown
    if NVML_AVAILABLE:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass
    if libvirt_conn:
        try:
            libvirt_conn.close()
        except Exception:
            pass
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


async def get_vm_stats() -> Optional[List[Dict[str, Any]]]:
    """Return libvirt domain inventory and live metrics for running KVM guests.

    Libvirt exposes CPU time and I/O counters cumulatively, therefore rates are
    derived from two successive samples. Values are zero on the first poll.
    """
    if not LIBVIRT_AVAILABLE or not libvirt_conn:
        return None

    state_map = {
        libvirt.VIR_DOMAIN_NOSTATE: "no_state", libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "blocked", libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown", libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
        libvirt.VIR_DOMAIN_CRASHED: "crashed", libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
    }
    async with vm_metrics_lock:
        now = time.monotonic()
        try:
            domains = libvirt_conn.listAllDomains(0)
        except Exception as exc:
            logger.warning("Could not list libvirt domains: %s", exc)
            return None

        vms: List[Dict[str, Any]] = []
        active_domain_ids = set()
        for domain in domains:
            try:
                info = domain.info()  # state, maxMem KiB, memory KiB, vCPUs, cpuTime ns
                state = state_map.get(info[0], "unknown")
                domain_id = domain.ID() if domain.isActive() else -1
                vm: Dict[str, Any] = {
                    "id": domain_id, "uuid": domain.UUIDString(), "name": domain.name(),
                    "state": state, "active": bool(domain.isActive()), "vcpus": info[3],
                    "max_memory": info[1], "memory": info[2], "cpu_time": info[4],
                    "cpu_percent": 0.0, "memory_used": 0, "memory_total": info[1],
                    "memory_percent": 0.0, "disk_read_bytes_sec": 0.0,
                    "disk_write_bytes_sec": 0.0, "network_rx_bytes_sec": 0.0,
                    "network_tx_bytes_sec": 0.0, "rates_available": False,
                    "disks": [], "interfaces": [],
                }
                if not vm["active"]:
                    vms.append(vm)
                    continue

                active_domain_ids.add(vm["uuid"])
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

                previous = vm_metric_samples.get(vm["uuid"])
                if previous:
                    elapsed = now - previous["time"]
                    if elapsed > 0:
                        vm["cpu_percent"] = round(min(100, max(0, (info[4] - previous["cpu_time"]) / elapsed / 1e7 / max(info[3], 1))), 1)
                        vm["disk_read_bytes_sec"] = round(max(0, (disk_read - previous["disk_read"]) / elapsed), 1)
                        vm["disk_write_bytes_sec"] = round(max(0, (disk_write - previous["disk_write"]) / elapsed), 1)
                        vm["network_rx_bytes_sec"] = round(max(0, (net_rx - previous["net_rx"]) / elapsed), 1)
                        vm["network_tx_bytes_sec"] = round(max(0, (net_tx - previous["net_tx"]) / elapsed), 1)
                        vm["rates_available"] = True
                vm_metric_samples[vm["uuid"]] = {"time": now, "cpu_time": info[4], "disk_read": disk_read,
                                                    "disk_write": disk_write, "net_rx": net_rx, "net_tx": net_tx}
                vms.append(vm)
            except Exception as exc:
                logger.warning("Could not collect metrics for a libvirt domain: %s", exc)

        # Discard counters for guests that were stopped or removed.
        for domain_uuid in list(vm_metric_samples):
            if domain_uuid not in active_domain_ids:
                vm_metric_samples.pop(domain_uuid, None)
        return vms


async def collect_all_stats() -> SystemStats:
    """Collect a consistent stats snapshot.

    Disk and network rates use previous samples, so serializing collection avoids
    concurrent REST/WebSocket requests corrupting those calculations.
    """
    async with stats_lock:
        cpu = await get_cpu_stats()
        memory = await get_memory_stats()
        disk = await get_disk_stats()
        network = await get_network_stats()
        gpu = await get_gpu_stats()
        processes = await get_process_stats()
        system = await get_system_info()
        vms = await get_vm_stats()
        return SystemStats(
            timestamp=datetime.now().isoformat(), cpu=cpu, memory=memory, disk=disk,
            network=network, gpu=gpu, processes=processes, system=system, vms=vms
        )


async def broadcast_stats():
    """Background task to broadcast stats to all connected clients"""
    while True:
        try:
            stats = await collect_all_stats()
            await manager.broadcast(stats.model_dump())
        except Exception as e:
            logger.error(f"Error broadcasting stats: {e}")
        await asyncio.sleep(2)


# REST API Endpoints
@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard page"""
    with open(str(FRONTEND_DIR / "index.html"), "r") as f:
        return f.read()


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
async def get_processes(limit: int = 30):
    return await get_process_stats(limit)


@app.get("/api/stats/system")
async def get_system():
    return await get_system_info()


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
        stats = await collect_all_stats()
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


# Process management endpoints
@app.get("/api/processes/{pid}")
async def get_process_detail(pid: int):
    """Get detailed process information"""
    try:
        proc = psutil.Process(pid)
        return {
            "pid": proc.pid,
            "name": proc.name(),
            "exe": proc.exe() if hasattr(proc, 'exe') else "",
            "cmdline": proc.cmdline() if hasattr(proc, 'cmdline') else [],
            "status": proc.status(),
            "username": proc.username() if hasattr(proc, 'username') else "unknown",
            "create_time": datetime.fromtimestamp(proc.create_time()).isoformat(),
            "cpu_percent": proc.cpu_percent(interval=0.1),
            "memory_percent": round(proc.memory_percent(), 2),
            "memory_info": dict(proc.memory_info()._asdict()) if hasattr(proc, 'memory_info') else {},
            "num_threads": proc.num_threads() if hasattr(proc, 'num_threads') else 1,
            "num_fds": proc.num_fds() if hasattr(proc, 'num_fds') else 0,
            "connections": [conn._asdict() for conn in proc.connections()] if hasattr(proc, 'connections') else [],
            "open_files": [f._asdict() for f in proc.open_files()] if hasattr(proc, 'open_files') and proc.open_files() else [],
            "environ": dict(list(proc.environ().items())[:20]) if hasattr(proc, 'environ') and proc.environ() else {}
        }
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="Process not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied")


@app.post("/api/processes/{pid}/kill")
async def kill_process(pid: int, signal: int = Query(15)):
    """Terminate a process with SIGTERM or SIGKILL."""
    if signal not in (9, 15):
        raise HTTPException(status_code=400, detail="Only SIGTERM (15) and SIGKILL (9) are allowed.")
    try:
        proc = psutil.Process(pid)
        proc.send_signal(signal)
        await asyncio.sleep(0.5)
        if proc.is_running():
            proc.kill()
        return {"success": True, "message": f"Process {pid} terminated"}
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
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    output = (stderr or stdout).decode().strip()
    if proc.returncode:
        if "password is required" in output.lower() or "not allowed" in output.lower():
            output = "MonitorX is not authorized to control system services. Run systemd/install-service.sh, then restart MonitorX."
        return None, output or f"systemctl {action} failed (exit code {proc.returncode})."
    return {"output": stdout.decode().strip()}, None


@app.get("/api/services/capabilities")
async def service_capabilities():
    """Expose whether the running dashboard can execute service controls."""
    if os.geteuid() == 0:
        return {"can_control": True, "mode": "root", "message": "Service controls are available."}
    sudo = shutil.which("sudo")
    if not sudo:
        return {"can_control": False, "mode": "unconfigured", "message": "sudo is unavailable; run the MonitorX installer."}
    proc = await asyncio.create_subprocess_exec(
        sudo, "-n", "-l", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()
    available = proc.returncode == 0
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
        proc = await asyncio.create_subprocess_exec(
            SYSTEMCTL_BIN, "list-units", "--type=service", "--no-pager", "--no-legend", "--all",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            raise HTTPException(status_code=503, detail=stderr.decode().strip() or "systemd is unavailable")
        services = []
        for line in stdout.decode().strip().split('\n'):
            parts = line.split()
            if len(parts) >= 4:
                services.append({"name": parts[0], "load": parts[1], "active": parts[2], "sub": parts[3],
                                 "description": " ".join(parts[4:]) if len(parts) > 4 else ""})
        return services
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
            "remediation": "Identify and terminate runaway process from Bottlenecks view.",
            "action": "view_bottlenecks"
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
            "remediation": "Monitor active processes for unexpected threads.",
            "action": None
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
            "action": None
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
            "remediation": "Clear page cache or restart high memory consumers.",
            "action": "clear_pagecache"
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
            "remediation": "Consider dropping page caches or expanding swap space.",
            "action": "clear_pagecache"
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
            "action": None
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
            "remediation": "Vacuum systemd journal or clean temp files.",
            "action": "vacuum_journal"
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
            "remediation": "Vacuum journal files or archive old log files.",
            "action": "vacuum_journal"
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
            "action": None
        })

    # 4. Systemd Failed Services
    failed_services = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "list-units", "--state=failed", "--no-pager", "--no-legend",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split('\n')
        for line in lines:
            if line.strip():
                failed_services.append(line.split()[0])
    except Exception:
        pass

    if failed_services:
        health_score -= 15 * len(failed_services)
        checks.append({
            "id": "systemd_services",
            "category": "Services",
            "name": "Systemd Service Health",
            "status": "critical" if len(failed_services) > 1 else "warning",
            "value": f"{len(failed_services)} failed unit(s): {', '.join(failed_services[:3])}",
            "message": f"Found failed systemd service(s): {', '.join(failed_services)}",
            "remediation": "Try restarting failed services.",
            "action": "restart_failed_services"
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
            "action": None
        })

    # 5. Zombie & Disk-Sleep (D State) Processes
    all_procs = await get_process_stats(limit=200)
    zombies = [p for p in all_procs if p["status"] == "zombie"]
    d_states = [p for p in all_procs if p["status"] == "uninterruptible sleep" or p["status"] == "stopped"]
    
    if zombies or d_states:
        health_score -= 10
        msg_parts = []
        if zombies: msg_parts.append(f"{len(zombies)} zombie process(es)")
        if d_states: msg_parts.append(f"{len(d_states)} hung/stopped process(es)")
        checks.append({
            "id": "zombie_hung",
            "category": "Processes",
            "name": "Zombie & Hung Processes",
            "status": "warning",
            "value": ", ".join(msg_parts),
            "message": f"Detected stuck process states: {', '.join(msg_parts)}.",
            "remediation": "Inspect processes in Process Manager.",
            "action": "view_processes"
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
            "action": None
        })

    # 6. Kernel & Log Errors (dmesg / journalctl)
    kernel_errors = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "dmesg", "-T",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
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
            "remediation": "Inspect full system logs in Log Inspector.",
            "action": "view_logs"
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
            "action": None
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
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", "8.8.8.8",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        ping_ok = (proc.returncode == 0)
    except Exception:
        pass

    if not ping_ok or not dns_ok:
        health_score -= 15
        checks.append({
            "id": "net_connectivity",
            "category": "Network",
            "name": "Network & DNS Connectivity",
            "status": "warning",
            "value": f"Ping: {'OK' if ping_ok else 'FAIL'}, DNS: {'OK' if dns_ok else 'FAIL'}",
            "message": "Network ping test or DNS resolution failed.",
            "remediation": "Run network diagnostic tests.",
            "action": "run_net_diag"
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
            "action": None
        })

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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        out_str = stdout.decode().strip()
        
        if "No journal files were opened due to insufficient permissions" in out_str or not out_str:
            try:
                dmesg_proc = await asyncio.create_subprocess_exec(
                    "dmesg", "-T",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                d_out, _ = await dmesg_proc.communicate()
                d_lines = [l for l in d_out.decode().strip().split('\n') if l.strip()]
                raw_logs = d_lines[-lines:] if d_lines else []
            except Exception:
                raw_logs = [out_str or "Unable to read system logs due to permissions"]
        else:
            raw_logs = [l for l in out_str.split('\n') if not l.startswith("Hint:")]

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
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", str(count), "-W", "3", host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode()
        
        loss_match = re.search(r'(\d+)% packet loss', out)
        rtt_match = re.search(r'(rtt|round-trip) min/avg/max/(mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)', out)
        
        return {
            "success": proc.returncode == 0,
            "host": host,
            "raw_output": out or stderr.decode(),
            "packet_loss_percent": float(loss_match.group(1)) if loss_match else (0.0 if proc.returncode == 0 else 100.0),
            "min_rtt": float(rtt_match.group(3)) if rtt_match else None,
            "avg_rtt": float(rtt_match.group(4)) if rtt_match else None,
            "max_rtt": float(rtt_match.group(5)) if rtt_match else None
        }
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
        loop = asyncio.get_event_loop()
        addrs = await loop.getaddrinfo(domain, None)
        ips = list(set([a[4][0] for a in addrs]))
        latency_ms = round((time.time() - start_time) * 1000, 2)
        results["local"] = {"success": True, "ips": ips, "latency_ms": latency_ms}
    except Exception as e:
        results["local"] = {"success": False, "error": str(e)}

    # Google DNS
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig", "+short", "+time=2", "@8.8.8.8", domain,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode().strip()
        if out:
            results["google_dns"] = {"success": True, "ips": [line.strip() for line in out.split('\n') if line.strip()]}
        else:
            results["google_dns"] = {"success": False, "error": "No response"}
    except Exception:
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
            proc = await asyncio.create_subprocess_exec(
                "ss", "-tulpn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
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
        except Exception as e:
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


@app.post("/api/troubleshoot/remediate")
async def perform_remediation(req: RemediateRequest):
    """
    Executes automated safe fix and remediation actions.
    """
    action = req.action
    target = req.target

    if action == "clear_pagecache":
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", SYSCTL_BIN, "-w", "vm.drop_caches=3",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return {"success": True, "message": "RAM page cache cleared successfully!"}
            else:
                return {"success": False, "message": f"Sudo permissions required: {stderr.decode().strip() or 'Access denied'}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    elif action == "restart_failed_services":
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "list-units", "--state=failed", "--no-pager", "--no-legend",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            failed_units = [line.split()[0] for line in stdout.decode().strip().split('\n') if line.strip()]

            if not failed_units:
                return {"success": True, "message": "No failed services to restart."}

            restarted = []
            failed_restarts = []
            errors = []
            for unit in failed_units:
                # Failed units can include non-service units; service controls are deliberately limited.
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
                "errors": errors
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    elif action == "vacuum_journal":
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", JOURNALCTL_BIN, "--vacuum-time=2d",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            return {"success": proc.returncode == 0, "message": stdout.decode().strip() or stderr.decode().strip()}
        except Exception as e:
            return {"success": False, "message": str(e)}

    elif action == "kill_process":
        if not target or not target.isdigit():
            raise HTTPException(status_code=400, detail="Target PID required")
        pid = int(target)
        try:
            proc = psutil.Process(pid)
            pname = proc.name()
            proc.kill()
            return {"success": True, "message": f"Terminated process {pid} ({pname})"}
        except psutil.NoSuchProcess:
            return {"success": False, "message": f"Process {pid} no longer active"}
        except psutil.AccessDenied:
            return {"success": False, "message": f"Permission denied to terminate PID {pid}"}

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported remediation action: {action}")


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
