/* MonitorX v2.0 - Application Logic */
const API_BASE = '';
const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;

let ws = null;
let reconnectTimer = null;
let statsData = null;
let autoTailInterval = null;
let healthData = null;
let serviceCapabilities = null;
// Auto-Fix Engine state
let fixCapabilities = null;
let fixRunning = false;
let currentFixPlan = [];

// History buffer for sparkline charts (last 30 samples)
const historyBuffer = {
    cpu: new Array(30).fill(0),
    mem: new Array(30).fill(0),
    netRx: new Array(30).fill(0),
    netTx: new Array(30).fill(0)
};

const state = {
    currentTab: 'dashboard',
    currentSubTab: 'health-hub',
    processFilter: 'cpu',
    processSearch: '',
    logLevel: 'all',
    logLines: 100,
    logAutoTail: false,
    cmdHistory: [],
    cmdHistoryIndex: -1,
    vmSearch: '',
    vmStateFilter: 'all',
    vmSort: 'name',
    vmSelected: new Set(),
    vmRefreshMs: 2000,
    vmAutoTimer: null,
    vmCapabilities: null,
    vmPending: new Set(),
    vmLastAction: new Map(),
    // Console state
    consoleTerminal: null,
    consoleWs: null,
    consoleAddonFit: null,
    consoleVmId: null,
    consoleResizeObserver: null,
    // Resize state
    resizeVmId: null,
    resizeVcpus: 2,
    resizeMemMb: 2048,
    // Container stats cache
    containerStats: {},
};

/* Format Helpers */
function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatSpeed(bytesPerSec) {
    if (!bytesPerSec || bytesPerSec === 0) return '0 B/s';
    return formatBytes(bytesPerSec) + '/s';
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.4s ease';
        setTimeout(() => toast.remove(), 400);
    }, 4000);
}

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = String(value ?? '');
    return div.innerHTML;
}

function escapeAttr(value) {
    return escapeHtml(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Escape a value for use inside a quoted CSS attribute selector. */
function attrSel(value) {
    return String(value ?? '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function fixMeta(action) {
    const meta = { label: action, level: 'info', sudo: false, description: '' };
    if (fixCapabilities?.fix_actions && fixCapabilities.fix_actions[action]) {
        meta.label = fixCapabilities.fix_actions[action].label || meta.label;
        meta.level = fixCapabilities.fix_actions[action].level || meta.level;
    }
    return meta;
}

function fixActionAvailable(action) {
    return !fixCapabilities || !fixCapabilities.available_actions ||
           fixCapabilities.available_actions[action] !== false;
}

/* WebSocket Connection */
function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        document.getElementById('ws-status').className = 'status-indicator';
        document.getElementById('ws-status-text').textContent = 'Connected';
        document.querySelector('.status-dot').className = 'status-dot connected';
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };
    ws.onmessage = (event) => {
        try {
            statsData = JSON.parse(event.data);
            // P1: aria-live announcement + throttle (update max 1/sec)
            if (window.__announceTimeout) clearTimeout(window.__announceTimeout);
            window.__announceTimeout = setTimeout(() => {
                const ann = document.getElementById('sparkline-announce');
                if (ann && statsData) ann.textContent = `CPU ${(statsData.cpu?.percent ?? '--')}%, Memory ${(statsData.memory?.percent ?? '--')}%`;
            }, 1000);
            updateDashboard(statsData);
            updateLastUpdate();
        } catch (e) { console.error('Error parsing WebSocket frame:', e); }
    };
    ws.onclose = () => {
        document.querySelector('.status-dot').className = 'status-dot disconnected';
        document.getElementById('ws-status-text').textContent = 'Disconnected';
        if (!reconnectTimer) {
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                connectWebSocket();
            }, 3000);
        }
    };
    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        ws.close();
    };
}

/* Dashboard Updates */
// Each panel is updated inside its own guarded slot: a rendering error in one
// panel (missing field, bad data shape, a transient null) degrades only that
// panel — the other live panels keep rendering on the next WebSocket frame.
function updateDashboard(data) {
    if (!data) return;

    const guard = (name, fn) => {
        try { fn(); }
        catch (e) { console.error(`[panel:${name}] render failed:`, e); }
    };

    guard('cpu',      () => updateCpu(data.cpu));
    guard('memory',   () => updateMemory(data.memory));
    guard('disk',     () => updateDisk(data.disk));
    guard('network',  () => updateNetwork(data.network));
    guard('gpu',      () => updateGpu(data.gpu));
    guard('thermal',  () => updateThermal(data.thermal));
    guard('system',   () => updateSystem(data.system));
    guard('processes',() => updateTopProcesses(data.processes));
    guard('issues',   () => checkOSIssues(data));
    guard('charts',   () => updateCharts(data));

    if (state.currentTab === 'processes') guard('process-table', filterProcesses);

    // VM panel: render inventory only when we have a real payload.
    if (state.currentTab === 'vms') {
        if (Array.isArray(data.vms)) guard('vms', () => renderVms(data.vms));
        else if (data.vms === null) guard('vms', renderVmsUnavailable);
    }

    if (state.currentTab === 'dashboard') {
        // Containers: [] = daemon up with zero containers; null = unavailable.
        if (Array.isArray(data.containers)) guard('containers', () => renderContainers(data.containers));
        else if (data.containers === null) guard('containers', renderContainersUnavailable);
        if (Array.isArray(data.pods)) {
            document.getElementById('pods-panel').style.display = data.pods.length > 0 ? 'block' : 'none';
            guard('pods', () => renderPods(data.pods));
        } else if (data.pods === null) {
            document.getElementById('pods-panel').style.display = 'none';
            const pc = document.getElementById('pod-count');
            if (pc) pc.textContent = 'K8s N/A';
        }
    }
}

function updateCpu(cpu) {
    if (!cpu) return;
    document.getElementById('cpu-total').textContent = cpu.percent_total.toFixed(1) + '%';
    document.getElementById('cpu-cores').textContent = cpu.count_logical;
    document.getElementById('cpu-load').textContent = `${cpu.load_1min.toFixed(2)}, ${cpu.load_5min.toFixed(2)}, ${cpu.load_15min.toFixed(2)}`;
    document.getElementById('cpu-freq').textContent = (cpu.frequency_current / 1000).toFixed(2) + ' GHz';

    const barsContainer = document.getElementById('cpu-bars');
    barsContainer.innerHTML = '';
    if (cpu.percent_per_core) {
        cpu.percent_per_core.forEach((pct, idx) => {
            const bar = document.createElement('div');
            bar.className = 'cpu-bar';
            const fill = document.createElement('div');
            fill.className = 'cpu-bar-fill';
            fill.style.height = Math.min(pct, 100) + '%';
            if (pct > 85) fill.classList.add('danger');
            else if (pct > 65) fill.classList.add('warning');
            bar.appendChild(fill);
            bar.title = `Core ${idx}: ${pct.toFixed(1)}%`;
            barsContainer.appendChild(bar);
        });
    }
}

function updateMemory(mem) {
    if (!mem) return;
    document.getElementById('ram-percent').textContent = mem.percent + '%';
    const fill = document.getElementById('ram-bar');
    fill.style.width = mem.percent + '%';
    if (mem.percent > 85) fill.style.background = 'var(--danger)';
    else if (mem.percent > 70) fill.style.background = 'var(--warning)';
    else fill.style.background = 'var(--accent)';

    document.getElementById('ram-used').textContent = formatBytes(mem.used);
    document.getElementById('ram-free').textContent = formatBytes(mem.available);
    document.getElementById('ram-cached').textContent = formatBytes((mem.buffers || 0) + (mem.cached || 0));
    document.getElementById('ram-swap').textContent = mem.swap_percent + '%';
}

function updateDisk(disk) {
    if (!disk) return;
    const list = document.getElementById('disk-list');
    list.innerHTML = '';
    (disk.partitions || []).forEach(p => {
        const item = document.createElement('div');
        item.className = 'disk-item';
        item.innerHTML = `
            <span><b>${escapeHtml(p.mountpoint)}</b> (${escapeHtml(p.fstype)})</span>
            <span><b>${p.percent.toFixed(1)}%</b> (${formatBytes(p.used)} / ${formatBytes(p.total)})</span>
        `;
        list.appendChild(item);
    });
    
    const rootPart = disk.partitions?.[0];
    document.getElementById('disk-percent').textContent = rootPart ? `${rootPart.percent.toFixed(1)}%` : '0%';
    document.getElementById('disk-read-speed').textContent = formatSpeed(disk.read_bytes_sec);
    document.getElementById('disk-write-speed').textContent = formatSpeed(disk.write_bytes_sec);
}

function updateNetwork(net) {
    if (!net) return;
    document.getElementById('net-conn').textContent = net.connections_count || 0;
    document.getElementById('net-rx-speed').textContent = formatSpeed(net.rx_bytes_sec);
    document.getElementById('net-tx-speed').textContent = formatSpeed(net.tx_bytes_sec);

    const list = document.getElementById('net-list');
    list.innerHTML = '';
    let totalErrors = 0;
    for (const [name, stats] of Object.entries(net.interfaces || {})) {
        if (name === 'lo') continue;
        const item = document.createElement('div');
        item.className = 'net-item';
        // Per-interface detail: RX/TX bytes plus cumulative errors & drops.
        const errin = stats.errin || 0, errout = stats.errout || 0, dropin = stats.dropin || 0, dropout = stats.dropout || 0;
        totalErrors += errin + errout + dropin + dropout;
        const detail = [errin + errout, dropin + dropout].some(v => v > 0)
            ? ` · <span class="net-warn">err ${errin + errout} · drop ${dropin + dropout}</span>`
            : '';
        item.innerHTML = `
            <span><b>${escapeHtml(name)}</b><small>${formatBytes(stats.bytes_recv)} ↓ / ${formatBytes(stats.bytes_sent)} ↑ ${detail}</small></span>
            <span>${formatBytes(stats.bytes_recv)} / ${formatBytes(stats.bytes_sent)}</span>
        `;
        list.appendChild(item);
    }
    const errEl = document.getElementById('net-errors');
    if (errEl) errEl.textContent = totalErrors;
}

function updateThermal(thermal) {
    const content = document.getElementById('thermal-content');
    const peakEl = document.getElementById('thermal-peak');
    const countEl = document.getElementById('thermal-count');
    const fansEl = document.getElementById('thermal-fans');
    if (!content) return;
    if (!thermal || !thermal.available) {
        content.innerHTML = '<p class="no-data">No temperature sensors exposed by this host.</p>';
        if (peakEl) peakEl.textContent = 'N/A';
        if (countEl) countEl.textContent = '0';
        if (fansEl) fansEl.textContent = '0';
        return;
    }
    const temps = thermal.temperatures || [];
    const fans = thermal.fans || [];
    if (countEl) countEl.textContent = temps.length;
    if (fansEl) fansEl.textContent = fans.length;

    // Peak sensor headline, colored by severity.
    if (peakEl) {
        if (thermal.peak_c != null) {
            peakEl.textContent = thermal.peak_c.toFixed(0) + '°C';
            peakEl.style.webkitTextFillColor =
                thermal.status === 'critical' ? 'var(--danger)'
                : thermal.status === 'warning' ? 'var(--warning)'
                : '';
        } else {
            peakEl.textContent = 'N/A';
        }
    }

    let html = '';
    // Sensors that actually report a current temperature.
    const measured = temps.filter(t => t.current_c != null);
    if (measured.length) {
        html += '<div class="thermal-list">';
        measured.forEach(t => {
            const cls = t.current_c >= 80 ? 'therm-critical' : t.current_c >= 70 ? 'therm-warning' : '';
            html += `<div class="thermal-item ${cls}"><span><b>${escapeHtml(t.name)}</b></span><span>${t.current_c.toFixed(1)}°C${t.high_c != null ? ' / ' + t.high_c.toFixed(0) + ' max' : ''}</span></div>`;
        });
        html += '</div>';
    } else {
        html += '<p class="no-data">Sensors present but not reporting current values.</p>';
    }
    if (fans.length) {
        html += '<div class="thermal-fans">' + fans.map(f => `<span><b>${escapeHtml(f.name)}</b> ${f.current_rpm} RPM</span>`).join('') + '</div>';
    }
    if (thermal.battery) {
        html += `<div class="thermal-battery">🔋 ${thermal.battery.percent}% ${thermal.battery.plugged ? '(on AC)' : '(on battery)'}</div>`;
    }
    content.innerHTML = html;
}

function updateGpu(gpus) {
    const content = document.getElementById('gpu-content');
    if (!gpus || gpus.length === 0) {
        content.innerHTML = '<p class="no-data">No NVIDIA GPU detected or NVML disabled</p>';
        document.getElementById('gpu-total').textContent = 'N/A';
        return;
    }

    let html = '<div class="gpu-grid">';
    gpus.forEach(gpu => {
        html += `
            <div class="gpu-item">
                <div class="gpu-item-header">
                    <span>${escapeHtml(gpu.name)}</span>
                    <span>${gpu.temperature}°C</span>
                </div>
                <div class="gpu-bars">
                    <div class="gpu-bar"><div class="gpu-bar-fill" style="width:${gpu.utilization_gpu}%"></div></div>
                    <div class="gpu-bar"><div class="gpu-bar-fill" style="width:${gpu.utilization_memory}%"></div></div>
                </div>
                <div class="gpu-stats">
                    <span>GPU: ${gpu.utilization_gpu}%</span>
                    <span>VRAM: ${gpu.utilization_memory}%</span>
                    <span>Power: ${gpu.power_draw}W / ${gpu.power_limit}W</span>
                </div>
            </div>`;
    });
    html += '</div>';
    content.innerHTML = html;

    const avgGpu = gpus.reduce((a, g) => a + g.utilization_gpu, 0) / gpus.length;
    document.getElementById('gpu-total').textContent = avgGpu.toFixed(1) + '%';
}

function updateSystem(sys) {
    if (!sys) return;
    const info = document.getElementById('system-info');
    info.innerHTML = `
        <span><span>Hostname:</span><b>${escapeHtml(sys.hostname)}</b></span>
        <span><span>OS Platform:</span><b>${escapeHtml(sys.platform)} ${escapeHtml(sys.platform_release)}</b></span>
        <span><span>Kernel Version:</span><b>${escapeHtml(sys.platform_version.substring(0, 20))}</b></span>
        <span><span>Architecture:</span><b>${escapeHtml(sys.architecture)}</b></span>
        <span><span>Uptime:</span><b>${escapeHtml(sys.uptime_str)}</b></span>
        <span><span>Boot Time:</span><b>${escapeHtml(sys.boot_time)}</b></span>
    `;
    document.getElementById('hostname').textContent = sys.hostname;
    document.getElementById('uptime').textContent = 'Uptime: ' + sys.uptime_str;
}

function updateTopProcesses(processes) {
    const tbody = document.getElementById('top-processes-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    (processes || []).slice(0, 10).forEach(p => {
        const row = document.createElement('tr');
        row.style.cursor = 'pointer';
        row.innerHTML = `
            <td><b>${p.pid}</b></td>
            <td>${escapeHtml(p.name)}</td>
            <td><b class="${p.cpu_percent > 50 ? 'text-danger' : ''}">${p.cpu_percent}%</b></td>
            <td>${p.memory_percent}%</td>
            <td>${p.memory_mb} MB</td>
            <td><span class="badge ${p.status === 'running' ? 'badge-success' : 'badge-warning'}">${escapeHtml(p.status)}</span></td>
            <td>${escapeHtml(p.username)}</td>
            <td>${p.threads || 1}</td>
        `;
        row.addEventListener('click', () => showProcessDetail(p.pid));
        tbody.appendChild(row);
    });
}

// Tracks alert messages the operator has manually dismissed via the "Clear Issues"
// button, so they stop re-appearing while the underlying condition persists but do
// come back once it clears and re-triggers.
const dismissedIssues = new Set();

function checkOSIssues(data) {
    const issues = [];

    const addIssue = (severity, message, action) => {
        issues.push({ severity, message, action });
    };

    if (data.cpu && data.cpu.percent_total > 85) {
        addIssue('critical', `Critical CPU load: ${data.cpu.percent_total.toFixed(1)}%`, {
            label: 'Open Bottlenecks',
            kind: 'subtab',
            target: 'bottlenecks'
        });
    } else if (data.cpu && data.cpu.percent_total > 70) {
        addIssue('warning', `Elevated CPU usage: ${data.cpu.percent_total.toFixed(1)}%`, {
            label: 'Investigate Processes',
            kind: 'tab',
            target: 'processes'
        });
    }

    if (data.memory && data.memory.percent > 90) {
        addIssue('critical', `RAM usage critically high: ${data.memory.percent}%`, {
            label: 'Fix Now: Clear RAM Cache',
            kind: 'remediate',
            action: 'clear_pagecache'
        });
    } else if (data.memory && data.memory.percent > 80) {
        addIssue('warning', `RAM usage elevated: ${data.memory.percent}%`, {
            label: 'Clear RAM Cache',
            kind: 'remediate',
            action: 'clear_pagecache'
        });
    }

    if (data.disk && data.disk.partitions) {
        data.disk.partitions.forEach(p => {
            if (p.percent > 90) {
                addIssue('critical', `Partition ${p.mountpoint} is nearly full: ${p.percent.toFixed(1)}%`, {
                    label: 'Fix Now: Vacuum Logs',
                    kind: 'remediate',
                    action: 'vacuum_journal'
                });
            } else if (p.percent > 80) {
                addIssue('warning', `Partition ${p.mountpoint} storage high: ${p.percent.toFixed(1)}%`, {
                    label: 'Vacuum Logs',
                    kind: 'remediate',
                    action: 'vacuum_journal'
                });
            }
        });
    }

    if (data.processes) {
        const zombies = data.processes.filter(p => p.status === 'zombie' || p.status === 'uninterruptible sleep');
        if (zombies.length > 0) {
            addIssue('warning', `${zombies.length} process(es) in zombie or disk-sleep state.`, {
                label: 'Open Stuck Processes',
                kind: 'subtab',
                target: 'bottlenecks'
            });
        }
    }

    // Forget dismissals for conditions that no longer exist so they can re-trigger.
    const activeKeys = new Set(issues.map(i => i.message));
    for (const key of Array.from(dismissedIssues)) {
        if (!activeKeys.has(key)) dismissedIssues.delete(key);
    }

    // Drop any alerts the operator dismissed; keep the rest visible.
    const visibleIssues = issues.filter(i => !dismissedIssues.has(i.message));

    const criticalCount = visibleIssues.filter(i => i.severity === 'critical').length;
    const warningCount = visibleIssues.filter(i => i.severity === 'warning').length;
    document.getElementById('issues-count-critical').textContent = `${criticalCount} Critical`;
    document.getElementById('issues-count-warning').textContent = `${warningCount} Warnings`;

    const list = document.getElementById('issues-list');
    list.innerHTML = '';

    if (visibleIssues.length === 0) {
        list.innerHTML = '<div class="issue-item success">✓ All core system monitors report healthy status.</div>';
        return;
    }

    visibleIssues.forEach(issue => {
        list.appendChild(createDashboardIssueItem(issue));
    });
}

// Dismiss every currently-visible alert. Dismissed alerts stay hidden until their
// underlying condition clears and later re-triggers.
function clearIssues() {
    document.querySelectorAll('#issues-list .issue-item').forEach(item => {
        const message = item.dataset.message;
        if (message) dismissedIssues.add(message);
    });
    // Re-render immediately from the last known dashboard snapshot.
    if (statsData) checkOSIssues(statsData);
    showToast('Current alerts dismissed', 'info');
}

function createDashboardIssueItem(issue) {
    const item = document.createElement('div');
    const isCritical = issue.severity === 'critical';
    item.className = `issue-item ${isCritical ? 'danger' : 'warning'}`;
    item.dataset.message = issue.message;

    const msg = document.createElement('span');
    msg.innerHTML = `${isCritical ? '🚨 <b>CRITICAL:</b>' : '⚠️ <b>WARNING:</b>'} ${escapeHtml(issue.message)}`;
    item.appendChild(msg);

    if (!issue.action) return item;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = `btn btn-sm ${isCritical ? 'btn-danger' : 'btn-warning'}`;
    button.textContent = issue.action.label || (isCritical ? 'Fix Now' : 'Investigate');
    button.addEventListener('click', () => runDashboardIssueAction(issue.action));
    item.appendChild(button);

    return item;
}

function runDashboardIssueAction(action) {
    if (!action) return;
    if (action.kind === 'remediate') {
        remediateAction(action.action, action.target || null);
    } else if (action.kind === 'tab') {
        switchTab(action.target);
    } else if (action.kind === 'subtab') {
        switchToTroubleshoot();
        switchSubTab(action.target);
    }
}

/* Canvas Sparklines */
function updateCharts(data) {
    if (!data) return;

    // Push new values
    historyBuffer.cpu.shift();
    historyBuffer.cpu.push(data.cpu?.percent_total || 0);

    historyBuffer.mem.shift();
    historyBuffer.mem.push(data.memory?.percent || 0);

    historyBuffer.netRx.shift();
    historyBuffer.netRx.push((data.network?.rx_bytes_sec || 0) / 1024); // KB/s

    historyBuffer.netTx.shift();
    historyBuffer.netTx.push((data.network?.tx_bytes_sec || 0) / 1024); // KB/s

    document.getElementById('cpu-chart-val').textContent = `${Number(data.cpu?.percent_total || 0).toFixed(1)}%`;
    document.getElementById('mem-chart-val').textContent = `${data.memory?.percent}%`;
    document.getElementById('net-chart-val').textContent = `↓ ${formatSpeed(data.network?.rx_bytes_sec)} | ↑ ${formatSpeed(data.network?.tx_bytes_sec)}`;

    drawSparkline('cpu-canvas', historyBuffer.cpu, '#3b82f6', 100);
    drawSparkline('mem-canvas', historyBuffer.mem, '#22c55e', 100);
    drawDoubleSparkline('net-canvas', historyBuffer.netRx, historyBuffer.netTx, '#22c55e', '#3b82f6');
}

function drawSparkline(canvasId, data, color, maxVal = 100) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width = canvas.parentElement.clientWidth || 300;
    const height = canvas.height = 60;

    ctx.clearRect(0, 0, width, height);
    if (data.length < 2) return;

    const step = width / (data.length - 1);
    ctx.beginPath();
    ctx.moveTo(0, height - (data[0] / maxVal) * height);

    for (let i = 1; i < data.length; i++) {
        const x = i * step;
        const y = height - (data[i] / maxVal) * height;
        ctx.lineTo(x, Math.max(2, Math.min(height - 2, y)));
    }

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Fill gradient
    ctx.lineTo(width, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    ctx.fillStyle = color + '22';
    ctx.fill();
}

function drawDoubleSparkline(canvasId, data1, data2, color1, color2) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width = canvas.parentElement.clientWidth || 300;
    const height = canvas.height = 60;

    ctx.clearRect(0, 0, width, height);

    const maxVal = Math.max(...data1, ...data2, 10); // Auto scale min 10 KB/s
    const step = width / (data1.length - 1);

    // Line 1
    ctx.beginPath();
    ctx.moveTo(0, height - (data1[0] / maxVal) * height);
    for (let i = 1; i < data1.length; i++) {
        ctx.lineTo(i * step, height - (data1[i] / maxVal) * height);
    }
    ctx.strokeStyle = color1;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Line 2
    ctx.beginPath();
    ctx.moveTo(0, height - (data2[0] / maxVal) * height);
    for (let i = 1; i < data2.length; i++) {
        ctx.lineTo(i * step, height - (data2[i] / maxVal) * height);
    }
    ctx.strokeStyle = color2;
    ctx.lineWidth = 2;
    ctx.stroke();
}

function updateLastUpdate() {
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
}

/* Modal for Process Inspector */
async function showProcessDetail(pid) {
    try {
        const res = await fetch(`${API_BASE}/api/processes/${pid}`);
        if (!res.ok) throw new Error('Process terminated or non-existent');
        const proc = await res.json();

        const modal = document.getElementById('process-modal');
        const body = document.getElementById('modal-body');
        body.innerHTML = `
            <div class="system-info" style="margin-bottom:14px;">
                <span><span>Process PID:</span><b>${proc.pid}</b></span>
                <span><span>Process Name:</span><b>${escapeHtml(proc.name)}</b></span>
                <span><span>Execution State:</span><b>${escapeHtml(proc.status)}</b></span>
                <span><span>User Owner:</span><b>${escapeHtml(proc.username)}</b></span>
                <span><span>CPU Usage:</span><b>${proc.cpu_percent}%</b></span>
                <span><span>RAM Usage:</span><b>${proc.memory_percent}% (${proc.memory_info?.rss ? (proc.memory_info.rss/1024/1024).toFixed(1) : 0} MB)</b></span>
                <span><span>Threads Count:</span><b>${proc.num_threads}</b></span>
                <span><span>Open File Descriptors:</span><b>${proc.num_fds}</b></span>
                <span><span>Launch Time:</span><b>${escapeHtml(proc.create_time)}</b></span>
            </div>
            <h4 style="margin-bottom:6px;font-size:0.85rem;color:var(--text-secondary);">Command Line Command</h4>
            <pre style="background:var(--bg-primary);padding:10px;border-radius:6px;font-size:0.8rem;border:1px solid var(--border);">${escapeHtml((proc.cmdline || []).join(' ') || proc.exe || proc.name)}</pre>
            <h4 style="margin:12px 0 6px;font-size:0.85rem;color:var(--text-secondary);">Open File Handles (${proc.open_files.length})</h4>
            <pre style="background:var(--bg-primary);padding:10px;border-radius:6px;font-size:0.75rem;max-height:140px;overflow-y:auto;border:1px solid var(--border);">${escapeHtml((proc.open_files || []).map(f => f.path || '').join('\n') || 'None reported')}</pre>
            <h4 style="margin:12px 0 6px;font-size:0.85rem;color:var(--text-secondary);">Active Socket Connections (${proc.connections.length})</h4>
            <pre style="background:var(--bg-primary);padding:10px;border-radius:6px;font-size:0.75rem;max-height:140px;overflow-y:auto;border:1px solid var(--border);">${escapeHtml((proc.connections || []).map(c => `${c.status || 'CONNECTED'} ${c.laddr ? c.laddr.ip + ':' + c.laddr.port : ''} -> ${c.raddr ? c.raddr.ip + ':' + c.raddr.port : ''}`).join('\n') || 'No active sockets')}</pre>
            <div style="margin-top:16px;display:flex;justify-content:flex-end;">
                <button class="btn btn-danger" onclick="killProcess(${proc.pid})">💀 Terminate Process</button>
            </div>
        `;
        modal.classList.add('show');
    } catch (e) {
        showToast('Error loading process details: ' + e.message, 'error');
    }
}

async function killProcess(pid, signal = 15) {
    if (!confirm(`Are you sure you want to terminate PID ${pid}?`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/processes/${pid}/kill?signal=${signal}`, {
            method: 'POST'
        });
        if (res.ok) {
            showToast(`Process ${pid} terminated successfully`, 'success');
            document.getElementById('process-modal')?.classList.remove('show');
            fetchStats();
        } else {
            // Surface the server's reason (ownership guard 403, not found, …)
            showToast(`Failed to terminate process: ${await readApiError(res)}`, 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* Multi-select (bulk) kill: one confirmation, one batch request, per-PID
   results. The backend applies its ownership guard per process, so
   foreign-owned PIDs are individually skipped and reported while authorized
   PIDs are still terminated. */
async function killSelectedProcesses(pids) {
    const btn = document.getElementById('kill-selected');
    if (!pids.length) return;
    if (!confirm(`Kill ${pids.length} selected process(es)?`)) return;
    if (btn) { btn.disabled = true; btn.textContent = 'Terminating…'; }
    try {
        const res = await fetch(`${API_BASE}/api/processes/kill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pids, signal: 15 })
        });
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();
        const results = data.results || [];
        const killed = results.filter(r => r.success);
        const refused = results.filter(r => !r.success);
        if (!results.length) {
            showToast('Nothing to kill: no valid process selected.', 'warning');
        } else if (refused.length === 0) {
            showToast(`Killed ${killed.length} of ${results.length} process(es).`, 'success');
        } else {
            const reasons = refused.slice(0, 3)
                .map(r => `PID ${r.pid}: ${r.message || 'refused'}`);
            const more = refused.length > 3 ? ` +${refused.length - 3} more` : '';
            showToast(
                `Killed ${killed.length}, skipped ${refused.length} (${reasons.join('; ')}${more})`,
                refused.length === results.length ? 'error' : 'warning'
            );
        }
        fetchStats();
    } catch (e) {
        showToast('Bulk kill failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '💀 Terminate Selected'; }
    }
}

async function fetchStats() {
    try {
        const res = await fetch(`${API_BASE}/api/stats`);
        const data = await res.json();
        statsData = data;
        updateDashboard(data);
        updateLastUpdate();
    } catch (e) {
        console.error('Fetch error:', e);
    }
}

/* ==========================================================================
   TROUBLESHOOT MODE IMPLEMENTATION
   ========================================================================== */

function switchToTroubleshoot() {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    const btn = document.querySelector('[data-tab="troubleshoot"]');
    if (btn) btn.classList.add('active');
    document.getElementById('tab-troubleshoot').classList.add('active');
    state.currentTab = 'troubleshoot';
    runFullHealthScan();
}

async function runFullHealthScan() {
    const btn = document.getElementById('run-full-scan-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Scanning System...';

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/health-check`);
        if (!res.ok) throw new Error('Health check failed');
        healthData = await res.json();

        updateHealthGauge(healthData.health_score);
        renderHealthChecks(healthData.checks);

        document.getElementById('pill-critical').textContent = `${healthData.summary.critical} Critical`;
        document.getElementById('pill-warning').textContent = `${healthData.summary.warning} Warnings`;
        document.getElementById('pill-ok').textContent = `${healthData.summary.ok} Passing`;

        // Headline is driven by the ACTUAL critical/warning counts, never by a
        // raw score threshold — so we never claim "Critical issues detected"
        // when there are zero critical checks (e.g. several warnings dragging
        // the score to <=65, which previously printed the false alarm).
        document.getElementById('health-summary-text').textContent = healthSummaryText(healthData);

        document.getElementById('last-scan-time').textContent = `Last Scan: ${new Date().toLocaleTimeString()}`;

        // persist last scan for quick restore on tab switch
        try { localStorage.setItem('monitorx-last-health', JSON.stringify(healthData)); } catch (_) {}

        renderHealthTrend();
        refreshFixEngine();
        loadFixHistory();
        if (!fixCapabilities) loadFixCapabilities();
        showToast('Diagnostic scan complete', 'info');
    } catch (e) {
        showToast('Error running health scan: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '⚡ Run Diagnostic Scan';
    }
}

/* Fetch and draw the rolling System Health Index trend sparkline. */
async function renderHealthTrend() {
    const canvas = document.getElementById('health-trend-chart');
    const label = document.getElementById('health-trend-label');
    if (!canvas || !label) return;

    let history = [];
    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/history?limit=40`);
        if (res.ok) history = ((await res.json()).history) || [];
    } catch (e) {
        history = [];
    }

    // Always include the current score so the sparkline updates on every scan.
    if (healthData && history.length && healthData.health_score !== undefined) {
        const last = history[history.length - 1];
        if (!last || last.score !== healthData.health_score) {
            history.push({ score: healthData.health_score });
        }
    }

    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    if (history.length < 2) {
        label.textContent = 'Trend: run more scans to chart';
        ctx.fillStyle = 'rgba(148,163,184,0.7)';
        ctx.font = '10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('—', W / 2, H / 2 + 4);
        return;
    }

    const scores = history.map(h => h.score);
    const min = Math.min(...scores), max = Math.max(...scores);
    const pad = 4, bottom = H - pad, top = pad;
    const range = (max - min) || 1;
    const x = i => pad + (i * (W - 2 * pad)) / (scores.length - 1);
    const y = s => bottom - ((s - min) / range) * (bottom - top);

    // Grid line at 100 (perfect score).
    ctx.strokeStyle = 'rgba(148,163,184,0.18)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad, y(100));
    ctx.lineTo(W - pad, y(100));
    ctx.stroke();

    // Area fill under the line.
    const grad = ctx.createLinearGradient(0, top, 0, bottom);
    grad.addColorStop(0, 'rgba(56,189,248,0.30)');
    grad.addColorStop(1, 'rgba(56,189,248,0.02)');
    ctx.beginPath();
    scores.forEach((s, i) => i ? ctx.lineTo(x(i), y(s)) : ctx.moveTo(x(i), y(s)));
    ctx.lineTo(x(scores.length - 1), bottom);
    ctx.lineTo(x(0), bottom);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line.
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 2;
    ctx.beginPath();
    scores.forEach((s, i) => i ? ctx.lineTo(x(i), y(s)) : ctx.moveTo(x(i), y(s)));
    ctx.stroke();

    // Points colored by the severity of the scan they came from.
    history.forEach((h, i) => {
        let color = '#38bdf8';
        if (h.critical > 0) color = '#ef4444';
        else if (h.warning > 0) color = '#f59e0b';
        ctx.beginPath();
        ctx.arc(x(i), y(h.score), i === history.length - 1 ? 3 : 2, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
    });

    // Direction + magnitude caption.
    const prev = scores[scores.length - 2], curr = scores[scores.length - 1];
    const delta = curr - prev;
    const arrow = delta > 0 ? '▲' : delta < 0 ? '▼' : '—';
    const tone = delta > 0 ? 'var(--success)' : delta < 0 ? 'var(--danger)' : 'var(--text-dim)';
    label.textContent = `Trend: ${arrow} ${delta > 0 ? '+' : ''}${delta} (last ${scores.length} scans)`;
    label.style.color = tone;
}

/* ==========================================================================
   AUTO-FIX ENGINE (Troubleshoot Hub)
   ========================================================================== */

/* Collect every fixable issue from the latest scan into a deduplicated plan. */
function buildFixPlan() {
    const plan = [];
    const seen = new Set();
    if (!healthData || !healthData.checks) return plan;
    healthData.checks.forEach(c => {
        const fixes = (c.fixes && c.fixes.length) ? c.fixes : (c.fix ? [c.fix] : []);
        fixes.forEach(f => {
            if (!f || !f.action) return;
            const key = `${f.action}|${f.target || ''}`;
            if (seen.has(key)) return;
            seen.add(key);
            plan.push({ ...f, checkName: c.name });
        });
    });
    return plan;
}

/* Update the Fix Engine header (count badge, buttons, subtitle). */
function refreshFixEngine() {
    currentFixPlan = buildFixPlan();
    const count = currentFixPlan.length;
    const allBtn = document.getElementById('fix-all-btn');
    const reviewBtn = document.getElementById('fix-review-btn');
    const badge = document.getElementById('fix-summary-badge');
    const sub = document.getElementById('fix-engine-sub');
    if (!allBtn || !badge) return;

    allBtn.disabled = count === 0 || fixRunning;
    reviewBtn.disabled = count === 0 || fixRunning;
    allBtn.textContent = count > 0 ? `⚡ Fix All Issues (${count})` : '⚡ Fix All Issues';
    badge.textContent = fixRunning ? 'Fixing…' : (count === 0 ? '✅ No fixes needed' : `${count} fix${count > 1 ? 'es' : ''} available`);
    badge.className = 'fix-summary-badge ' + (fixRunning ? 'running' : (count === 0 ? 'ok' : 'warn'));
    sub.textContent = count === 0
        ? 'The last scan found no fixable issues. Run a scan anytime to rebuild the repair plan.'
        : 'Every issue detected below can be repaired directly from this hub — one click or one button per issue.';
}

/* Render a single fix execution, returning {success, message, label}. */
async function executeFix(action, target = null) {
    const res = await fetch(`${API_BASE}/api/troubleshoot/remediate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, target })
    });
    return await res.json();
}

/* One-button fix for a single check card (called from inline onclick). */
async function remediateAction(action, target = null, opts = {}) {
    if (fixRunning) return null;
    const btn = document.querySelector(`.check-card button[data-action="${attrSel(action)}"][data-target="${attrSel(target || '')}"]`);
    const card = btn?.closest('.check-card');
    const resultBoxId = card ? `${card.id || 'check'}-result` : null;

    if (btn) {
        btn.disabled = true;
        btn.classList.add('running');
        btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
        btn.textContent = 'Running…';
    }
    const removeResult = () => {
        if (resultBoxId) document.getElementById(resultBoxId)?.remove();
    };
    removeResult();
    if (!opts.silent) {
        showToast(`Executing fix: ${action}${target ? ' → ' + target : ''}`, 'info');
    }

    try {
        const result = await executeFix(action, target);
        if (result.success) {
            showToast(`✓ ${result.message}`, 'success');
        } else {
            showToast(`✗ ${result.message || 'Fix failed'}`, 'error');
        }
        if (card && resultBoxId) {
            const box = document.createElement('div');
            box.id = resultBoxId;
            box.className = `fix-card-result ${result.success ? 'success' : 'failed'}`;
            box.textContent = `${result.success ? '✓' : '✗'} ${result.message || 'No output'}`;
            card.appendChild(box);
            setTimeout(() => { box.style.opacity = '0.35'; }, 15000);
        }
        // Re-scan + refresh history + rebuild the plan after every fix.
        await refreshAfterFix();
        return result;
    } catch (e) {
        showToast('Remediation error: ' + e.message, 'error');
        if (card && resultBoxId) {
            const box = document.createElement('div');
            box.id = resultBoxId;
            box.className = 'fix-card-result failed';
            box.textContent = `✗ ${e.message}`;
            card.appendChild(box);
        }
        return { success: false, message: e.message };
    } finally {
        if (btn) {
            btn.classList.remove('running');
            // refreshAfterFix re-renders the checks; restore only if still detached.
            if (!document.body.contains(btn)) return;
            btn.disabled = false;
            btn.textContent = btn.dataset.originalLabel || 'Fix';
        }
    }
}

async function refreshAfterFix() {
    if (state.currentTab === 'troubleshoot') {
        loadFixHistory();
        await runFullHealthScan();
    } else {
        fetchStats();
    }
}

/* Batch fix-all runner with live per-item progress. */
async function runFixAll(plan = null) {
    const items = plan || buildFixPlan();
    if (!items.length || fixRunning) return;
    fixRunning = true;
    refreshFixEngine();
    document.getElementById('fix-review-btn').disabled = true;
    renderFixRunArea(items);

    const results = [];
    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        setFixRunRow(i, 'running', 'Executing…');
        let result;
        try {
            result = await executeFix(item.action, item.target || null);
        } catch (e) {
            result = { success: false, message: e.message };
        }
        results.push({ ...item, ...result });
        setFixRunRow(i, result.success ? 'success' : 'failed',
            `${result.success ? '✓' : '✗'} ${result.message || 'no output'}`);
    }

    const ok = results.filter(r => r.success).length;
    const summary = document.createElement('div');
    summary.className = `fix-run-summary ${ok === results.length ? '' : 'partial'}`;
    summary.textContent = ok === results.length
        ? `✅ ALL FIXES APPLIED — ${ok}/${results.length} succeeded. Re-running scan…`
        : `⚠️ ${ok}/${results.length} fixes applied; ${results.length - ok} failed. Re-running scan…`;
    document.getElementById('fix-run-area').appendChild(summary);

    fixRunning = false;
    refreshFixEngine();
    // Re-enable review button explicitly (refreshFixEngine only does it when count>0)
    const reviewBtn = document.getElementById('fix-review-btn');
    if (reviewBtn) reviewBtn.disabled = currentFixPlan.length === 0 || fixRunning;
    await refreshAfterFix();
}

function renderFixRunArea(items) {
    const area = document.getElementById('fix-run-area');
    area.hidden = false;
    area.innerHTML = '';
    items.forEach((item, i) => {
        const meta = fixMeta(item.action);
        const row = document.createElement('div');
        row.id = `fix-run-${i}`;
        row.className = 'fix-run-row pending';
        row.innerHTML = `
            <span class="fix-run-icon">⏳</span>
            <span class="fix-run-label">${escapeHtml(item.label || meta.label)}</span>
            <span class="fix-run-msg">Queued…</span>
        `;
        area.appendChild(row);
    });
}

function setFixRunRow(i, status, message) {
    const row = document.getElementById(`fix-run-${i}`);
    if (!row) return;
    row.className = `fix-run-row ${status}`;
    const icon = row.querySelector('.fix-run-icon');
    icon.textContent = status === 'success' ? '✅' : status === 'failed' ? '❌' : '⏳';
    icon.style.animation = 'none';
    const msg = row.querySelector('.fix-run-msg');
    if (msg) msg.textContent = message || '';
}

/* Review plan modal */
function openFixPlanModal() {
    const plan = buildFixPlan();
    if (!plan.length) return;
    const list = document.getElementById('fix-plan-list');
    list.innerHTML = '';
    plan.forEach((item, i) => {
        const meta = fixMeta(item.action);
        const available = fixActionAvailable(item.action);
        const div = document.createElement('div');
        div.className = 'fix-plan-item' + (available ? '' : ' unavailable');
        div.innerHTML = `
            <input type="checkbox" class="fix-plan-check" data-plan-idx="${i}" ${available ? 'checked' : 'disabled'} ${available ? '' : 'title="Not available in this environment"'}>
            <div class="fix-plan-body">
                <div class="fix-plan-title-row">
                    <span class="fix-plan-label">${escapeHtml(item.label || meta.label)}</span>
                    ${item.target ? `<span class="fix-plan-target">${escapeHtml(item.target)}</span>` : ''}
                    <span class="fix-plan-level ${escapeHtml(meta.level)}">${escapeHtml(meta.level.toUpperCase())}</span>
                </div>
                <div class="fix-plan-desc">${escapeHtml(item.description || meta.description || item.checkName || '')}</div>
                ${available ? '' : '<div class="fix-unavailable-note">⚠️ Not executable in this environment (missing tooling or permissions).</div>'}
            </div>
        `;
        list.appendChild(div);
    });
    document.getElementById('fix-plan-modal').classList.add('show');
}

function closeFixPlanModal() {
    document.getElementById('fix-plan-modal').classList.remove('show');
}

async function applySelectedFixes() {
    const checked = Array.from(document.querySelectorAll('.fix-plan-check:checked'));
    if (!checked.length) {
        showToast('No fixes selected', 'info');
        return;
    }
    const plan = buildFixPlan();
    const selected = checked.map(cb => plan[Number(cb.dataset.planIdx)]).filter(Boolean);
    closeFixPlanModal();
    await runFixAll(selected);
}

/* Fix history */
async function loadFixHistory() {
    const list = document.getElementById('fix-history-list');
    if (!list) return;
    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/fix-history?limit=15`);
        if (!res.ok) throw new Error('history fetch failed');
        const data = await res.json();
        const entries = data.entries || [];
        if (!entries.length) {
            list.innerHTML = '<p class="no-data">No automated fixes executed yet. Fixes run here are recorded automatically.</p>';
            return;
        }
        list.innerHTML = '';
        const wrap = document.createElement('div');
        wrap.className = 'fix-history-list';
        entries.forEach(e => {
            const action = String(e.action || '').replace(/^remediate:/, '');
            const item = document.createElement('div');
            item.className = 'fix-history-item';
            item.innerHTML = `
                <span class="fix-history-time">${escapeHtml((e.timestamp || '').slice(0, 19).replace('T', ' '))}</span>
                <span class="fix-history-action">${escapeHtml(fixMeta(action).label || action)}</span>
                ${e.target ? `<span class="fix-history-target">${escapeHtml(e.target)}</span>` : ''}
                <span class="fix-history-outcome ${e.outcome === 'success' ? 'success' : 'failed'}">${escapeHtml(e.outcome || '?')}</span>
                <span class="fix-history-detail" title="${escapeAttr(e.detail || '')}">${escapeHtml(e.detail || '')}</span>
            `;
            wrap.appendChild(item);
        });
        list.appendChild(wrap);
    } catch (e) {
        list.innerHTML = `<p class="no-data">History unavailable: ${escapeHtml(e.message)}</p>`;
    }
}

/* Fix capabilities (which fixes this environment can actually run) */
async function loadFixCapabilities() {
    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/fix-capabilities`);
        if (!res.ok) return;
        fixCapabilities = await res.json();
        // Re-render check cards so unavailable fixes grey out.
        if (healthData && healthData.checks) renderHealthChecks(healthData.checks);
    } catch (e) {
        console.error('fix capabilities fetch failed:', e);
    }
}

/* Severity-accurate headline for the System Health Index.
   Driven by the ACTUAL critical/warning counts, never by a raw score
   threshold — so the banner can never contradict the pills (e.g. "0 Critical"
   next to a "Critical issues detected" alarm). */
function healthSummaryText(healthData) {
    const summary = (healthData && healthData.summary) || { critical: 0, warning: 0, ok: 0 };
    if (summary.critical > 0) {
        const plural = summary.critical === 1 ? 'issue' : 'issues';
        return `${summary.critical} critical ${plural} detected — immediate remediation required.`;
    }
    if (summary.warning > 0) {
        const plural = summary.warning === 1 ? '' : 's';
        return `${summary.warning} warning${plural} need attention. System Health Index: ${healthData.health_score}.`;
    }
    return 'All system checks passing — no issues detected.';
}

function updateHealthGauge(score) {
    const valText = document.getElementById('health-score-val');
    valText.textContent = score;

    const circle = document.getElementById('health-circle');
    circle.setAttribute('stroke-dasharray', `${score}, 100`);

    // Color the ring by real severity (not the numeric score) so a low score
    // caused only by warnings is orange, and red is reserved for actual
    // critical checks. The numeric score is still shown in the center.
    const summary = (healthData && healthData.summary) || { critical: 0, warning: 0, ok: 0 };
    if (summary.critical > 0) circle.style.stroke = 'var(--danger)';
    else if (summary.warning > 0) circle.style.stroke = 'var(--warning)';
    else circle.style.stroke = 'var(--success)';
}

function renderHealthChecks(checks) {
    const container = document.getElementById('checks-grid-container');
    container.innerHTML = '';

    checks.forEach((c, idx) => {
        const card = document.createElement('div');
        card.className = 'check-card';
        card.id = `check-card-${c.id || idx}`;

        const statusBadgeClass =
            c.status === 'critical' ? 'badge-danger' :
            c.status === 'warning' ? 'badge-warning' : 'badge-success';

        const statusIcon =
            c.status === 'critical' ? '🔴 CRITICAL' :
            c.status === 'warning' ? '⚠️ WARNING' : '✅ PASS';

        // Auto-Fix Engine: one button per fixable action on this check.
        const fixList = (c.fixes && c.fixes.length) ? c.fixes : (c.fix ? [c.fix] : []);
        let fixButtonHtml = '';
        fixList.forEach(f => {
            const meta = fixMeta(f.action);
            const available = fixActionAvailable(f.action);
            const levelClass = f.level || meta.level || 'info';
            const btnClass = levelClass === 'critical' ? 'critical' : levelClass === 'warning' ? 'warning' : 'info';
            const label = f.label || meta.label;
            const title = available
                ? (f.description || meta.description || '')
                : 'Not available: missing tooling or permissions in this environment';
            fixButtonHtml += `
                <button type="button" class="btn-fix ${btnClass}" data-action="${escapeAttr(f.action)}"
                        data-target="${escapeAttr(f.target || '')}" onclick="remediateAction('${escapeAttr(f.action)}','${escapeAttr(f.target || '')}')"
                        ${available ? '' : 'disabled'} title="${escapeAttr(title)}">${escapeHtml(label)}</button>
            `;
        });

        // Fallback navigation buttons (non-fixable checks still lead somewhere useful).
        const legacyAction = c.action;
        let navHtml = '';
        if (!fixList.length && legacyAction === 'view_bottlenecks') {
            navHtml = `<button class="btn btn-sm btn-primary" onclick="switchSubTab('bottlenecks')">🔥 Open Bottleneck Finder</button>`;
        } else if (!fixList.length && legacyAction === 'view_logs') {
            navHtml = `<button class="btn btn-sm btn-primary" onclick="switchSubTab('log-inspector')">📋 Inspect Logs</button>`;
        } else if (!fixList.length && legacyAction === 'run_net_diag') {
            navHtml = `<button class="btn btn-sm btn-primary" onclick="switchSubTab('net-suite')">🌐 Open Network Suite</button>`;
        } else if (!fixList.length && legacyAction === 'view_processes') {
            navHtml = `<button class="btn btn-sm btn-primary" onclick="switchTab('processes')">📋 Open Process Manager</button>`;
        }

        card.innerHTML = `
            <div>
                <div class="check-card-header">
                    <span class="check-card-title">${escapeHtml(c.category)}: ${escapeHtml(c.name)}</span>
                    <span class="badge ${statusBadgeClass}">${statusIcon}</span>
                </div>
                <div class="check-card-val">${escapeHtml(c.value)}</div>
                <div class="check-card-msg">${escapeHtml(c.message)}</div>
                ${c.remediation ? `<div class="check-card-msg recommended-fix" style="color:var(--text-dim);font-size:0.8rem;margin-top:10px;padding-top:10px;border-top:1px dashed rgba(255,255,255,0.15);">💡 <strong>Recommended Fix:</strong> ${escapeHtml(c.remediation)}</div>` : ''}
            </div>
            <div class="check-card-footer">
                ${fixButtonHtml}
                ${navHtml}
            </div>
        `;
        container.appendChild(card);
    });
}

/* Log Inspector */
async function fetchLogs() {
    const container = document.getElementById('logs-container');
    const level = state.logLevel;
    const lines = state.logLines;
    const search = document.getElementById('log-search-input').value;

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/logs?lines=${lines}&level=${level}&search=${encodeURIComponent(search)}`);
        if (!res.ok) throw new Error('Failed to fetch system logs');
        const data = await res.json();

        container.innerHTML = '';
        if (!data.logs || data.logs.length === 0) {
            container.innerHTML = '<p class="no-data">No log entries matching criteria.</p>';
            return;
        }

        data.logs.forEach(l => {
            const line = document.createElement('div');
            line.className = `log-line log-${l.level}`;
            line.textContent = l.text;
            container.appendChild(line);
        });

        if (state.logAutoTail) {
            container.scrollTop = container.scrollHeight;
        }
    } catch (e) {
        container.innerHTML = `<p class="issue-item danger">Error: ${escapeHtml(e.message)}</p>`;
    }
}

/* Network Suite Tools */
async function runPingTest() {
    const host = document.getElementById('ping-host-input').value.trim();
    const resultsBox = document.getElementById('ping-results-box');
    if (!host) return;

    resultsBox.innerHTML = '<p class="text-muted">Pinging ' + escapeHtml(host) + '...</p>';
    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/ping`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host, count: 4 })
        });
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();

        if (data.success) {
            resultsBox.innerHTML = `
                <div style="color:var(--success);margin-bottom:6px;"><b>✓ Ping Successful:</b> ${data.packet_loss_percent}% loss</div>
                <div><b>Latency:</b> Min ${data.min_rtt}ms | Avg ${data.avg_rtt}ms | Max ${data.max_rtt}ms</div>
                <pre style="margin-top:8px;font-size:0.75rem;background:var(--bg-secondary);padding:6px;border-radius:4px;">${escapeHtml(data.raw_output)}</pre>
            `;
        } else {
            resultsBox.innerHTML = `<div style="color:var(--danger)"><b>✗ Ping Failed to ${escapeHtml(host)}</b></div><pre style="font-size:0.75rem">${escapeHtml(data.raw_output || data.error)}</pre>`;
        }
    } catch (e) {
        resultsBox.innerHTML = `<p style="color:var(--danger)">Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function runPortTest() {
    const host = document.getElementById('port-host-input').value.trim();
    const port = parseInt(document.getElementById('port-num-input').value);
    const resultsBox = document.getElementById('port-results-box');

    if (!host || !port) return;
    resultsBox.innerHTML = '<p class="text-muted">Testing connection to ' + escapeHtml(host) + ':' + port + '...</p>';

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/port-check`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host, port })
        });
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();

        if (data.open) {
            resultsBox.innerHTML = `<div style="color:var(--success)"><b>✓ OPEN:</b> Port ${port} on ${escapeHtml(host)} is listening (${data.latency_ms} ms)</div>`;
        } else {
            resultsBox.innerHTML = `<div style="color:var(--danger)"><b>✗ CLOSED / UNREACHABLE:</b> ${escapeHtml(data.message)}</div>`;
        }
    } catch (e) {
        resultsBox.innerHTML = `<p style="color:var(--danger)">Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function runDnsTest() {
    const domain = document.getElementById('dns-host-input').value.trim();
    const resultsBox = document.getElementById('dns-results-box');
    if (!domain) return;

    resultsBox.innerHTML = '<p class="text-muted">Resolving ' + escapeHtml(domain) + '...</p>';

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/dns-lookup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ domain })
        });
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();

        let html = `<div><b>Domain:</b> ${escapeHtml(domain)}</div>`;
        if (data.resolutions.local?.success) {
            html += `<div style="color:var(--success)"><b>Local Resolver (${data.resolutions.local.latency_ms}ms):</b> ${data.resolutions.local.ips.join(', ')}</div>`;
        } else {
            html += `<div style="color:var(--danger)"><b>Local Resolver:</b> Failed (${data.resolutions.local?.error})</div>`;
        }

        if (data.resolutions.google_dns?.success) {
            html += `<div style="color:var(--accent)"><b>Google DNS (8.8.8.8):</b> ${data.resolutions.google_dns.ips.join(', ')}</div>`;
        } else {
            html += `<div style="color:var(--danger)"><b>Google DNS:</b> Failed</div>`;
        }

        resultsBox.innerHTML = html;
    } catch (e) {
        resultsBox.innerHTML = `<p style="color:var(--danger)">Error: ${escapeHtml(e.message)}</p>`;
    }
}

async function fetchListeningPorts() {
    const tbody = document.getElementById('ports-table-body');
    tbody.innerHTML = '<tr><td colspan="5" class="text-muted">Scanning active listening sockets...</td></tr>';

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/network-ports`);
        if (!res.ok) throw new Error('Failed to fetch listening ports');
        const ports = await res.json();

        tbody.innerHTML = '';
        if (ports.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="no-data">No open listening ports detected.</td></tr>';
            return;
        }

        ports.forEach(p => {
            const row = document.createElement('tr');
            row.innerHTML = `
                <td><b>${p.port}</b></td>
                <td><span class="badge ${p.protocol === 'TCP' ? 'badge-success' : 'badge-warning'}">${p.protocol}</span></td>
                <td>${escapeHtml(p.ip)}</td>
                <td>${p.pid || 'N/A'}</td>
                <td><b>${escapeHtml(p.process)}</b></td>
            `;
            tbody.appendChild(row);
        });
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
}

/* Bottleneck Finder */
async function fetchBottlenecks() {
    const cpuBox = document.getElementById('cpu-hogs-box');
    const memBox = document.getElementById('mem-hogs-box');
    const stuckBox = document.getElementById('stuck-procs-box');

    cpuBox.innerHTML = '<p class="text-muted">Loading...</p>';
    memBox.innerHTML = '<p class="text-muted">Loading...</p>';
    stuckBox.innerHTML = '<p class="text-muted">Loading...</p>';

    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/bottlenecks`);
        if (!res.ok) throw new Error('Failed to fetch resource bottlenecks');
        const data = await res.json();

        // CPU Hogs
        cpuBox.innerHTML = (data.cpu_hogs || []).map(p => `
            <div class="disk-item" style="margin-bottom:6px;">
                <span><b>${escapeHtml(p.name)}</b> (PID ${p.pid})</span>
                <span><b class="text-danger">${p.cpu_percent}% CPU</b></span>
            </div>
        `).join('');

        // Memory Hogs
        memBox.innerHTML = (data.memory_hogs || []).map(p => `
            <div class="disk-item" style="margin-bottom:6px;">
                <span><b>${escapeHtml(p.name)}</b> (PID ${p.pid})</span>
                <span><b>${p.memory_mb} MB RAM</b> (${p.memory_percent}%)</span>
            </div>
        `).join('');

        // Stuck / Zombies
        if (!data.stuck_processes || data.stuck_processes.length === 0) {
            stuckBox.innerHTML = '<p class="no-data">✓ No zombie or hung processes detected.</p>';
        } else {
            stuckBox.innerHTML = data.stuck_processes.map(p => `
                <div class="issue-item danger">
                    <span><b>${escapeHtml(p.name)}</b> (PID ${p.pid}) - State: <b>${p.status}</b></span>
                    <button class="btn btn-sm btn-danger" onclick="remediateAction('kill_process', '${p.pid}')">💀 Terminate Process</button>
                </div>
            `).join('');
        }
    } catch (e) {
        cpuBox.innerHTML = `<p style="color:var(--danger)">Error: ${escapeHtml(e.message)}</p>`;
    }
}

/* Terminal & Command Runner */
async function executeCommand(cmdText = null) {
    const input = document.getElementById('cmd-input');
    const output = document.getElementById('cmd-output');
    const statusTag = document.getElementById('cmd-status-tag');

    const cmd = cmdText || input.value.trim();
    if (!cmd) return;

    output.textContent = `$ ${cmd}\nRunning command...`;
    statusTag.textContent = 'Running';
    statusTag.className = 'cmd-status-badge badge-warning';

    state.cmdHistory.push(cmd);
    state.cmdHistoryIndex = state.cmdHistory.length;

    try {
        const res = await fetch(`${API_BASE}/api/commands/run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd })
        });

        if (res.ok) {
            const result = await res.json();
            output.textContent = `$ ${cmd}\n\n` + (result.output || result.error || '[Process completed with no output]');
            statusTag.textContent = `Exit Code: ${result.returncode}`;
            statusTag.className = `cmd-status-badge ${result.returncode === 0 ? 'badge-success' : 'badge-danger'}`;
        } else {
            const errText = await readApiError(res);
            output.textContent = `$ ${cmd}\n\nCommand Failed:\n${errText}`;
            statusTag.textContent = 'Error';
            statusTag.className = 'cmd-status-badge badge-danger';
        }
    } catch (e) {
        output.textContent = `$ ${cmd}\n\nExecution Error: ${escapeHtml(e.message)}`;
        statusTag.textContent = 'Error';
        statusTag.className = 'cmd-status-badge badge-danger';
    }
}

/* Sub-Tab Navigation inside Troubleshoot */
function switchSubTab(subtabId) {
    document.querySelectorAll('.sub-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.sub-tab-content').forEach(c => c.classList.remove('active'));

    const btn = document.querySelector(`[data-subtab="${subtabId}"]`);
    if (btn) btn.classList.add('active');
    const content = document.getElementById(`subtab-${subtabId}`);
    if (content) content.classList.add('active');

    state.currentSubTab = subtabId;

    if (subtabId === 'health-hub') {
        if (!healthData) runFullHealthScan();
        else refreshFixEngine();
        loadFixHistory();
        if (!fixCapabilities) loadFixCapabilities();
    }
    if (subtabId === 'log-inspector') fetchLogs();
    if (subtabId === 'net-suite') fetchListeningPorts();
    if (subtabId === 'bottlenecks') fetchBottlenecks();
}

async function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.remove('active');
        b.removeAttribute('aria-current');
    });
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));

    const btn = document.querySelector(`[data-tab="${tabId}"]`);
    if (btn) {
        btn.classList.add('active');
        btn.setAttribute('aria-current', 'page');
    }
    document.getElementById(`tab-${tabId}`).classList.add('active');
    state.currentTab = tabId;

    if (tabId === 'processes') fetchStats();
    if (tabId === 'vms') {
        await fetchVmCapabilities();
        fetchVms();
        fetchVmAuditLog();
    }
    if (tabId === 'services') fetchServices();
    if (tabId === 'troubleshoot') switchSubTab(state.currentSubTab || 'health-hub');
}

/* Process Filtering & Sorting */
function filterProcesses() {
    const tbody = document.getElementById('all-processes-body');
    if (!tbody || !statsData?.processes) return;

    let procs = [...statsData.processes];
    const search = state.processSearch;
    const filter = state.processFilter;

    if (search) {
        procs = procs.filter(p => p.name.toLowerCase().includes(search) || String(p.pid).includes(search) || p.username.toLowerCase().includes(search));
    }

    if (filter === 'cpu') procs.sort((a, b) => b.cpu_percent - a.cpu_percent);
    else if (filter === 'mem') procs.sort((a, b) => b.memory_percent - a.memory_percent);
    else if (filter === 'pid') procs.sort((a, b) => a.pid - b.pid);
    else if (filter === 'name') procs.sort((a, b) => a.name.localeCompare(b.name));

    tbody.innerHTML = '';
    procs.forEach(p => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td><input type="checkbox" class="proc-check" value="${p.pid}"></td>
            <td><b>${p.pid}</b></td>
            <td>${escapeHtml(p.name)}</td>
            <td><b class="${p.cpu_percent > 50 ? 'text-danger' : ''}">${p.cpu_percent}%</b></td>
            <td>${p.memory_percent}%</td>
            <td>${p.memory_mb} MB</td>
            <td><span class="badge ${p.status === 'running' ? 'badge-success' : 'badge-warning'}">${escapeHtml(p.status)}</span></td>
            <td>${escapeHtml(p.username)}</td>
            <td>${p.threads || 1}</td>
            <td style="font-size:0.75rem">${p.create_time}</td>
            <td>
                <button class="btn btn-sm btn-outline" onclick="showProcessDetail(${p.pid})">Inspect</button>
                <button class="btn btn-sm btn-danger" onclick="killProcess(${p.pid})">Kill</button>
            </td>
        `;
        tbody.appendChild(row);
    });
}

/* VMs */
const VM_STATE_META = {
    running:      { label: 'RUNNING',       row: 'kpi-running' },
    shutoff:      { label: 'SHUTOFF',       row: 'kpi-stopped' },
    paused:       { label: 'PAUSED',        row: 'kpi-paused'  },
    pmsuspended:  { label: 'PMSUSPENDED',   row: 'kpi-paused'  },
    shutdown:     { label: 'SHUTTING DOWN', row: 'kpi-paused'  },
    crashed:      { label: 'CRASHED',       row: 'kpi-stopped' },
    blocked:      { label: 'BLOCKED',       row: 'kpi-other'   },
    no_state:     { label: 'NO STATE',      row: 'kpi-other'   },
    unknown:      { label: 'UNKNOWN',       row: 'kpi-other'   }
};

function vmActionButtons(vm) {
    const vmState = vm.state;
    const startable = vmState === 'shutoff' || vmState === 'crashed';
    const stoppable = vmState === 'running';
    const suspendable = vmState === 'running';
    const resumable = vmState === 'paused' || vmState === 'pmsuspended';
    const rebootable = vmState === 'running';
    const poweroffable = !startable && vmState !== 'no_state';

    const id = escapeHtml(vm.uuid || vm.name);
    const btn = (action, label, klass) => `<button type="button" class="btn ${klass}" data-vm-action="${action}" data-vm-id="${id}">${label}</button>`;
    let html = '';
    if (startable)    html += btn('start',    '▶ Start',      'btn-success');
    if (resumable)    html += btn('resume',   '▶ Resume',     'btn-success');
    if (stoppable)    html += btn('shutdown', '⏹ Shutdown',   'btn-warning');
    if (suspendable)  html += btn('suspend',  '⏸ Suspend',    'btn-outline');
    if (rebootable)   html += btn('reboot',   '↻ Reboot',     'btn-primary');
    if (poweroffable) html += btn('poweroff', '⏻ Poweroff',   'btn-danger');
    return html;
}

function vmExtraButtons(vm) {
    const running = vm.active && vm.state === 'running';
    const id = escapeHtml(vm.uuid || vm.name);
    const name = escapeHtml(vm.name);
    let html = '';

    // Console button - available for running VMs
    if (running) {
        html += `<button type="button" class="btn btn-sm btn-accent vm-console-btn" data-vm-console="${id}" data-vm-console-name="${name}" title="Open VM Console">🖥️ Console</button>`;
    }

    // Resize button - available for running or stopped VMs
    const currentMemMb = Math.round((vm.memory_total || vm.max_memory || 0) / 1024);
    html += `<button type="button" class="btn btn-sm btn-outline vm-resize-btn" data-vm-resize="${id}" data-vm-resize-name="${name}" data-vm-vcpus="${vm.vcpus || 1}" data-vm-mem="${currentMemMb}" title="Resize CPU/RAM">⚙️ Resize</button>`;

    return html;
}

function vmFilterSort(vms) {
    const search = state.vmSearch.toLowerCase();
    const stateFilter = state.vmStateFilter;
    let list = vms.filter(vm => {
        if (stateFilter !== 'all' && vm.state !== stateFilter) return false;
        if (!search) return true;
        return (vm.name || '').toLowerCase().includes(search)
            || (vm.uuid || '').toLowerCase().includes(search)
            || (vm.state || '').toLowerCase().includes(search);
    });
    const sortKey = state.vmSort;
    list.sort((a, b) => {
        if (sortKey === 'state') return (a.state || '').localeCompare(b.state || '');
        if (sortKey === 'cpu') return (b.cpu_percent || 0) - (a.cpu_percent || 0);
        if (sortKey === 'memory') return (b.memory_percent || 0) - (a.memory_percent || 0);
        return (a.name || '').localeCompare(b.name || '');
    });
    return list;
}

function renderVms(vms) {
    const container = document.getElementById('vm-list');
    if (!container) return;

    // Cache the latest inventory so filters/sorting can re-render without a
    // network round-trip, and so a suppressed render can be replayed later.
    state.vmLastData = vms;

    // Suppress re-rendering while an action is in flight. Rebuilding innerHTML
    // detaches the button the user just clicked (and every other listener),
    // which is why controls appeared dead during the 2s refresh cycle.
    if (state.vmPending.size > 0) return;

    document.getElementById('vm-count').textContent = `${vms.length} VM${vms.length === 1 ? '' : 's'}`;

    // KPI counts
    const counts = { total: vms.length, running: 0, stopped: 0, paused: 0, other: 0 };
    vms.forEach(vm => {
        const meta = VM_STATE_META[vm.state] || VM_STATE_META.unknown;
        if (vm.state === 'running') counts.running++;
        else if (vm.state === 'shutoff' || vm.state === 'crashed') counts.stopped++;
        else if (vm.state === 'paused' || vm.state === 'pmsuspended' || vm.state === 'shutdown') counts.paused++;
        else counts.other++;
    });
    document.getElementById('vm-kpi-total').textContent = counts.total;
    document.getElementById('vm-kpi-running').textContent = counts.running;
    document.getElementById('vm-kpi-stopped').textContent = counts.stopped;
    document.getElementById('vm-kpi-paused').textContent = counts.paused;
    document.getElementById('vm-kpi-other').textContent = counts.other;

    if (!vms.length) {
        container.innerHTML = `
            <div class="vm-empty-state">
                <div class="icon">🐳</div>
                <h3>No libvirt/KVM guests found</h3>
                <p>This host has no defined virtual machines. New guests can be created with
                <code>virt-install</code> or via the <code>cockpit-machines</code> web UI.</p>
            </div>`;
        return;
    }

    const visible = vmFilterSort(vms);
    if (!visible.length) {
        container.innerHTML = `<div class="vm-empty-state"><div class="icon">🔍</div><h3>No matching VMs</h3><p>Adjust the search query or state filter to see more guests.</p></div>`;
        return;
    }

    const canControl = state.vmCapabilities?.can_control ?? false;
    container.innerHTML = visible.map(vm => {
        const running = vm.active && vm.state === 'running';
        const rateStatus = running && !vm.rates_available ? 'Collecting live rates…' : '';
        const cpu = Number(vm.cpu_percent || 0);
        const memory = Number(vm.memory_percent || 0);
        const meta = VM_STATE_META[vm.state] || VM_STATE_META.unknown;
        const selected = state.vmSelected.has(vm.uuid) ? 'selected' : '';
        return `
            <article class="vm-card ${running ? 'vm-running' : ''} ${selected}" data-vm-uuid="${escapeHtml(vm.uuid)}" data-vm-name="${escapeHtml(vm.name)}">
                <div class="vm-card-header">
                    <label class="vm-checkbox-cell"><input type="checkbox" class="vm-checkbox" data-vm-select="${escapeHtml(vm.uuid)}" ${selected ? 'checked' : ''}></label>
                    <div><strong>${escapeHtml(vm.name)}</strong><div class="vm-uuid">${escapeHtml(vm.uuid || '')}</div></div>
                    <span class="vm-state ${escapeHtml(vm.state)}">${escapeHtml(meta.label)}</span>
                </div>
                <div class="vm-utilization">
                    <div class="vm-meter"><div><span>CPU</span><b>${cpu.toFixed(1)}%</b></div><div class="vm-progress"><i style="width:${Math.min(cpu, 100)}%"></i></div><small>of ${vm.vcpus || 0} vCPU(s)</small></div>
                    <div class="vm-meter memory"><div><span>RAM</span><b>${memory.toFixed(1)}%</b></div><div class="vm-progress"><i style="width:${Math.min(memory, 100)}%"></i></div><small>${formatBytes((vm.memory_used || 0) * 1024)} / ${formatBytes((vm.memory_total || vm.max_memory || 0) * 1024)}</small></div>
                </div>
                <div class="vm-live-grid">
                    <div><span>Disk read</span><b>↓ ${formatSpeed(vm.disk_read_bytes_sec)}</b></div>
                    <div><span>Disk write</span><b>↑ ${formatSpeed(vm.disk_write_bytes_sec)}</b></div>
                    <div><span>Network RX</span><b>↓ ${formatSpeed(vm.network_rx_bytes_sec)}</b></div>
                    <div><span>Network TX</span><b>↑ ${formatSpeed(vm.network_tx_bytes_sec)}</b></div>
                </div>
                <div class="vm-stats">
                    <div class="vm-stat"><span>Domain ID</span><span>${vm.id >= 0 ? vm.id : '—'}</span></div>
                    <div class="vm-stat"><span>Disks / NICs</span><span>${(vm.disks || []).length} / ${(vm.interfaces || []).length}</span></div>
                </div>
                ${rateStatus ? `<p class="vm-rate-status">${rateStatus}</p>` : ''}
                ${renderVmActions(vm, canControl)}
            </article>`;
    }).join('');

    // Action buttons and checkboxes are handled by a single delegated listener
    // installed once on the container (see initVmDelegation), so a re-render
    // can never leave the UI with dead controls.
    initVmDelegation(container);
}

// Attach exactly one delegated listener per container. Because it lives on the
// container rather than on each button, it survives every innerHTML rebuild.
function initVmDelegation(container) {
    if (!container || container.dataset.vmDelegated === '1') return;
    container.dataset.vmDelegated = '1';

    container.addEventListener('click', (e) => {
        // Handle VM lifecycle actions (start, shutdown, etc.)
        const btn = e.target.closest('[data-vm-action]');
        if (btn && container.contains(btn)) {
            e.preventDefault();
            e.stopPropagation();
            triggerVmAction(btn.dataset.vmId, btn.dataset.vmAction);
            return;
        }
        // Handle Console button
        const consoleBtn = e.target.closest('[data-vm-console]');
        if (consoleBtn && container.contains(consoleBtn)) {
            e.preventDefault();
            e.stopPropagation();
            openConsole(consoleBtn.dataset.vmConsole, consoleBtn.dataset.vmConsoleName);
            return;
        }
        // Handle Resize button
        const resizeBtn = e.target.closest('[data-vm-resize]');
        if (resizeBtn && container.contains(resizeBtn)) {
            e.preventDefault();
            e.stopPropagation();
            openResizeModal(
                resizeBtn.dataset.vmResize,
                resizeBtn.dataset.vmResizeName,
                parseInt(resizeBtn.dataset.vmVcpus) || 2,
                parseInt(resizeBtn.dataset.vmMem) || 2048
            );
            return;
        }
    });

    container.addEventListener('change', (e) => {
        const cb = e.target.closest('[data-vm-select]');
        if (!cb || !container.contains(cb)) return;
        if (cb.checked) state.vmSelected.add(cb.dataset.vmSelect);
        else state.vmSelected.delete(cb.dataset.vmSelect);
        updateBulkBar();
        const card = cb.closest('.vm-card');
        if (card) card.classList.toggle('selected', cb.checked);
    });
}

function renderVmActions(vm, canControl) {
    let actionsHtml = '';
    if (canControl) {
        const buttons = vmActionButtons(vm);
        if (buttons) actionsHtml = buttons;
    } else {
        const reason = state.vmCapabilities?.message
            || 'VM controls disabled — run systemd/install-service.sh to enable.';
        actionsHtml = `<span class="text-muted" style="font-size:.75rem;">${escapeHtml(reason)}</span>`;
    }
    const extraHtml = vmExtraButtons(vm);
    return `<div class="vm-actions">${actionsHtml}</div>${extraHtml ? `<div class="vm-extra-actions">${extraHtml}</div>` : ''}`;
}

function updateBulkBar() {
    const bar = document.getElementById('vm-bulk-bar');
    const countEl = document.getElementById('vm-selected-count');
    const n = state.vmSelected.size;
    countEl.textContent = n;
    bar.hidden = n === 0;
    document.getElementById('vm-bulk-start').disabled    = n === 0;
    document.getElementById('vm-bulk-shutdown').disabled = n === 0;
    document.getElementById('vm-bulk-poweroff').disabled = n === 0;
    document.getElementById('vm-bulk-reboot').disabled   = n === 0;
}

function clearVmSelection() {
    state.vmSelected.clear();
    updateBulkBar();
    document.querySelectorAll('.vm-checkbox').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.vm-card.selected').forEach(c => c.classList.remove('selected'));
}

async function fetchVms() {
    const container = document.getElementById('vm-list');
    try {
        const res = await fetch(`${API_BASE}/api/stats/vms`);
        if (res.status === 404) {
            renderVmsUnavailable();
            return;
        }
        if (!res.ok) throw new Error(await readApiError(res));
        renderVms(await res.json());
    } catch (e) {
        container.innerHTML = `<div class="issue-item danger">Error: ${escapeHtml(e.message)}</div>`;
    }
}

// Rendered when the WebSocket snapshot carries vms:null (libvirt unavailable).
// Keeps the VMs tab useful instead of hanging on the "Loading VMs..." splash.
function renderVmsUnavailable() {
    const container = document.getElementById('vm-list');
    if (!container) return;
    document.getElementById('vm-count').textContent = '0 VMs';
    container.innerHTML = `
        <div class="vm-empty-state">
            <div class="icon">⚠️</div>
            <h3>VM monitoring unavailable</h3>
            <p>The libvirt daemon is not running on this host, or the <code>python3-libvirt</code> package is not installed. Start the service and reload MonitorX to enable.</p>
        </div>`;
}

async function fetchVmCapabilities() {
    const notice = document.getElementById('vm-permission-notice');
    try {
        const res = await fetch(`${API_BASE}/api/vms/capabilities`);
        if (!res.ok) throw new Error(await readApiError(res));
        state.vmCapabilities = await res.json();
        if (notice) {
            notice.textContent = state.vmCapabilities.message;
            notice.className = `service-permission-notice ${state.vmCapabilities.can_control ? 'ready' : 'blocked'}`;
        }
        // Capabilities just landed; re-render VMs so action buttons appear.
        // Without this, a race in switchTab() may have rendered the cards
        // before the capabilities fetch finished, leaving them disabled.
        if (state.currentTab === 'vms') {
            const vms = state.vmLastData || statsData?.vms;
            if (vms) renderVms(vms);
        }
    } catch (e) {
        state.vmCapabilities = { can_control: false, can_list: false };
        if (notice) {
            notice.textContent = `Could not verify VM-control permissions: ${e.message}`;
            notice.className = 'service-permission-notice blocked';
        }
    }
}

async function fetchVmAuditLog() {
    const list = document.getElementById('vm-audit-list');
    try {
        const res = await fetch(`${API_BASE}/api/vms/log?limit=20`);
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();
        if (!data.entries || data.entries.length === 0) {
            list.innerHTML = '<p class="no-data">No VM control actions recorded yet.</p>';
            return;
        }
        list.innerHTML = data.entries.map(e => {
            const ts = new Date(e.timestamp);
            const time = ts.toLocaleTimeString();
            const date = ts.toLocaleDateString();
            const cls = e.success ? 'success' : 'failed';
            return `<div class="vm-audit-entry ${cls}">
                <time title="${escapeHtml(date)} ${escapeHtml(time)}">${escapeHtml(time)}</time>
                <span><span class="vm-audit-vm">${escapeHtml(e.vm)}</span> <span class="vm-audit-action">${escapeHtml(e.action)}</span> <span class="vm-audit-msg">${escapeHtml(e.message)}</span></span>
                <span>${e.success ? '✓' : '✗'}</span>
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = `<p class="issue-item danger">Error: ${escapeHtml(e.message)}</p>`;
    }
}

function confirmAction({ title, message, target, confirmLabel = 'Confirm', confirmClass = 'btn-danger' }) {
    return new Promise(resolve => {
        const modal = document.getElementById('confirm-modal');
        const titleEl = document.getElementById('confirm-modal-title');
        const msgEl = document.getElementById('confirm-modal-message');
        const targetEl = document.getElementById('confirm-modal-target');
        const cancelBtn = document.getElementById('confirm-modal-cancel');
        const confirmBtn = document.getElementById('confirm-modal-confirm');
        const closeBtn = document.getElementById('confirm-modal-close');

        titleEl.textContent = title;
        msgEl.textContent = message;
        targetEl.textContent = target || '';
        targetEl.style.display = target ? 'block' : 'none';
        confirmBtn.textContent = confirmLabel;
        confirmBtn.className = `btn ${confirmClass}`;

        const cleanup = (result) => {
            modal.classList.remove('show');
            confirmBtn.onclick = null;
            cancelBtn.onclick = null;
            closeBtn.onclick = null;
            resolve(result);
        };
        confirmBtn.onclick = () => cleanup(true);
        cancelBtn.onclick = () => cleanup(false);
        closeBtn.onclick = () => cleanup(false);
        modal.classList.add('show');
    });
}

function markCardPending(vmId, pending) {
    if (pending) state.vmPending.add(vmId);
    else state.vmPending.delete(vmId);

    // Match on name OR uuid: bulk actions dispatch by name while cards are
    // keyed by uuid, so a single-attribute lookup missed the card entirely and
    // the "Working…" overlay never appeared.
    const escaped = cssEscape(vmId);
    const card = document.querySelector(
        `.vm-card[data-vm-name="${escaped}"], .vm-card[data-vm-uuid="${escaped}"]`
    );
    if (card) card.classList.toggle('pending', pending);
}

function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, c => '\\' + c);
}

async function triggerVmAction(vmId, action, opts = {}) {
    if (!vmId || !action) return;

    // Ignore repeat clicks on a guest that already has an action in flight.
    if (state.vmPending.has(vmId)) return;

    if (state.vmCapabilities && state.vmCapabilities.can_control === false) {
        showToast(state.vmCapabilities.message
            || 'VM controls are not authorized. Run systemd/install-service.sh.', 'error');
        return;
    }

    const destructive = ['poweroff', 'destroy'];
    let confirmed = opts.skipConfirm === true;
    if (!confirmed && destructive.includes(action)) {
        confirmed = await confirmAction({
            title: `⚠️ Confirm ${action.toUpperCase()}`,
            message: `The "${action}" action immediately terminates the guest without graceful shutdown. Unsaved data inside the VM will be lost.`,
            target: `Target: ${vmDisplayName(vmId)}`,
            confirmLabel: `Yes, ${action.toUpperCase()}`,
            confirmClass: 'btn-danger'
        });
        if (!confirmed) return;
    }

    markCardPending(vmId, true);
    const label = vmDisplayName(vmId);
    const url = `${API_BASE}/api/vms/${encodeURIComponent(vmId)}/${action}`;
    const body = JSON.stringify({ confirm: confirmed || destructive.includes(action) });
    let succeeded = false;

    // Retry transient network failures once with a brief backoff.
    // "Failed to fetch" is a browser TypeError thrown when the TCP
    // connection is refused, reset, or times out — often caused by a
    // temporary exec backlog in the libvirt thread pool.
    let lastError = null;
    for (let attempt = 0; attempt < 2; attempt++) {
        if (attempt > 0) {
            await new Promise(r => setTimeout(r, 500));
        }
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body,
            });
            const result = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
            if (!res.ok) throw new Error(result.detail || `Action failed (${res.status})`);

            let msg = result.message || `${action} completed.`;
            let tone = 'success';
            if (result.noop) { msg = `No change: ${msg}`; tone = 'info'; }
            else if (result.pending) { tone = 'info'; }
            if (!opts.bulk) showToast(msg, tone);
            state.vmLastAction.set(vmId, { action, at: Date.now() });
            succeeded = true;
            break; // success — exit the retry loop
        } catch (e) {
            lastError = e;
            // Only retry on network/TypeError (e.g. "Failed to fetch"),
            // not on application-level 4xx/5xx errors.
            if (e instanceof TypeError || e.message === 'Failed to fetch' || e.name === 'TypeError') {
                continue; // retry
            }
            break; // application error — do not retry
        }
    }

    if (!succeeded && lastError) {
        const hint = lastError instanceof TypeError
            ? 'Network error — the server may be busy. Please try again.'
            : '';
        showToast(`Could not ${action} ${label}: ${lastError.message}${hint ? ' (' + hint + ')' : ''}`, 'error');
    }

    markCardPending(vmId, false);
    // Re-check authorization in case the operator just installed the policy.
    if (state.vmCapabilities && !state.vmCapabilities.can_control) fetchVmCapabilities();
    // Refresh immediately, then again once the guest has had time to settle
    // (graceful shutdown/reboot are asynchronous inside the guest).
    // During bulk runs the caller refreshes once at the end instead.
    if (!opts.bulk) {
        await fetchVms();
        fetchVmAuditLog();
        scheduleVmSettleRefresh();
    }

    return succeeded;
}

// Domain state changes land asynchronously; poll a few times after an action
// so the card reflects reality without waiting for the slow refresh tick.
function scheduleVmSettleRefresh() {
    [1500, 4000, 8000].forEach(delay => setTimeout(() => {
        if (state.currentTab === 'vms' && state.vmPending.size === 0) {
            fetchVms();
            fetchVmAuditLog();
        }
    }, delay));
}

// Resolve a UUID back to a human-friendly name for toasts and dialogs.
function vmDisplayName(vmId) {
    const match = (state.vmLastData || []).find(vm => vm.uuid === vmId || vm.name === vmId);
    return match ? match.name : vmId;
}

async function bulkVmAction(action) {
    if (state.vmSelected.size === 0) return;

    // Selection is stored as UUIDs, which is exactly what the API accepts.
    // The old code mapped them back to names via the DOM, so any guest that
    // was filtered out of view resolved to null and was silently dropped.
    const ids = Array.from(state.vmSelected);
    if (ids.length === 0) return;
    const names = ids.map(vmDisplayName);

    const destructive = ['poweroff', 'destroy'];
    if (destructive.includes(action)) {
        const confirmed = await confirmAction({
            title: `⚠️ Bulk ${action.toUpperCase()}`,
            message: `You are about to ${action} ${ids.length} guest(s). This cannot be undone for running VMs.`,
            target: `Targets: ${names.slice(0, 5).join(', ')}${names.length > 5 ? `, +${names.length - 5} more` : ''}`,
            confirmLabel: `Yes, ${action.toUpperCase()} all`,
            confirmClass: 'btn-danger'
        });
        if (!confirmed) return;
    }

    showToast(`Dispatching ${action} to ${ids.length} VM(s)…`, 'info');
    let ok = 0, failed = 0;
    for (const id of ids) {
        // Sequential dispatch keeps virsh from overloading the libvirt socket.
        const success = await triggerVmAction(id, action, { skipConfirm: true, bulk: true });
        if (success === false) failed++; else ok++;
    }
    showToast(`Bulk ${action}: ${ok} succeeded, ${failed} failed.`,
              failed ? 'error' : 'success');
    clearVmSelection();
    fetchVms();
    fetchVmAuditLog();
}

function setVmAutoRefresh(intervalSeconds) {
    const seconds = parseInt(intervalSeconds, 10);
    state.vmRefreshMs = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
    if (state.vmAutoTimer) {
        clearInterval(state.vmAutoTimer);
        state.vmAutoTimer = null;
    }
    if (state.vmRefreshMs > 0) {
        state.vmAutoTimer = setInterval(() => {
            if (state.currentTab === 'vms' && state.vmPending.size === 0) fetchVms();
        }, state.vmRefreshMs);
    }
}


/* ==========================================================================
   VM CONSOLE (xterm.js + WebSocket)
   ========================================================================== */

function openConsole(vmId, vmName) {
    state.consoleVmId = vmId;
    document.getElementById('console-vm-name').textContent = vmName || vmId;
    document.getElementById('console-conn-status').textContent = 'Connecting...';
    document.getElementById('console-conn-status').style.color = 'var(--warning)';
    document.getElementById('console-type-badge').textContent = '';

    const modal = document.getElementById('console-modal');
    modal.classList.add('show');

    // Initialize xterm.js
    if (state.consoleTerminal) {
        state.consoleTerminal.dispose();
        state.consoleTerminal = null;
    }
    const container = document.getElementById('console-terminal');
    container.innerHTML = '';

    const fontSize = parseInt(document.getElementById('console-font-size').value) || 14;
    const term = new Terminal({
        fontSize: fontSize,
        fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
        theme: {
            background: '#0a0e1a',
            foreground: '#e2e8f0',
            cursor: '#38bdf8',
            selectionBackground: 'rgba(56, 189, 248, 0.3)',
        },
        cursorBlink: true,
        allowProposedApi: true,
    });
    state.consoleTerminal = term;

    const fitAddon = new FitAddon.FitAddon();
    state.consoleAddonFit = fitAddon;
    term.loadAddon(fitAddon);
    term.open(container);

    setTimeout(() => {
        fitAddon.fit();
    }, 100);

    // Connect WebSocket
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${window.location.host}/ws/vm-console/${encodeURIComponent(vmId)}`;
    const ws = new WebSocket(wsUrl);
    state.consoleWs = ws;

    ws.onopen = () => {
        document.getElementById('console-conn-status').textContent = 'Connected';
        document.getElementById('console-conn-status').style.color = 'var(--success)';
    };

    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'vnc') {
                document.getElementById('console-type-badge').textContent = `VNC :${data.port}`;
                document.getElementById('console-type-badge').className = 'badge badge-success';
                document.getElementById('console-conn-status').textContent = `VNC connected to ${data.host}:${data.port}`;
            } else if (data.type === 'serial') {
                document.getElementById('console-type-badge').textContent = 'Serial Console';
                document.getElementById('console-type-badge').className = 'badge badge-warning';
                document.getElementById('console-conn-status').textContent = 'Serial console connected';
            } else if (data.type === 'error') {
                document.getElementById('console-conn-status').textContent = `Error: ${data.message}`;
                document.getElementById('console-conn-status').style.color = 'var(--danger)';
            }
        } catch (e) {
            // Binary data from VNC
            if (event.data instanceof ArrayBuffer) {
                term.write(new Uint8Array(event.data));
            } else if (event.data instanceof Blob) {
                event.data.arrayBuffer().then(buf => term.write(new Uint8Array(buf)));
            } else {
                term.write(event.data);
            }
        }
    };

    ws.onclose = () => {
        document.getElementById('console-conn-status').textContent = 'Disconnected';
        document.getElementById('console-conn-status').style.color = 'var(--danger)';
    };

    ws.onerror = () => {
        document.getElementById('console-conn-status').textContent = 'Connection error';
        document.getElementById('console-conn-status').style.color = 'var(--danger)';
    };

    // Terminal input -> WebSocket
    term.onData(data => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(new TextEncoder().encode(data));
        }
    });

    // Handle resize
    if (state.consoleResizeObserver) state.consoleResizeObserver.disconnect();
    state.consoleResizeObserver = new ResizeObserver(() => {
        if (state.consoleAddonFit) {
            state.consoleAddonFit.fit();
        }
    });
    state.consoleResizeObserver.observe(container);
}

function closeConsole() {
    if (state.consoleWs) {
        state.consoleWs.close();
        state.consoleWs = null;
    }
    if (state.consoleTerminal) {
        state.consoleTerminal.dispose();
        state.consoleTerminal = null;
        state.consoleAddonFit = null;
    }
    if (state.consoleResizeObserver) {
        state.consoleResizeObserver.disconnect();
        state.consoleResizeObserver = null;
    }
    document.getElementById('console-modal').classList.remove('show');
    state.consoleVmId = null;
}


/* ==========================================================================
   VM RESIZE
   ========================================================================== */

function openResizeModal(vmId, vmName, currentVcpus, currentMemMb) {
    state.resizeVmId = vmId;
    state.resizeVcpus = currentVcpus || 2;
    state.resizeMemMb = currentMemMb || 2048;

    document.getElementById('resize-vm-name').textContent = vmName || vmId;
    document.getElementById('resize-current-vcpus').textContent = `Current: ${currentVcpus || '?'} vCPU(s)`;
    document.getElementById('resize-current-mem').textContent = `Current: ${currentMemMb ? formatBytes(currentMemMb * 1024 * 1024) : '?'}`;

    const vcpuInput = document.getElementById('resize-vcpu-input');
    const vcpuSlider = document.getElementById('resize-vcpu-slider');
    const memInput = document.getElementById('resize-mem-input');
    const memSlider = document.getElementById('resize-mem-slider');

    vcpuInput.value = state.resizeVcpus;
    vcpuSlider.value = Math.min(state.resizeVcpus, 64);
    memInput.value = state.resizeMemMb;
    memSlider.value = Math.min(state.resizeMemMb, 65536);

    document.getElementById('resize-modal').classList.add('show');
}

async function applyResize() {
    if (!state.resizeVmId) return;
    const vcpus = parseInt(document.getElementById('resize-vcpu-input').value) || null;
    const memMb = parseInt(document.getElementById('resize-mem-input').value) || null;

    if (!vcpus && !memMb) {
        showToast('Provide at least one value to resize.', 'warning');
        return;
    }

    const btn = document.getElementById('resize-apply');
    btn.disabled = true;
    btn.textContent = 'Applying...';

    try {
        const body = {};
        if (vcpus) body.vcpus = vcpus;
        if (memMb) body.memory_mb = memMb;

        const res = await fetch(`${API_BASE}/api/vms/${encodeURIComponent(state.resizeVmId)}/resize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const result = await res.json();
        if (!res.ok) throw new Error(result.detail || 'Resize failed');

        showToast(result.message || 'VM resized successfully', 'success');
        closeResizeModal();
        fetchVms();
    } catch (e) {
        showToast(`Resize failed: ${e.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Apply Resize';
    }
}

function closeResizeModal() {
    document.getElementById('resize-modal').classList.remove('show');
    state.resizeVmId = null;
}


/* ==========================================================================
   DOCKER CONTAINERS
   ========================================================================== */

async function fetchContainers() {
    const panel = document.getElementById('containers-panel');
    const content = document.getElementById('containers-content');
    if (!content) return;

    try {
        const res = await fetch(`${API_BASE}/api/stats/containers`);
        if (res.status === 404) {
            renderContainersUnavailable();
            return;
        }
        if (!res.ok) throw new Error(await readApiError(res));
        const containers = await res.json();
        renderContainers(containers);
    } catch (e) {
        content.innerHTML = `<p class="no-data">Error: ${escapeHtml(e.message)}</p>`;
    }
}

// Rendered when the WebSocket snapshot carries containers:null (docker CLI
// missing, daemon down, or the 10s/20s collection timeout elapsed).
function renderContainersUnavailable() {
    const content = document.getElementById('containers-content');
    if (!content) return;
    content.innerHTML = '<p class="no-data">Docker is not installed or the daemon is unreachable on this host. Install Docker to monitor containers.</p>';
    const badge = document.getElementById('container-count');
    if (badge) badge.textContent = 'Docker N/A';
}

function renderContainers(containers) {
    const content = document.getElementById('containers-content');
    if (!content) return;

    const running = containers.filter(c => c.running);
    const stopped = containers.filter(c => !c.running);

    document.getElementById('container-count').textContent = `${containers.length} Containers (${running.length} running)`;

    if (containers.length === 0) {
        content.innerHTML = '<p class="no-data">No Docker containers found on this host.</p>';
        return;
    }

    let html = '<div class="container-grid">';

    // Running containers first
    running.forEach(c => {
        html += renderContainerCard(c);
    });
    // Then stopped
    stopped.forEach(c => {
        html += renderContainerCard(c);
    });

    html += '</div>';
    content.innerHTML = html;
}

function renderContainerCard(c) {
    const stateClass = c.running ? 'running' : 'stopped';
    const stateLabel = c.running ? 'RUNNING' : 'STOPPED';
    return `
        <div class="container-card ${stateClass}">
            <div class="container-card-header">
                <div>
                    <strong>${escapeHtml(c.name)}</strong>
                    <div class="container-id">${escapeHtml(c.id)}</div>
                </div>
                <span class="vm-state ${stateClass}">${stateLabel}</span>
            </div>
            <div class="container-details">
                <div><span>Image:</span><b>${escapeHtml(c.image)}</b></div>
                <div><span>Status:</span><b>${escapeHtml(c.status)}</b></div>
                ${c.ports ? `<div><span>Ports:</span><b>${escapeHtml(c.ports)}</b></div>` : ''}
                ${c.size ? `<div><span>Size:</span><b>${escapeHtml(c.size)}</b></div>` : ''}
            </div>
            <div class="container-actions">
                <button class="btn btn-sm btn-outline" onclick="showContainerLogs('${escapeHtml(c.id)}', '${escapeHtml(c.name)}')">📋 Logs</button>
            </div>
        </div>
    `;
}

async function showContainerLogs(containerId, containerName) {
    document.getElementById('logs-container-name').textContent = containerName || containerId;
    const output = document.getElementById('container-logs-output');
    output.textContent = 'Loading logs...';
    document.getElementById('container-logs-modal').classList.add('show');

    try {
        const res = await fetch(`${API_BASE}/api/stats/containers/${containerId}/logs?lines=200`);
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();
        output.textContent = data.logs || 'No logs available.';
    } catch (e) {
        output.textContent = `Error: ${e.message}`;
    }
}


/* ==========================================================================
   KUBERNETES PODS
   ========================================================================== */

async function fetchPods() {
    const panel = document.getElementById('pods-panel');
    const content = document.getElementById('pods-content');
    if (!content) return;

    try {
        const res = await fetch(`${API_BASE}/api/stats/pods`);
        if (res.status === 404) {
            panel.style.display = 'none';
            document.getElementById('pod-count').textContent = 'K8s N/A';
            return;
        }
        if (!res.ok) throw new Error(await readApiError(res));
        const pods = await res.json();
        panel.style.display = 'block';
        renderPods(pods);
    } catch (e) {
        panel.style.display = 'none';
    }
}

function renderPods(pods) {
    const content = document.getElementById('pods-content');
    if (!content) return;

    const running = pods.filter(p => p.status === 'Running');
    document.getElementById('pod-count').textContent = `${pods.length} Pods (${running.length} running)`;

    if (pods.length === 0) {
        content.innerHTML = '<p class="no-data">No Kubernetes pods found.</p>';
        return;
    }

    let html = '<div class="pod-grid">';
    pods.forEach(p => {
        const stateClass = p.status === 'Running' ? 'running' : (p.status === 'Failed' ? 'crashed' : 'paused');
        const stateLabel = p.status.toUpperCase();
        html += `
            <div class="container-card ${stateClass}">
                <div class="container-card-header">
                    <div>
                        <strong>${escapeHtml(p.name)}</strong>
                        <div class="container-id">${escapeHtml(p.namespace)} / ${escapeHtml(p.node)}</div>
                    </div>
                    <span class="vm-state ${stateClass}">${stateLabel}</span>
                </div>
                <div class="container-details">
                    <div><span>IP:</span><b>${escapeHtml(p.pod_ip || '—')}</b></div>
                    <div><span>Containers:</span><b>${p.container_count}</b></div>
                    <div><span>Restarts:</span><b class="${p.restarts > 5 ? 'text-danger' : ''}">${p.restarts}</b></div>
                    ${p.waiting_reasons.length ? `<div><span>Issues:</span><b class="text-danger">${escapeHtml(p.waiting_reasons.join(', '))}</b></div>` : ''}
                </div>
            </div>
        `;
    });
    html += '</div>';
    content.innerHTML = html;
}



/* Services */
async function readApiError(res) {
    try {
        const data = await res.json();
        return data.detail || data.message || `Request failed (${res.status})`;
    } catch (_) {
        return `Request failed (${res.status})`;
    }
}

async function fetchServiceCapabilities() {
    const notice = document.getElementById('service-permission-notice');
    try {
        const res = await fetch(`${API_BASE}/api/services/capabilities`);
        if (!res.ok) throw new Error(await readApiError(res));
        serviceCapabilities = await res.json();
        if (notice) {
            notice.textContent = serviceCapabilities.message;
            notice.className = `service-permission-notice ${serviceCapabilities.can_control ? 'ready' : 'blocked'}`;
        }
    } catch (e) {
        serviceCapabilities = { can_control: false };
        if (notice) {
            notice.textContent = `Unable to verify service-control permissions: ${e.message}`;
            notice.className = 'service-permission-notice blocked';
        }
    }
}

async function fetchServices(refreshPermissions = false) {
    try {
        if (!serviceCapabilities || refreshPermissions) await fetchServiceCapabilities();
        const res = await fetch(`${API_BASE}/api/services`);
        if (!res.ok) throw new Error(await readApiError(res));
        renderServices(await res.json());
    } catch (e) {
        showToast('Error fetching services: ' + e.message, 'error');
    }
}

function renderServices(services) {
    const tbody = document.getElementById('services-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    const filter = document.getElementById('service-filter').value;
    const search = document.getElementById('service-search').value.toLowerCase();
    let filtered = services;
    if (filter === 'running') filtered = filtered.filter(s => s.active === 'active');
    else if (filter === 'stopped') filtered = filtered.filter(s => s.active !== 'active');
    else if (filter === 'failed') filtered = filtered.filter(s => s.active === 'failed' || s.sub === 'failed');
    if (search) filtered = filtered.filter(s => s.name.toLowerCase().includes(search) || s.description.toLowerCase().includes(search));

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="no-data">No services match the current filter.</td></tr>';
        return;
    }
    const disabled = serviceCapabilities?.can_control ? '' : 'disabled title="Service controls are not configured"';
    filtered.forEach(s => {
        const row = document.createElement('tr');
        const escapedName = escapeHtml(s.name);
        row.innerHTML = `
            <td><b>${escapedName}</b></td><td>${escapeHtml(s.load)}</td>
            <td><span class="badge ${s.active === 'active' ? 'badge-success' : s.active === 'failed' ? 'badge-danger' : 'badge-warning'}">${escapeHtml(s.active)}</span></td>
            <td>${escapeHtml(s.sub)}</td><td style="font-size:0.8rem">${escapeHtml(s.description)}</td>
            <td><div class="service-actions">
                ${s.active !== 'active' ? `<button class="btn btn-sm btn-success" ${disabled} onclick="controlService('${escapedName}','start', this)">Start</button>` : ''}
                ${s.active === 'active' ? `<button class="btn btn-sm btn-warning" ${disabled} onclick="controlService('${escapedName}','stop', this)">Stop</button>` : ''}
                <button class="btn btn-sm btn-primary" ${disabled} onclick="controlService('${escapedName}','restart', this)">Restart</button>
                <button class="btn btn-sm btn-outline" ${disabled} onclick="controlService('${escapedName}','reload', this)">Reload</button>
                <button class="btn btn-sm btn-outline" ${disabled} onclick="controlService('${escapedName}','enable', this)">Enable</button>
                <button class="btn btn-sm btn-outline" ${disabled} onclick="controlService('${escapedName}','disable', this)">Disable</button>
                <button class="btn btn-sm btn-outline" onclick="openServiceLogs('${escapedName}')">📜 Logs</button>
            </div></td>`;
        tbody.appendChild(row);
    });
}

async function controlService(name, action, button) {
    if (!serviceCapabilities?.can_control) {
        showToast('Service controls are not authorized. Run systemd/install-service.sh.', 'error');
        return;
    }
    if (!confirm(`Are you sure you want to ${action.toUpperCase()} service ${name}?`)) return;
    const originalLabel = button?.textContent;
    if (button) { button.disabled = true; button.textContent = 'Working…'; }
    try {
        const res = await fetch(`${API_BASE}/api/services/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
        if (!res.ok) throw new Error(await readApiError(res));
        const result = await res.json();
        showToast(result.message, 'success');
        await fetchServices();
    } catch (e) {
        showToast(`Could not ${action} ${name}: ${e.message}`, 'error');
        if (button) { button.disabled = false; button.textContent = originalLabel; }
    }
}

/* Open the journal log viewer for a systemd unit (reuses /api/troubleshoot/logs). */
async function openServiceLogs(unit) {
    const modal = document.getElementById('service-logs-modal');
    if (!modal) return;
    document.getElementById('service-logs-name').textContent = unit;
    const out = document.getElementById('service-logs-output');
    out.textContent = 'Loading journal…';
    modal.classList.add('show');
    await fetchServiceLogs(unit);
}
async function fetchServiceLogs(unit) {
    const out = document.getElementById('service-logs-output');
    if (!out) return;
    const lines = document.getElementById('service-logs-lines')?.value || 100;
    out.textContent = 'Loading journal…';
    try {
        const res = await fetch(`${API_BASE}/api/troubleshoot/logs?lines=${lines}&level=all&service=${encodeURIComponent(unit)}`);
        if (!res.ok) throw new Error(await readApiError(res));
        const data = await res.json();
        if (!data.logs || !data.logs.length) {
            out.textContent = 'No log lines matched (empty journal or permission denied).';
            return;
        }
        out.textContent = data.logs.map(l => l.text).join('\n');
    } catch (e) {
        out.textContent = 'Could not load journal: ' + e.message;
    }
}
function closeServiceLogs() {
    document.getElementById('service-logs-modal')?.classList.remove('show');
}

/* Export a diagnostics report (JSON or Markdown) as a download. */
async function exportReport(format) {
    try {
        const res = await fetch(`${API_BASE}/api/report/export?format=${format}`);
        if (!res.ok) throw new Error(await readApiError(res));
        const blob = await res.blob();
        const disposition = res.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : `monitorx-report.${format === 'markdown' ? 'md' : 'json'}`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        showToast('Report exported: ' + filename, 'success');
    } catch (e) {
        showToast('Export failed: ' + e.message, 'error');
    }
}

/* Event Listeners Initialization */
const THEMES = ['midnight', 'aurora', 'ember', 'forest', 'nebula', 'graphite'];

/* Normalise legacy localStorage values to the new theme names.
   Older builds stored 'dark' / 'light'; we map them onto the closest theme. */
function normalizeTheme(value) {
    if (value === 'light') return 'aurora';
    if (value === 'dark') return 'midnight';
    return THEMES.includes(value) ? value : 'midnight';
}

function syncThemeMenu(name) {
    document.querySelectorAll('.theme-option').forEach(opt => {
        const active = opt.dataset.theme === name;
        opt.classList.toggle('active', active);
        opt.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    const btn = document.getElementById('theme-picker-btn');
    if (btn) {
        btn.setAttribute('aria-expanded', document.getElementById('theme-picker')?.classList.contains('open') ? 'true' : 'false');
        btn.title = `Switch theme (current: ${name})`;
    }
}

function setTheme(name, persist = true) {
    const theme = normalizeTheme(name);
    // Clear every theme class (and the legacy light flag), then apply the chosen one.
    THEMES.forEach(t => document.body.classList.remove('theme-' + t));
    document.body.classList.remove('light-theme');
    document.body.classList.add('theme-' + theme);
    // Aurora is the light theme — the existing light-mode overlay handling keys off .light-theme.
    if (theme === 'aurora') document.body.classList.add('light-theme');
    if (persist) {
        try { localStorage.setItem('monitorx-theme', theme); } catch (_) {}
    }
    syncThemeMenu(theme);
}

function applySavedTheme() {
    const savedTheme = localStorage.getItem('monitorx-theme');
    // First visit: follow the OS preference for light vs dark.
    if (!savedTheme) {
        const prefersLight = window.matchMedia?.('(prefers-color-scheme: light)').matches;
        setTheme(prefersLight ? 'aurora' : 'midnight', false);
        return;
    }
    setTheme(savedTheme, false);
}

function closeThemeMenu() {
    document.getElementById('theme-picker')?.classList.remove('open');
    syncThemeMenu(normalizeTheme(localStorage.getItem('monitorx-theme')));
}

function focusActiveTabSearch() {
    const searchByTab = {
        processes: 'proc-search',
        vms: 'vm-search',
        services: 'service-search',
        troubleshoot: 'cmd-input'
    };
    const target = document.getElementById(searchByTab[state.currentTab] || 'proc-search');
    target?.focus();
}

document.addEventListener('DOMContentLoaded', () => {
    applySavedTheme();

    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    // Sub tab switching
    document.querySelectorAll('.sub-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchSubTab(btn.dataset.subtab));
    });

    // Troubleshoot Scan
    document.getElementById('run-full-scan-btn').addEventListener('click', runFullHealthScan);

    // Auto-Fix Engine controls
    document.getElementById('fix-all-btn')?.addEventListener('click', () => runFixAll());
    document.getElementById('fix-review-btn')?.addEventListener('click', openFixPlanModal);
    document.getElementById('fix-plan-close')?.addEventListener('click', closeFixPlanModal);
    document.getElementById('fix-plan-cancel')?.addEventListener('click', closeFixPlanModal);
    document.getElementById('fix-plan-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('fix-plan-modal')) closeFixPlanModal();
    });
    document.getElementById('fix-plan-apply')?.addEventListener('click', applySelectedFixes);
    document.getElementById('fix-history-refresh')?.addEventListener('click', loadFixHistory);

    // Clear Fix History (new improvement)
    const clearFixHistoryBtn = document.getElementById('fix-history-clear');
    if (clearFixHistoryBtn) {
        clearFixHistoryBtn.addEventListener('click', async () => {
            if (!confirm('Clear all remediation history?')) return;
            try {
                await fetch(`${API_BASE}/api/troubleshoot/fix-history`, { method: 'DELETE' });
                loadFixHistory();
                showToast('Fix history cleared', 'info');
            } catch (_) {
                showToast('Could not clear history (endpoint may not exist)', 'warning');
            }
        });
    }

    // Refresh button
    document.getElementById('refresh-btn').addEventListener('click', () => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
        fetchStats();
        if (state.currentTab === 'troubleshoot') runFullHealthScan();
        showToast('Refreshed data', 'info');
    });

    // Theme picker: toggle the menu, choose a theme, close on outside click / Esc.
    const picker = document.getElementById('theme-picker');
    const pickerBtn = document.getElementById('theme-picker-btn');
    pickerBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        picker.classList.toggle('open');
        syncThemeMenu(normalizeTheme(localStorage.getItem('monitorx-theme')));
    });
    picker.querySelectorAll('.theme-option').forEach(opt => {
        opt.addEventListener('click', () => {
            setTheme(opt.dataset.theme);
            picker.classList.remove('open');
        });
    });
    document.addEventListener('click', (e) => {
        if (picker.classList.contains('open') && !picker.contains(e.target)) {
            closeThemeMenu();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && picker.classList.contains('open')) closeThemeMenu();
    });

    // Lightweight keyboard shortcuts: navigation only, never intercept typing.
    document.addEventListener('keydown', (event) => {
        const target = event.target;
        const isTyping = target.matches('input, textarea, select, [contenteditable="true"]');
        if (isTyping) return;
        // Never swallow browser/OS shortcuts: Ctrl/Cmd/Alt + key must pass
        // through (e.g. Ctrl+R page reload, Cmd+R).
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (event.key === '/') {
            event.preventDefault();
            focusActiveTabSearch();
        } else if (event.key.toLowerCase() === 'r') {
            event.preventDefault();
            document.getElementById('refresh-btn')?.click();
        } else if (/^[1-5]$/.test(event.key)) {
            const tabs = ['dashboard', 'processes', 'troubleshoot', 'vms', 'services'];
            switchTab(tabs[Number(event.key) - 1]);
        }
    });

    // View All Procs button
    document.getElementById('view-all-procs-btn')?.addEventListener('click', () => switchTab('processes'));

    // Clear Issues button (dismisses current OS alerts)
    document.getElementById('clear-issues-btn')?.addEventListener('click', clearIssues);

    // Process Search & Filter
    document.getElementById('proc-search')?.addEventListener('input', (e) => {
        state.processSearch = e.target.value.toLowerCase();
        filterProcesses();
    });

    document.getElementById('proc-filter')?.addEventListener('change', (e) => {
        state.processFilter = e.target.value;
        filterProcesses();
    });

    document.getElementById('select-all-proc')?.addEventListener('change', (e) => {
        document.querySelectorAll('.proc-check').forEach(c => c.checked = e.target.checked);
    });

    document.getElementById('kill-selected')?.addEventListener('click', () => {
        const checked = document.querySelectorAll('.proc-check:checked');
        if (checked.length === 0) { showToast('No processes selected', 'warning'); return; }
        killSelectedProcesses(Array.from(checked).map(c => parseInt(c.value)));
    });

    // Log Inspector Controls
    document.querySelectorAll('.level-pill').forEach(pill => {
        pill.addEventListener('click', () => {
            document.querySelectorAll('.level-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            state.logLevel = pill.dataset.level;
            fetchLogs();
        });
    });

    document.getElementById('log-lines-select')?.addEventListener('change', (e) => {
        state.logLines = parseInt(e.target.value);
        fetchLogs();
    });

    // Debounced log search
    let logSearchTimer = null;
    document.getElementById('log-search-input')?.addEventListener('input', () => {
        clearTimeout(logSearchTimer);
        logSearchTimer = setTimeout(() => fetchLogs(), 250);
    });
    document.getElementById('fetch-logs-btn')?.addEventListener('click', () => fetchLogs());

    document.getElementById('copy-logs-btn')?.addEventListener('click', () => {
        const container = document.getElementById('logs-container');
        navigator.clipboard.writeText(container.textContent);
        showToast('Logs copied to clipboard', 'success');
    });

    document.getElementById('log-autotail-toggle')?.addEventListener('change', (e) => {
        state.logAutoTail = e.target.checked;
        if (state.logAutoTail) {
            autoTailInterval = setInterval(fetchLogs, 2000);
            showToast('Auto-Tail streaming active', 'info');
        } else {
            if (autoTailInterval) clearInterval(autoTailInterval);
            showToast('Auto-Tail paused', 'info');
        }
    });

    // Network Suite Tool Events
    document.getElementById('run-ping-btn')?.addEventListener('click', runPingTest);
    document.getElementById('run-port-btn')?.addEventListener('click', runPortTest);
    document.getElementById('run-dns-btn')?.addEventListener('click', runDnsTest);
    document.getElementById('refresh-ports-btn')?.addEventListener('click', fetchListeningPorts);
    document.getElementById('refresh-vms-btn')?.addEventListener('click', async () => {
        await fetchVmCapabilities();
        fetchVms();
        fetchVmAuditLog();
    });

    // VM controls: search, filter, sort, bulk, auto-refresh.
    // Re-render from the freshest inventory (state.vmLastData), falling back to
    // the WebSocket snapshot. The old code only ever read statsData, which is
    // stale whenever the VM tab refreshed via REST.
    const rerenderVms = () => {
        const vms = state.vmLastData || statsData?.vms;
        if (vms) renderVms(vms);
    };
    document.getElementById('vm-search')?.addEventListener('input', (e) => {
        state.vmSearch = e.target.value;
        rerenderVms();
    });
    document.getElementById('vm-state-filter')?.addEventListener('change', (e) => {
        state.vmStateFilter = e.target.value;
        rerenderVms();
    });
    document.getElementById('vm-sort')?.addEventListener('change', (e) => {
        state.vmSort = e.target.value;
        rerenderVms();
    });
    document.getElementById('vm-refresh-select')?.addEventListener('change', (e) => {
        setVmAutoRefresh(e.target.value);
    });
    document.getElementById('vm-bulk-start')?.addEventListener('click', () => bulkVmAction('start'));
    document.getElementById('vm-bulk-shutdown')?.addEventListener('click', () => bulkVmAction('shutdown'));
    document.getElementById('vm-bulk-poweroff')?.addEventListener('click', () => bulkVmAction('poweroff'));
    document.getElementById('vm-bulk-reboot')?.addEventListener('click', () => bulkVmAction('reboot'));
    document.getElementById('vm-bulk-clear')?.addEventListener('click', clearVmSelection);
    document.getElementById('vm-audit-refresh')?.addEventListener('click', fetchVmAuditLog);

    // Keyboard shortcut: '/' to focus the VM search input on the VMs tab.
    document.addEventListener('keydown', (e) => {
        if (e.key === '/' && state.currentTab === 'vms' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            document.getElementById('vm-search')?.focus();
        }
    });

    // Terminal Command Runner
    document.getElementById('run-cmd')?.addEventListener('click', () => executeCommand());
    document.getElementById('clear-cmd-btn')?.addEventListener('click', () => {
        document.getElementById('cmd-output').textContent = 'Ready to execute commands.';
        document.getElementById('cmd-status-tag').textContent = 'Ready';
        document.getElementById('cmd-status-tag').className = 'cmd-status-badge';
    });

    document.getElementById('cmd-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            executeCommand();
        } else if (e.key === 'ArrowUp') {
            if (state.cmdHistory.length > 0 && state.cmdHistoryIndex > 0) {
                state.cmdHistoryIndex--;
                e.target.value = state.cmdHistory[state.cmdHistoryIndex];
            }
        } else if (e.key === 'ArrowDown') {
            if (state.cmdHistoryIndex < state.cmdHistory.length - 1) {
                state.cmdHistoryIndex++;
                e.target.value = state.cmdHistory[state.cmdHistoryIndex];
            } else {
                state.cmdHistoryIndex = state.cmdHistory.length;
                e.target.value = '';
            }
        }
    });

    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const cmd = btn.dataset.cmd;
            document.getElementById('cmd-input').value = cmd;
            executeCommand(cmd);
        });
    });

    // Services Filters
    document.getElementById('service-filter')?.addEventListener('change', fetchServices);
    document.getElementById('service-search')?.addEventListener('input', fetchServices);
    document.getElementById('refresh-services-btn')?.addEventListener('click', () => fetchServices(true));

    // Service journal viewer modal
    document.getElementById('service-logs-close')?.addEventListener('click', closeServiceLogs);
    document.getElementById('service-logs-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('service-logs-modal')) closeServiceLogs();
    });
    document.getElementById('service-logs-refresh')?.addEventListener('click', () => {
        const unit = document.getElementById('service-logs-name')?.textContent;
        if (unit) fetchServiceLogs(unit);
    });
    document.getElementById('service-logs-lines')?.addEventListener('change', () => {
        const unit = document.getElementById('service-logs-name')?.textContent;
        if (unit) fetchServiceLogs(unit);
    });

    // Report export button (opens a small choice: JSON or Markdown)
    document.getElementById('report-export-btn')?.addEventListener('click', () => {
        const fmt = confirm('Export diagnostics report.\n\nOK = Markdown\nCancel = JSON') ? 'markdown' : 'json';
        exportReport(fmt);
    });

    // Console Modal
    document.getElementById('console-modal-close')?.addEventListener('click', closeConsole);
    document.getElementById('console-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('console-modal')) closeConsole();
    });
    document.getElementById('console-clear')?.addEventListener('click', () => {
        if (state.consoleTerminal) state.consoleTerminal.clear();
    });
    document.getElementById('console-font-size')?.addEventListener('change', (e) => {
        if (state.consoleTerminal) {
            state.consoleTerminal.options.fontSize = parseInt(e.target.value) || 14;
            if (state.consoleAddonFit) state.consoleAddonFit.fit();
        }
    });

    // Resize Modal
    document.getElementById('resize-modal-close')?.addEventListener('click', closeResizeModal);
    document.getElementById('resize-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('resize-modal')) closeResizeModal();
    });
    document.getElementById('resize-cancel')?.addEventListener('click', closeResizeModal);
    document.getElementById('resize-apply')?.addEventListener('click', applyResize);

    // Resize slider <-> number sync
    document.getElementById('resize-vcpu-slider')?.addEventListener('input', (e) => {
        document.getElementById('resize-vcpu-input').value = e.target.value;
    });
    document.getElementById('resize-vcpu-input')?.addEventListener('input', (e) => {
        document.getElementById('resize-vcpu-slider').value = Math.min(parseInt(e.target.value) || 1, 64);
    });
    document.getElementById('resize-mem-slider')?.addEventListener('input', (e) => {
        document.getElementById('resize-mem-input').value = e.target.value;
    });
    document.getElementById('resize-mem-input')?.addEventListener('input', (e) => {
        document.getElementById('resize-mem-slider').value = Math.min(parseInt(e.target.value) || 256, 65536);
    });

    // Container Logs Modal
    document.getElementById('container-logs-close')?.addEventListener('click', () => {
        document.getElementById('container-logs-modal').classList.remove('show');
    });
    document.getElementById('container-logs-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('container-logs-modal')) {
            e.target.classList.remove('show');
        }
    });

    // Container & Pod refresh buttons
    document.getElementById('refresh-containers-btn')?.addEventListener('click', fetchContainers);
    document.getElementById('refresh-pods-btn')?.addEventListener('click', fetchPods);

    // Process Modal Close (original)
    document.getElementById('close-modal')?.addEventListener('click', () => {
        document.getElementById('process-modal').classList.remove('show');
    });
    document.getElementById('process-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('process-modal')) {
            e.target.classList.remove('show');
        }
    });

    // Startup initialization
    connectWebSocket();
    fetchStats();
    fetchVmCapabilities();
    setVmAutoRefresh(document.getElementById('vm-refresh-select')?.value ?? 2);
    // Fetch containers and pods on startup
    fetchContainers();
    fetchPods();
});
