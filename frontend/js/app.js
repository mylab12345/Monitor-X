const API_BASE = '';
const WS_URL = `ws://${window.location.host}/ws`;

let ws = null;
let reconnectInterval = null;
let statsData = null;

const state = {
    currentTab: 'dashboard',
    processFilter: 'all',
    processSearch: '',
    allProcesses: [],
    autoRefresh: true
};

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function cpuColor(percent) {
    if (percent > 80) return 'danger';
    if (percent > 60) return 'warning';
    return 'normal';
}

function statusClass(status) {
    const map = { running: 'success', sleeping: 'info', stopped: 'warning', zombie: 'danger', idle: 'info', waiting: 'warning' };
    return map[status?.toLowerCase()] || '';
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

function connectWebSocket() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        document.getElementById('ws-status').className = 'status-indicator';
        document.getElementById('ws-status-text').textContent = 'Connected';
        document.querySelector('.status-dot').className = 'status-dot connected';
        if (reconnectInterval) { clearInterval(reconnectInterval); reconnectInterval = null; }
    };

    ws.onmessage = (event) => {
        try {
            statsData = JSON.parse(event.data);
            updateDashboard(statsData);
            updateLastUpdate();
        } catch (e) {
            console.error('Error parsing message:', e);
        }
    };

    ws.onclose = () => {
        document.querySelector('.status-dot').className = 'status-dot disconnected';
        document.getElementById('ws-status-text').textContent = 'Disconnected';
        if (!reconnectInterval) {
            reconnectInterval = setInterval(() => {
                connectWebSocket();
            }, 3000);
        }
    };

    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        ws.close();
    };
}

function updateDashboard(data) {
    if (!data) return;

    updateCpu(data.cpu);
    updateMemory(data.memory);
    updateDisk(data.disk);
    updateGpu(data.gpu);
    updateNetwork(data.network);
    updateSystem(data.system);
    updateTopProcesses(data.processes);
    checkIssues(data);
    updateUptime(data.system);
}

function updateCpu(cpu) {
    document.getElementById('cpu-total').textContent = cpu.percent_total.toFixed(1) + '%';
    document.getElementById('cpu-cores').textContent = cpu.count_logical;
    document.getElementById('cpu-load').textContent = cpu.load_1min.toFixed(1) + ', ' + cpu.load_5min.toFixed(1) + ', ' + cpu.load_15min.toFixed(1);
    document.getElementById('cpu-freq').textContent = (cpu.frequency_current / 1000).toFixed(2) + ' GHz';

    const barsContainer = document.getElementById('cpu-bars');
    barsContainer.innerHTML = '';
    if (cpu.percent_per_core) {
        for (const pct of cpu.percent_per_core) {
            const bar = document.createElement('div');
            bar.className = 'cpu-bar';
            const fill = document.createElement('div');
            fill.className = 'cpu-bar-fill';
            fill.style.height = Math.min(pct, 100) + '%';
            if (pct > 80) fill.classList.add('danger');
            else if (pct > 60) fill.classList.add('warning');
            bar.appendChild(fill);
            bar.title = pct.toFixed(1) + '%';
            barsContainer.appendChild(bar);
        }
    }
}

function updateMemory(mem) {
    document.getElementById('ram-percent').textContent = mem.percent + '%';
    document.getElementById('ram-bar').style.width = mem.percent + '%';
    document.getElementById('ram-bar').parentElement.className = 'progress-bar ' + (mem.percent > 80 ? 'progress-bar-danger' : mem.percent > 60 ? 'progress-bar-warning' : 'progress-bar-success');
    document.getElementById('ram-used').textContent = formatBytes(mem.used) + ' / ' + formatBytes(mem.total);
    document.getElementById('ram-free').textContent = formatBytes(mem.available);
    document.getElementById('ram-swap').textContent = mem.swap_percent + '%';
}

function updateDisk(disk) {
    const list = document.getElementById('disk-list');
    list.innerHTML = '';
    disk.partitions.forEach(p => {
        const item = document.createElement('div');
        item.className = 'disk-item';
        item.innerHTML = `<span>${p.mountpoint} (${p.fstype})</span><span>${p.percent.toFixed(1)}% (${formatBytes(p.used)}/${formatBytes(p.total)})</span>`;
        list.appendChild(item);
    });
    document.getElementById('disk-percent').textContent = disk.partitions.length > 0 ? disk.partitions[0].percent.toFixed(1) + '%' : '0%';
    document.getElementById('disk-read').textContent = formatBytes(disk.io_read_bytes);
    document.getElementById('disk-write').textContent = formatBytes(disk.io_write_bytes);
}

function updateGpu(gpus) {
    const content = document.getElementById('gpu-content');
    if (!gpus || gpus.length === 0) {
        content.innerHTML = '<p class="no-data">No GPU detected</p>';
        document.getElementById('gpu-total').textContent = 'N/A';
        return;
    }

    let html = '<div class="gpu-grid">';
    gpus.forEach(gpu => {
        html += `<div class="gpu-item">
            <div class="gpu-item-header"><span>${gpu.name}</span><span>${gpu.temperature}°C</span></div>
            <div class="gpu-bars">
                <div class="gpu-bar"><div class="gpu-bar-fill" style="width:${gpu.utilization_gpu}%"></div></div>
                <div class="gpu-bar"><div class="gpu-bar-fill" style="width:${gpu.utilization_memory}%"></div></div>
            </div>
            <div class="gpu-stats">
                <span>GPU: ${gpu.utilization_gpu}%</span>
                <span>MEM: ${gpu.utilization_memory}%</span>
                <span>Power: ${gpu.power_draw}W</span>
            </div>
        </div>`;
    });
    html += '</div>';
    content.innerHTML = html;

    const avgGpu = gpus.reduce((a, g) => a + g.utilization_gpu, 0) / gpus.length;
    document.getElementById('gpu-total').textContent = avgGpu.toFixed(1) + '%';
}

function updateNetwork(net) {
    document.getElementById('net-conn').textContent = net.connections_count;
    const list = document.getElementById('net-list');
    list.innerHTML = '';
    for (const [name, stats] of Object.entries(net.interfaces)) {
        if (name === 'lo') continue;
        const item = document.createElement('div');
        item.className = 'net-item';
        item.innerHTML = `<span>${name}</span><span>↑ ${formatBytes(stats.bytes_sent)} ↓ ${formatBytes(stats.bytes_recv)}</span>`;
        list.appendChild(item);
    }
}

function updateSystem(sys) {
    const info = document.getElementById('system-info');
    info.innerHTML = `
        <span><span>Host:</span><b>${sys.hostname}</b></span>
        <span><span>OS:</span><b>${sys.platform} ${sys.platform_release}</b></span>
        <span><span>Kernel:</span><b>${sys.platform_version}</b></span>
        <span><span>Arch:</span><b>${sys.architecture}</b></span>
        <span><span>Uptime:</span><b>${sys.uptime_str}</b></span>
        <span><span>Boot:</span><b>${sys.boot_time}</b></span>
    `;
    document.getElementById('hostname').textContent = sys.hostname;
    document.getElementById('uptime').textContent = 'Uptime: ' + sys.uptime_str;
}

function updateTopProcesses(processes) {
    const tbody = document.getElementById('top-processes-body');
    tbody.innerHTML = '';
    (processes || []).forEach(p => {
        const row = document.createElement('tr');
        row.innerHTML = `<td>${p.pid}</td><td>${p.name}</td><td>${p.cpu_percent}%</td><td>${p.memory_percent}%</td><td>${p.memory_mb}</td><td>${p.status}</td><td>${p.username}</td>`;
        row.style.cursor = 'pointer';
        row.addEventListener('click', () => showProcessDetail(p.pid));
        tbody.appendChild(row);
    });
}

function updateAllProcesses(processes, data) {
    const tbody = document.getElementById('all-processes-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    (data.processes || []).forEach(p => {
        const row = document.createElement('tr');
        row.innerHTML = `<td><input type="checkbox" class="proc-check" value="${p.pid}"></td><td>${p.pid}</td><td>${p.name}</td><td>${p.cpu_percent}%</td><td>${p.memory_percent}%</td><td>${p.memory_mb}</td><td>${p.status}</td><td>${p.username}</td><td>${p.create_time}</td><td><button class="btn btn-sm btn-danger" onclick="killProcess(${p.pid})">Kill</button></td>`;
        tbody.appendChild(row);
    });
}

function checkIssues(data) {
    const issues = [];

    if (data.cpu && data.cpu.percent_total > 80) {
        issues.push({ type: 'danger', msg: `High CPU usage: ${data.cpu.percent_total.toFixed(1)}%` });
    }

    if (data.memory && data.memory.percent > 85) {
        issues.push({ type: 'danger', msg: `High memory usage: ${data.memory.percent}%` });
    } else if (data.memory && data.memory.percent > 70) {
        issues.push({ type: 'warning', msg: `Memory usage moderate: ${data.memory.percent}%` });
    }

    if (data.disk && data.disk.partitions) {
        data.disk.partitions.forEach(p => {
            if (p.percent > 90) {
                issues.push({ type: 'danger', msg: `Disk full on ${p.mountpoint}: ${p.percent.toFixed(1)}%` });
            } else if (p.percent > 80) {
                issues.push({ type: 'warning', msg: `Disk usage high on ${p.mountpoint}: ${p.percent.toFixed(1)}%` });
            }
        });
    }

    if (data.swap && data.swap.percent > 50) {
        issues.push({ type: 'warning', msg: `Heavy swap usage: ${data.swap.percent}%` });
    }

    if (data.processes && data.processes.length > 0) {
        const zombieProcs = data.processes.filter(p => p.status === 'zombie' || p.status === 'stopped');
        if (zombieProcs.length > 0) {
            issues.push({ type: 'warning', msg: `${zombieProcs.length} zombie/stopped process(es) detected` });
        }
    }

    if (data.vms && data.vms.length > 0) {
        const crashedVMs = data.vms.filter(v => v.state === 'crashed');
        if (crashedVMs.length > 0) {
            issues.push({ type: 'danger', msg: `${crashedVMs.length} VM(s) in crashed state` });
        }
    }

    if (data.gpu) {
        data.gpu.forEach(g => {
            if (g.temperature > 85) {
                issues.push({ type: 'danger', msg: `GPU ${g.index} overheating: ${g.temperature}°C` });
            }
        });
    }

    document.getElementById('issues-count').textContent = issues.length;
    const issuesList = document.getElementById('issues-list');
    issuesList.innerHTML = '';

    if (issues.length === 0) {
        issuesList.innerHTML = '<div class="issue-item success">✓ System looks healthy</div>';
    } else {
        issues.forEach(i => {
            const item = document.createElement('div');
            item.className = `issue-item ${i.type}`;
            item.innerHTML = `<span class="check-status ${i.type === 'danger' ? 'error' : i.type === 'warning' ? 'warn' : 'ok'}">⚠</span> ${i.msg}`;
            issuesList.appendChild(item);
        });
    }
}

function updateLastUpdate() {
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
}

async function showProcessDetail(pid) {
    try {
        const res = await fetch(`${API_BASE}/api/processes/${pid}`);
        if (!res.ok) throw new Error('Process not found');
        const proc = await res.json();

        const modal = document.getElementById('process-modal');
        const body = document.getElementById('modal-body');
        body.innerHTML = `
            <div class="system-info" style="margin-bottom:12px;">
                <span><span>PID:</span><b>${proc.pid}</b></span>
                <span><span>Name:</span><b>${proc.name}</b></span>
                <span><span>Status:</span><b>${proc.status}</b></span>
                <span><span>User:</span><b>${proc.username}</b></span>
                <span><span>CPU%:</span><b>${proc.cpu_percent}%</b></span>
                <span><span>MEM%:</span><b>${proc.memory_percent}%</b></span>
                <span><span>Threads:</span><b>${proc.num_threads}</b></span>
                <span><span>FDs:</span><b>${proc.num_fds}</b></span>
                <span><span>Started:</span><b>${proc.create_time}</b></span>
            </div>
            <h4 style="margin-bottom:6px;">Command Line</h4>
            <pre style="background:var(--bg-primary);padding:8px;border-radius:4px;font-size:0.75rem;">${(proc.cmdline || []).join(' ')}</pre>
            <h4 style="margin:10px 0 6px;">Open Files (${proc.open_files.length})</h4>
            <pre style="background:var(--bg-primary);padding:8px;border-radius:4px;font-size:0.7rem;max-height:150px;overflow-y:auto;">${(proc.open_files || []).map(f => f.path || '').join('\n')}</pre>
            <h4 style="margin:10px 0 6px;">Network Connections (${proc.connections.length})</h4>
            <pre style="background:var(--bg-primary);padding:8px;border-radius:4px;font-size:0.7rem;max-height:150px;overflow-y:auto;">${(proc.connections || []).map(c => `${c.status || ''} ${c.laddr || ''} -> ${c.raddr || ''}`).join('\n')}</pre>
        `;
        modal.classList.add('show');
    } catch (e) {
        showToast('Error loading process details: ' + e.message, 'error');
    }
}

async function killProcess(pid, signal = 15) {
    if (!confirm(`Kill process ${pid} with SIG${signal === 9 ? 'KILL' : 'TERM'}?`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/processes/${pid}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ signal })
        });
        if (res.ok) showToast(`Process ${pid} killed`, 'success');
        else showToast('Failed to kill process', 'error');
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function fetchStats() {
    try {
        const res = await fetch(`${API_BASE}/api/stats`);
        const data = await res.json();
        statsData = data;
        updateDashboard(data);
        updateLastUpdate();
        if (state.currentTab === 'processes') updateAllProcesses(null, data);
    } catch (e) {
        console.error('Fetch error:', e);
    }
}

/* Tab Navigation */
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        state.currentTab = btn.dataset.tab;

        if (state.currentTab === 'processes') fetchStats();
        if (state.currentTab === 'vms') fetchVms();
        if (state.currentTab === 'services') fetchServices();
        if (state.currentTab === 'troubleshoot') runTroubleshootChecks();
    });
});

/* Refresh Button */
document.getElementById('refresh-btn').addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send('ping');
    }
    fetchStats();
    showToast('Refreshed', 'info');
});

/* Theme Toggle */
document.getElementById('theme-toggle').addEventListener('click', () => {
    document.body.classList.toggle('light-theme');
    const theme = document.body.classList.contains('light-theme') ? 'light' : 'dark';
    document.getElementById('theme-toggle').textContent = theme === 'dark' ? '🌙' : '☀️';
});

/* Troubleshoot Checks */
document.getElementById('run-checks').addEventListener('click', runTroubleshootChecks);

async function runTroubleshootChecks() {
    const checks = document.getElementById('sys-checks');
    checks.innerHTML = '<p>Running checks...</p>';

    const results = [];

    results.push({ name: 'CPU Load', ok: statsData?.cpu?.percent_total < 80, detail: `${statsData?.cpu?.percent_total || 0}%` });
    results.push({ name: 'Memory Usage', ok: statsData?.memory?.percent < 85, detail: `${statsData?.memory?.percent || 0}%` });
    results.push({ name: 'Swap Usage', ok: statsData?.memory?.swap_percent < 50, detail: `${statsData?.memory?.swap_percent || 0}%` });

    if (statsData?.disk?.partitions) {
        statsData.disk.partitions.forEach(p => {
            results.push({ name: `Disk ${p.mountpoint}`, ok: p.percent < 90, detail: `${p.percent.toFixed(1)}%` });
        });
    }

    results.push({ name: 'GPU Temp', ok: true, detail: statsData?.gpu ? 'Detected' : 'No GPU' });
    results.push({ name: 'VM Status', ok: true, detail: statsData?.vms ? `${statsData.vms.length} VM(s)` : 'No VMs' });
    results.push({ name: 'System Uptime', ok: true, detail: statsData?.system?.uptime_str || 'N/A' });
    results.push({ name: 'Boot Health', ok: true, detail: statsData?.system?.boot_time || 'N/A' });

    checks.innerHTML = results.map(r => `
        <div class="check-item">
            <span class="check-status ${r.ok ? 'ok' : 'error'}">${r.ok ? '✓' : '✗'}</span>
            <span>${r.name}</span>
            <span style="margin-left:auto;color:var(--text-muted)">${r.detail}</span>
        </div>
    `).join('');
}

/* Recent Errors */
document.getElementById('refresh-errors').addEventListener('click', refreshErrors);

async function refreshErrors() {
    const container = document.getElementById('recent-errors');
    container.innerHTML = '<p>Checking system logs...</p>';

    try {
        const res = await fetch(`${API_BASE}/api/system/logs?lines=20`);
        let logs = [];
        if (res.ok) logs = await res.json();

        if (logs.length === 0) {
            container.innerHTML = '<p class="no-data">No recent errors found</p>';
            return;
        }

        const errors = logs.filter(l => /error|fail|critical|panic|oom|kill/.test(l.toLowerCase()));
        if (errors.length === 0) {
            container.innerHTML = '<p class="no-data">No errors in recent logs</p>';
            return;
        }

        container.innerHTML = errors.map(e => `
            <div class="check-item">
                <span class="check-status error">⚠</span>
                <span style="font-size:0.8rem;word-break:break-all">${escapeHtml(e)}</span>
            </div>
        `).join('');
    } catch (e) {
        container.innerHTML = `<p class="issue-item danger">Error: ${e.message}</p>`;
    }
}

/* Resource Analysis */
async function analyzeResources() {
    const container = document.getElementById('resource-analysis');
    if (!statsData) { container.innerHTML = '<p>No data yet</p>'; return; }

    let html = '';
    const cpu = statsData.cpu?.percent_total || 0;
    const mem = statsData.memory?.percent || 0;
    const disk = statsData.disk?.partitions?.[0]?.percent || 0;

    html += `<div class="check-item"><span class="check-status ${cpu > 80 ? 'error' : cpu > 60 ? 'warn' : 'ok'}"></span> CPU: ${cpu.toFixed(1)}% - ${cpu > 80 ? 'Critical' : cpu > 60 ? 'Warning' : 'Normal'}</div>`;
    html += `<div class="check-item"><span class="check-status ${mem > 85 ? 'error' : mem > 70 ? 'warn' : 'ok'}"></span> Memory: ${mem}% - ${mem > 85 ? 'Critical' : mem > 70 ? 'Warning' : 'Normal'}</div>`;
    html += `<div class="check-item"><span class="check-status ${disk > 90 ? 'error' : disk > 80 ? 'warn' : 'ok'}"></span> Disk: ${disk.toFixed(1)}% - ${disk > 90 ? 'Critical' : disk > 80 ? 'Warning' : 'Normal'}</div>`;

    const topMem = statsData.processes?.slice(0, 3) || [];
    html += '<h4 style="margin-top:8px;">Top Memory Consumers</h4>';
    topMem.forEach(p => {
        html += `<div class="check-item"><span class="check-status ${p.memory_percent > 10 ? 'warn' : 'ok'}"></span> ${p.name} (${p.pid}) - ${p.memory_percent}%</div>`;
    });

    container.innerHTML = html;
}

/* Network Diagnostics */
document.getElementById('run-net-diag').addEventListener('click', runNetDiag);

async function runNetDiag() {
    const container = document.getElementById('net-diagnostics');
    container.innerHTML = '<p>Running network diagnostics...</p>';

    try {
        const res = await fetch(`${API_BASE}/api/system/net-diag`);
        if (res.ok) {
            const data = await res.json();
            container.innerHTML = `<pre style="font-size:0.75rem">${JSON.stringify(data, null, 2)}</pre>`;
        } else {
            container.innerHTML = '<p class="issue-item danger">Network diagnostics endpoint not available</p>';
        }
    } catch (e) {
        container.innerHTML = '<p>Network check: unavailable</p>';
    }
}

/* Command Runner */
document.getElementById('run-cmd').addEventListener('click', runCommand);
document.getElementById('cmd-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runCommand();
});

async function runCommand() {
    const input = document.getElementById('cmd-input');
    const output = document.getElementById('cmd-output');
    const cmd = input.value.trim();
    if (!cmd) return;

    output.textContent = 'Running...';

    try {
        const res = await fetch(`${API_BASE}/api/commands/run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd })
        });
        if (res.ok) {
            const data = await res.json();
            output.textContent = data.output || 'No output';
        } else {
            output.textContent = 'Command failed: ' + (await res.text());
        }
    } catch (e) {
        output.textContent = 'Error: ' + e.message;
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/* Modal Close */
document.getElementById('close-modal').addEventListener('click', () => {
    document.getElementById('process-modal').classList.remove('show');
});

document.getElementById('process-modal').addEventListener('click', (e) => {
    if (e.target === document.getElementById('process-modal')) {
        e.target.classList.remove('show');
    }
});

/* VMs */
async function fetchVms() {
    const container = document.getElementById('vm-list');
    try {
        const res = await fetch(`${API_BASE}/api/stats/vms`);
        if (res.status === 404) {
            container.innerHTML = '<p class="no-data">VM monitoring not available (libvirt not installed)</p>';
            return;
        }
        const vms = await res.json();
        document.getElementById('vm-count').textContent = vms.length + ' VM(s)';

        if (vms.length === 0) {
            container.innerHTML = '<p class="no-data">No VMs found</p>';
            return;
        }

        container.innerHTML = vms.map(vm => `
            <div class="vm-card">
                <div class="vm-card-header">
                    <strong>${vm.name}</strong>
                    <span class="vm-state ${vm.state}">${vm.state.toUpperCase()}</span>
                </div>
                <div class="vm-stats">
                    <div class="vm-stat"><span>ID</span><span>${vm.id}</span></div>
                    <div class="vm-stat"><span>vCPUs</span><span>${vm.vcpus}</span></div>
                    <div class="vm-stat"><span>Memory</span><span>${formatBytes(vm.memory * 1024)}</span></div>
                    <div class="vm-stat"><span>Max Mem</span><span>${formatBytes(vm.max_memory * 1024)}</span></div>
                </div>
            </div>
        `).join('');
    } catch (e) {
        container.innerHTML = `<p class="issue-item danger">Error: ${e.message}</p>`;
    }
}

/* Services */
async function fetchServices() {
    try {
        const res = await fetch(`${API_BASE}/api/services`);
        if (!res.ok) throw new Error('Failed to fetch services');
        const services = await res.json();
        renderServices(services);
    } catch (e) {
        console.error('Error fetching services:', e);
    }
}

function renderServices(services) {
    const tbody = document.getElementById('services-body');
    if (!tbody) return;
    tbody.innerHTML = '';

    const filter = document.getElementById('service-filter').value;
    const search = document.getElementById('service-search').value.toLowerCase();
    state.allServices = services;

    let filtered = services;
    if (filter !== 'all') {
        filtered = filtered.filter(s => {
            if (filter === 'running') return s.active === 'active';
            if (filter === 'stopped') return s.active !== 'active';
            if (filter === 'failed') return s.active === 'failed';
            return true;
        });
    }
    if (search) {
        filtered = filtered.filter(s => s.name.toLowerCase().includes(search) || s.description.toLowerCase().includes(search));
    }

    filtered.forEach(s => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td><b>${s.name}</b></td>
            <td>${s.load}</td>
            <td>${s.active}</td>
            <td>${s.sub}</td>
            <td style="font-size:0.8rem">${s.description}</td>
            <td class="service-actions">
                ${s.active !== 'active' ? `<button class="btn btn-sm btn-success" onclick="controlService('${s.name}','start')">Start</button>` : ''}
                ${s.active === 'active' ? `<button class="btn btn-sm btn-warning" onclick="controlService('${s.name}','stop')">Stop</button>` : ''}
                <button class="btn btn-sm btn-primary" onclick="controlService('${s.name}','restart')">Restart</button>
            </td>
        `;
        tbody.appendChild(row);
    });
}

document.getElementById('service-filter').addEventListener('change', () => {
    if (state.allServices) renderServices(state.allServices);
});

document.getElementById('service-search').addEventListener('input', () => {
    if (state.allServices) renderServices(state.allServices);
});

async function controlService(name, action) {
    if (!confirm(`${action.toUpperCase()} service ${name}?`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/services/${name}/${action}`, { method: 'POST' });
        if (res.ok) {
            showToast(`Service ${name} ${action}ed`, 'success');
            fetchServices();
        } else {
            showToast('Failed to control service', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* Process Kill from processes tab */
document.getElementById('kill-selected').addEventListener('click', () => {
    const checked = document.querySelectorAll('.proc-check:checked');
    if (checked.length === 0) { showToast('No processes selected', 'warning'); return; }
    if (!confirm(`Kill ${checked.length} selected process(es)?`)) return;
    checked.forEach(c => killProcess(parseInt(c.value)));
});

/* Process Search/Filter */
document.getElementById('proc-search').addEventListener('input', (e) => {
    state.processSearch = e.target.value.toLowerCase();
    filterProcesses();
});

document.getElementById('proc-filter').addEventListener('change', (e) => {
    state.processFilter = e.target.value;
    filterProcesses();
});

function filterProcesses() {
    const tbody = document.getElementById('all-processes-body');
    if (!tbody || !statsData?.processes) return;

    let procs = statsData.processes;
    if (state.processSearch) {
        procs = procs.filter(p => p.name.toLowerCase().includes(state.processSearch) || String(p.pid).includes(state.processSearch));
    }
    if (state.processFilter === 'cpu') procs.sort((a, b) => b.cpu_percent - a.cpu_percent);
    else if (state.processFilter === 'mem') procs.sort((a, b) => b.memory_percent - a.memory_percent);
    else if (state.processFilter === 'pid') procs.sort((a, b) => a.pid - b.pid);

    updateAllProcesses(procs, statsData);
}

/* Select All */
document.getElementById('select-all-proc')?.addEventListener('change', (e) => {
    document.querySelectorAll('.proc-check').forEach(c => c.checked = e.target.checked);
});

/* Initialize */
connectWebSocket();
fetchStats();