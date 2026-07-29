"""
Monitoring Dashboard Backend - FastAPI Application
Provides real-time system monitoring via WebSocket and REST API
"""
import asyncio
import json
import logging
import os
import platform
import socket
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
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

# Global state
connected_clients: List[WebSocket] = []
system_stats_cache: Dict[str, Any] = {}
stats_lock = asyncio.Lock()

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
        except:
            pass
    if libvirt_conn:
        libvirt_conn.close()
    logger.info("Monitoring Dashboard stopped")


BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(
    title="System Monitoring Dashboard",
    description="Real-time system monitoring dashboard with WebSocket support",
    version="1.0.0",
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


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    memory_mb: float
    status: str
    username: str
    create_time: str


class GPUInfo(BaseModel):
    index: int
    name: str
    temperature: int
    utilization_gpu: int
    utilization_memory: int
    memory_used: int
    memory_total: int
    memory_free: int
    power_draw: float
    power_limit: float


class VMInfo(BaseModel):
    id: int
    name: str
    state: str
    cpu_time: int
    max_memory: int
    memory: int
    vcpus: int


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
    """Get CPU statistics"""
    cpu_percent = psutil.cpu_percent(interval=0.5, percpu=True)
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    load_avg = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
    
    return {
        "percent_per_core": cpu_percent,
        "percent_total": sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0,
        "count_logical": cpu_count,
        "count_physical": cpu_count_physical,
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
        "percent": vm.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_free": swap.free,
        "swap_percent": swap.percent
    }


async def get_disk_stats() -> Dict[str, Any]:
    """Get disk statistics"""
    partitions = psutil.disk_partitions()
    disks = []
    
    for partition in partitions:
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append({
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "fstype": partition.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": (usage.used / usage.total * 100) if usage.total > 0 else 0
            })
        except PermissionError:
            continue
    
    disk_io = psutil.disk_io_counters()
    
    return {
        "partitions": disks,
        "io_read_bytes": disk_io.read_bytes if disk_io else 0,
        "io_write_bytes": disk_io.write_bytes if disk_io else 0,
        "io_read_count": disk_io.read_count if disk_io else 0,
        "io_write_count": disk_io.write_count if disk_io else 0
    }


async def get_network_stats() -> Dict[str, Any]:
    """Get network statistics"""
    net_io = psutil.net_io_counters(pernic=True)
    interfaces = {}
    
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
    
    # Get network connections count
    connections = len(psutil.net_connections())
    
    return {
        "interfaces": interfaces,
        "connections_count": connections
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
            
            # Get GPU info
            name = nvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode('utf-8')
            
            # Temperature
            try:
                temp = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            except:
                temp = 0
            
            # Utilization
            try:
                util = nvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_util = util.gpu
                mem_util = util.memory
            except:
                gpu_util = 0
                mem_util = 0
            
            # Memory
            try:
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                mem_used = mem.used
                mem_total = mem.total
                mem_free = mem.free
            except:
                mem_used = mem_total = mem_free = 0
            
            # Power
            try:
                power_draw = nvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW to W
                power_limit = nvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)[1] / 1000.0
            except:
                power_draw = 0
                power_limit = 0
            
            gpus.append({
                "index": i,
                "name": name,
                "temperature": temp,
                "utilization_gpu": gpu_util,
                "utilization_memory": mem_util,
                "memory_used": mem_used,
                "memory_total": mem_total,
                "memory_free": mem_free,
                "power_draw": power_draw,
                "power_limit": power_limit
            })
    except Exception as e:
        logger.error(f"Error getting GPU stats: {e}")
        return None
    
    return gpus if gpus else None


async def get_process_stats(limit: int = 20) -> List[Dict[str, Any]]:
    """Get top processes by CPU usage"""
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'username', 'create_time']):
        try:
            info = proc.info
            if info['cpu_percent'] is not None and info['cpu_percent'] > 0:
                processes.append({
                    "pid": info['pid'],
                    "name": info['name'][:50] if info['name'] else "unknown",
                    "cpu_percent": round(info['cpu_percent'], 1),
                    "memory_percent": round(info['memory_percent'], 1) if info['memory_percent'] else 0,
                    "memory_mb": round(info['memory_info'].rss / 1024 / 1024, 1) if info['memory_info'] else 0,
                    "status": info['status'],
                    "username": info['username'] or "unknown",
                    "create_time": datetime.fromtimestamp(info['create_time']).strftime('%Y-%m-%d %H:%M:%S') if info['create_time'] else "unknown"
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Sort by CPU usage and limit
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
    """Get VM statistics using libvirt"""
    if not LIBVIRT_AVAILABLE or not libvirt_conn:
        return None
    
    vms = []
    try:
        for domain_id in libvirt_conn.listDomainsID():
            domain = libvirt_conn.lookupByID(domain_id)
            info = domain.info()
            
            state_map = {
                libvirt.VIR_DOMAIN_NOSTATE: "no_state",
                libvirt.VIR_DOMAIN_RUNNING: "running",
                libvirt.VIR_DOMAIN_BLOCKED: "blocked",
                libvirt.VIR_DOMAIN_PAUSED: "paused",
                libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
                libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
                libvirt.VIR_DOMAIN_CRASHED: "crashed",
                libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended"
            }
            
            vms.append({
                "id": domain_id,
                "name": domain.name(),
                "state": state_map.get(info[0], "unknown"),
                "cpu_time": info[2],  # CPU time in nanoseconds
                "max_memory": info[1],  # Max memory in KB
                "memory": info[2],  # Current memory in KB
                "vcpus": info[3]
            })
    except Exception as e:
        logger.error(f"Error getting VM stats: {e}")
        return None
    
    return vms if vms else None


async def collect_all_stats() -> SystemStats:
    """Collect all system statistics"""
    cpu = await get_cpu_stats()
    memory = await get_memory_stats()
    disk = await get_disk_stats()
    network = await get_network_stats()
    gpu = await get_gpu_stats()
    processes = await get_process_stats()
    system = await get_system_info()
    vms = await get_vm_stats()
    
    return SystemStats(
        timestamp=datetime.now().isoformat(),
        cpu=cpu,
        memory=memory,
        disk=disk,
        network=network,
        gpu=gpu,
        processes=processes,
        system=system,
        vms=vms
    )


async def broadcast_stats():
    """Background task to broadcast stats to all connected clients"""
    while True:
        try:
            stats = await collect_all_stats()
            await manager.broadcast(stats.model_dump())
        except Exception as e:
            logger.error(f"Error broadcasting stats: {e}")
        await asyncio.sleep(2)  # Update every 2 seconds


# REST API Endpoints
@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard page"""
    with open(str(FRONTEND_DIR / "index.html"), "r") as f:
        return f.read()


@app.get("/api/stats", response_model=SystemStats)
async def get_stats():
    """Get current system statistics via REST API"""
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
async def get_processes(limit: int = 20):
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
async def health_check():
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
        # Send initial stats immediately
        stats = await collect_all_stats()
        await websocket.send_json(stats.model_dump())
        
        # Keep connection alive
        while True:
            data = await websocket.receive_text()
            # Handle any client messages if needed
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
            "exe": proc.exe(),
            "cmdline": proc.cmdline(),
            "status": proc.status(),
            "username": proc.username(),
            "create_time": datetime.fromtimestamp(proc.create_time()).isoformat(),
            "cpu_percent": proc.cpu_percent(interval=0.5),
            "memory_percent": proc.memory_percent(),
            "memory_info": dict(proc.memory_info()._asdict()),
            "num_threads": proc.num_threads(),
            "num_fds": proc.num_fds() if hasattr(proc, 'num_fds') else 0,
            "connections": [conn._asdict() for conn in proc.connections()] if hasattr(proc, 'connections') else [],
            "open_files": [f._asdict() for f in proc.open_files()] if proc.open_files() else [],
            "environ": proc.environ() if hasattr(proc, 'environ') else {}
        }
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="Process not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="Access denied")


@app.post("/api/processes/{pid}/kill")
async def kill_process(pid: int, signal: int = 15):
    """Kill a process (default SIGTERM=15, SIGKILL=9)"""
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


# System control endpoints
@app.post("/api/system/reboot")
async def reboot_system():
    """Reboot the system (requires root)"""
    try:
        await asyncio.create_subprocess_exec("systemctl", "reboot")
        return {"success": True, "message": "Reboot initiated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/system/shutdown")
async def shutdown_system():
    """Shutdown the system (requires root)"""
    try:
        await asyncio.create_subprocess_exec("systemctl", "poweroff")
        return {"success": True, "message": "Shutdown initiated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/services")
async def list_services():
    """List systemd services"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "list-units", "--type=service", "--no-pager", "--no-legend",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        services = []
        for line in stdout.decode().strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 4:
                    services.append({
                        "name": parts[0],
                        "load": parts[1],
                        "active": parts[2],
                        "sub": parts[3],
                        "description": " ".join(parts[4:]) if len(parts) > 4 else ""
                    })
        return services
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/services/{service_name}/{action}")
async def control_service(service_name: str, action: str):
    """Control a systemd service (start/stop/restart/status)"""
    valid_actions = ["start", "stop", "restart", "reload", "enable", "disable"]
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Invalid action. Valid: {valid_actions}")
    
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", action, service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=stderr.decode())
        return {"success": True, "message": f"Service {service_name} {action}ed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/net-diag")
async def net_diagnostics():
    """Run network diagnostics"""
    results = {}
    
    # Check network interfaces
    try:
        addrs = psutil.net_if_addrs()
        results["interfaces"] = {}
        for iface, addr_list in addrs.items():
            results["interfaces"][iface] = [
                {"family": str(a.family), "address": a.address, "netmask": a.netmask}
                for a in addr_list
            ]
    except Exception as e:
        results["interfaces_error"] = str(e)
    
    # Check network connections summary
    try:
        connections = psutil.net_connections(kind='inet')
        results["total_connections"] = len(connections)
        tcp_states = {}
        for conn in connections:
            state = conn.status
            tcp_states[state] = tcp_states.get(state, 0) + 1
        results["tcp_states"] = tcp_states
    except Exception as e:
        results["connections_error"] = str(e)
    
    # Check DNS resolution
    try:
        import socket as sock
        results["dns_google"] = sock.gethostbyname("google.com") != ""
    except:
        results["dns_google"] = False
    
    # Ping test
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", "8.8.8.8",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        results["ping_google_dns"] = proc.returncode == 0
    except:
        results["ping_google_dns"] = False
    
    return results


@app.get("/api/system/logs")
async def system_logs(lines: int = 50):
    """Get recent system logs"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-n", str(lines), "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()
        
        if not output:
            # Fallback to /var/log/syslog
            try:
                with open("/var/log/syslog", "r") as f:
                    all_lines = f.readlines()
                    output = "".join(all_lines[-lines:])
            except FileNotFoundError:
                # Fallback to dmesg
                proc2 = await asyncio.create_subprocess_exec(
                    "dmesg", "-n", str(lines),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout2, stderr2 = await proc2.communicate()
                output = stdout2.decode().strip()
        
        return {"logs": output.split('\n') if output else []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/commands/run")
async def run_command(request: Request):
    """Run a shell command (dangerous - should be protected in production)"""
    try:
        body = await request.json()
        cmd = body.get("command", "")
    except:
        raise HTTPException(status_code=400, detail="Invalid request body")
    
    if not cmd:
        raise HTTPException(status_code=400, detail="No command provided")
    
    # Block dangerous commands
    dangerous = ["rm -rf /", "mkfs", "dd if=", "shutdown", "reboot", "halt", "poweroff"]
    for d in dangerous:
        if d in cmd.lower():
            raise HTTPException(status_code=403, detail=f"Blocked dangerous command: {d}")
    
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode()
        error = stderr.decode()
        return {
            "output": output,
            "error": error,
            "returncode": proc.returncode
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=500, detail="Command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)