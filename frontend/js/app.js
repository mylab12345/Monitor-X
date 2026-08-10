/* MonitorX v2.5 - Application Logic */
const API_BASE = '';
const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;

let ws = null;
let reconnectTimer = null;
let statsData = null;
let autoTailInterval = null;
let healthData = null;
let serviceCapabilities = null;
let servicesCache = [];
let serviceSearchTimer = null;
// Auto-Fix Engine state
let fixCapabilities = null;
let fixRunning = false;
let currentFixPlan = [];
let authPromptVisible = false;
// WebSocket frames may arrive faster than the browser can paint. Coalesce DOM
// work to one animation frame so controls stay responsive under load.
let pendingDashboardData = null;
let dashboardFramePending = false;

// Keep one native fetch reference so protected deployments can surface a
// friendly login prompt without changing every API call site.
const monitorxNativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
    const response = await monitorxNativeFetch(...args);
    if (response.status === 401) {
        window.dispatchEvent(new CustomEvent('monitorx:auth-required'));
    }
    return response;
};

function showAuthOverlay() {
    if (authPromptVisible || document.getElementById('monitorx-auth-overlay')) return;
    authPromptVisible = true;
    const overlay = document.createElement('div');
    overlay.id = 'monitorx-auth-overlay';
    overlay.className = 'modal show auth-overlay';
    overlay.innerHTML = `
        <div class="modal-content auth-card" role="dialog" aria-modal="true" aria-labelledby="monitorx-auth-title">
            <div class="modal-header"><h3 id="monitorx-auth-title">🔐 MonitorX sign-in</h3></div>
            <div class="modal-body">
                <p class="text-muted">This dashboard is protected. Enter the MonitorX authentication token to continue.</p>
                <form id="monitorx-auth-form" class="auth-form">
                    <label for="monitorx-auth-token">Authentication token</label>
                    <input id="monitorx-auth-token" type="password" autocomplete="current-password" required class="search-input">
                    <p id="monitorx-auth-error" class="auth-error" role="alert"></p>
                    <button type="submit" class="btn btn-primary">Sign in</button>
                </form>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    const form = overlay.querySelector('#monitorx-auth-form');
    const input = overlay.querySelector('#monitorx-auth-token');
    const error = overlay.querySelector('#monitorx-auth-error');
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        button.textContent = 'Signing in…';
        error.textContent = '';
        try {
            const response = await monitorxNativeFetch(`${API_BASE}/api/auth/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token: input.value })
            });
            if (!response.ok) throw new Error('Invalid authentication token.');
            overlay.remove();
            authPromptVisible = false;
            connectWebSocket();
            fetchStats();
        } catch (err) {
            error.textContent = err.message;
            input.select();
        } finally {
            button.disabled = false;
            button.textContent = 'Sign in';
        }
    });
    input.focus();
}
window.addEventListener('monitorx:auth-required', showAuthOverlay);

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
    // Systemd services state
    svcSearch: '',
    svcStateFilter: 'all',
    svcSort: 'name',
    svcSelected: new Set(),
    svcRefreshMs: 2000,
    svcAutoTimer: null,
    svcPending: new Set(),
    svcLastData: null,
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
    // Full process table cache (WebSocket keeps a lightweight top-N sample).
    processesFull: null,
    processListLoading: false,
    processListLastFetch: 0,
    // Selection survives table re-renders (rebuilt on every telemetry tick
    // would otherwise wipe checked rows), and the last rendered signature
    // lets us skip no-op DOM rebuilds entirely.
    procSelected: new Set(),
    procTableSig: '',
    topProcSig: '',
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

/* Compact large counters (inodes): 12,345,678 -> "12.3M" */
function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
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

/* Modal accessibility: focus the dialog on open, keep Tab inside it, restore
   focus on close, and make Escape behave consistently across every modal. */
function setupModalAccessibility() {
    let activeModal = null;
    let previouslyFocused = null;
    const focusable = (modal) => Array.from(modal.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    ));

    const sync = () => {
        const open = Array.from(document.querySelectorAll('.modal.show')).pop() || null;
        document.querySelectorAll('.modal').forEach(modal => modal.setAttribute('aria-hidden', modal === open ? 'false' : 'true'));
        if (open && open !== activeModal) {
            activeModal = open;
            previouslyFocused = document.activeElement;
            const first = focusable(open)[0] || open.querySelector('.modal-content');
            if (first) {
                if (first === open.querySelector('.modal-content')) first.setAttribute('tabindex', '-1');
                setTimeout(() => first.focus(), 0);
            }
        } else if (!open && activeModal) {
            activeModal = null;
            if (previouslyFocused && document.contains(previouslyFocused)) previouslyFocused.focus();
            previouslyFocused = null;
        }
    };

    const observer = new MutationObserver(sync);
    document.querySelectorAll('.modal').forEach(modal => observer.observe(modal, { attributes: true, attributeFilter: ['class'] }));
    sync();

    document.addEventListener('keydown', (event) => {
        const modal = Array.from(document.querySelectorAll('.modal.show')).pop();
        if (!modal) return;
        if (event.key === 'Escape' && modal.id !== 'monitorx-auth-overlay') {
            event.preventDefault();
            const close = modal.querySelector('.modal-close');
            if (close) close.click();
            else modal.classList.remove('show');
            return;
        }
        if (event.key !== 'Tab') return;
        const items = focusable(modal);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
}

/* WebSocket Connection */
function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        window.dispatchEvent(new CustomEvent('monitorx:datalink', { detail: { connected: true } }));
        document.getElementById('ws-status').className = 'status-indicator';
        document.getElementById('ws-status-text').textContent = 'Connected';
        document.querySelector('.status-dot').className = 'status-dot connected';
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };
    ws.onmessage = (event) => {
        try {
            statsData = JSON.parse(event.data);
            // Shared telemetry bus: progressive modules consume the same
            // frame; no second WebSocket is opened by the browser.
            window.dispatchEvent(new CustomEvent('monitorx:stats', { detail: statsData }));
            // P1: aria-live announcement + throttle (update max 1/sec)
            if (window.__announceTimeout) clearTimeout(window.__announceTimeout);
            window.__announceTimeout = setTimeout(() => {
                const ann = document.getElementById('sparkline-announce');
                if (ann && statsData) ann.textContent = `CPU ${(statsData.cpu?.percent ?? '--')}%, Memory ${(statsData.memory?.percent ?? '--')}%`;
            }, 1000);
            pendingDashboardData = statsData;
            if (!dashboardFramePending) {
                dashboardFramePending = true;
                requestAnimationFrame(() => {
                    dashboardFramePending = false;
                    const frame = pendingDashboardData;
                    pendingDashboardData = null;
                    if (!frame) return;
                    updateDashboard(frame);
                    updateLastUpdate();
                });
            }
        } catch (e) { console.error('Error parsing WebSocket frame:', e); }
    };
    ws.onclose = () => {
        window.dispatchEvent(new CustomEvent('monitorx:datalink', { detail: { connected: false } }));
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
    if (state.currentTab === 'dashboard') guard('charts', () => updateCharts(data));

    if (state.currentTab === 'processes') {
        // Telemetry tick: re-render only when the process data actually
        // changed (checked inside filterProcesses via its signature).
        guard('process-table', () => filterProcesses(false));
        fetchProcessList();
    }

    // VM panel: render inventory only when we have a real payload.
    if (state.currentTab === 'vms') {
        if (Array.isArray(data.vms)) guard('vms', () => renderVms(data.vms));
        else if (data.vms === null) guard('vms', renderVmsUnavailable);
    }

}

function updateCpu(cpu) {
    if (!cpu) return;
    document.getElementById('cpu-total').textContent = cpu.percent_total.toFixed(1) + '%';
    document.getElementById('cpu-cores').textContent = cpu.count_logical;
    document.getElementById('cpu-load').textContent = `${cpu.load_1min.toFixed(2)}, ${cpu.load_5min.toFixed(2)}, ${cpu.load_15min.toFixed(2)}`;
    document.getElementById('cpu-freq').textContent = (cpu.frequency_current / 1000).toFixed(2) + ' GHz';

    const barsContainer = document.getElementById('cpu-bars');
    const perCore = cpu.percent_per_core || [];
    // Core count changes rarely. Keep existing nodes and only mutate values,
    // avoiding a full layout/repaint of every bar on each telemetry frame.
    if (barsContainer.children.length !== perCore.length) {
        const fragment = document.createDocumentFragment();
        perCore.forEach(() => {
            const bar = document.createElement('div');
            bar.className = 'cpu-bar';
            const fill = document.createElement('div');
            fill.className = 'cpu-bar-fill';
            bar.appendChild(fill);
            fragment.appendChild(bar);
        });
        barsContainer.replaceChildren(fragment);
    }
    perCore.forEach((pct, idx) => {
        const bar = barsContainer.children[idx];
        const fill = bar?.firstElementChild;
        if (!bar || !fill) return;
        fill.style.height = Math.min(pct, 100) + '%';
        fill.classList.toggle('danger', pct > 85);
        fill.classList.toggle('warning', pct > 65 && pct <= 85);
        bar.title = `Core ${idx}: ${pct.toFixed(1)}%`;
    });
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

/* Storage card: tracks ONLY the root filesystem (/). Space and inode usage
   each get a bar plus used/free/total figures. The DOM block is rebuilt only
   when the rendered values actually change, so a steady telemetry stream does
   not thrash layout every frame. */
function updateDisk(disk) {
    if (!disk) return;
    const list = document.getElementById('disk-list');
    const root = disk.root || (disk.partitions || [])[0];

    if (!root) {
        list.dataset.sig = '';
        list.innerHTML = '<p class="no-data">Root filesystem (/) stats unavailable.</p>';
    } else {
        const spacePct = Math.min(Math.max(Number(root.percent) || 0, 0), 100);
        const inodePct = Math.min(Math.max(Number(root.inode_percent) || 0, 0), 100);
        const tone = (pct) => pct > 90 ? 'var(--danger)' : pct > 80 ? 'var(--warning)' : 'var(--accent-blue)';
        const deviceLabel = [root.device, root.fstype].filter(Boolean).join(' · ') || '/';

        // Cheap change signature: skip the whole rebuild when nothing moved.
        const sig = [spacePct, inodePct, root.used, root.free, root.total,
                     root.inode_used, root.inode_free, root.inode_total, deviceLabel].join('|');
        if (list.dataset.sig !== sig) {
            list.dataset.sig = sig;
            list.innerHTML = `
                <div class="root-disk">
                    <div class="root-disk-label">
                        <span>💾 Space — <b>${escapeHtml(deviceLabel)}</b></span>
                        <b>${spacePct.toFixed(1)}% used</b>
                    </div>
                    <div class="progress-bar"><div class="progress-fill" style="width:${spacePct}%;background:${tone(spacePct)};"></div></div>
                    <div class="root-disk-grid">
                        <div>Used: <b>${formatBytes(root.used)}</b></div>
                        <div>Free: <b>${formatBytes(root.free)}</b></div>
                        <div>Total: <b>${formatBytes(root.total)}</b></div>
                    </div>
                    <div class="root-disk-label">
                        <span>🧮 Inodes — <b>/</b></span>
                        <b>${inodePct.toFixed(1)}% used</b>
                    </div>
                    <div class="progress-bar"><div class="progress-fill" style="width:${inodePct}%;background:${tone(inodePct)};"></div></div>
                    <div class="root-disk-grid">
                        <div>Used: <b>${formatCount(root.inode_used)}</b></div>
                        <div>Free: <b>${formatCount(root.inode_free)}</b></div>
                        <div>Total: <b>${formatCount(root.inode_total)}</b></div>
                    </div>
                </div>`;
        }
        document.getElementById('disk-percent').textContent = `${spacePct.toFixed(1)}%`;
    }
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
    const driverEl = document.getElementById('gpu-driver-status');
    const countEl = document.getElementById('gpu-count-val');
    const totalEl = document.getElementById('gpu-total');
    if (!gpus || gpus.length === 0) {
        if (content) content.innerHTML = '<p class="no-data">No NVIDIA GPU detected or NVML disabled</p>';
        if (totalEl) totalEl.textContent = 'N/A';
        if (driverEl) driverEl.textContent = 'Inactive';
        if (countEl) countEl.textContent = '0';
        return;
    }

    if (driverEl) driverEl.textContent = 'Active';
    if (countEl) countEl.textContent = String(gpus.length);

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
    const value = (item) => escapeHtml(String(item ?? 'Unavailable'));
    const kernel = String(sys.platform_version ?? 'Unavailable');
    const metadata = [
        ['Hostname', sys.hostname],
        ['Operating system', [sys.platform, sys.platform_release].filter(Boolean).join(' ') || 'Unavailable'],
        ['Kernel version', kernel.length > 42 ? `${kernel.slice(0, 42)}…` : kernel],
        ['Architecture', sys.architecture],
        ['Uptime', sys.uptime_str],
        ['Boot time', sys.boot_time],
    ];

    info.innerHTML = metadata.map(([label, item]) => `
        <div class="system-info-row">
            <dt>${label}</dt>
            <dd title="${escapeAttr(item ?? 'Unavailable')}">${value(item)}</dd>
        </div>`).join('');
    info.setAttribute('aria-busy', 'false');
    document.getElementById('hostname').textContent = sys.hostname ?? '—';
    document.getElementById('uptime').textContent = 'Uptime: ' + (sys.uptime_str ?? '—');
}

function updateTopProcesses(processes) {
    const tbody = document.getElementById('top-processes-body');
    if (!tbody) return;
    const top = (processes || []).slice(0, 10);
    // Skip the rebuild when the visible rows are identical to last frame.
    const sig = top.map(p => `${p.pid}:${p.cpu_percent}:${p.memory_percent}:${p.memory_mb}:${p.status}:${p.threads}`).join('|');
    if (sig === state.topProcSig) return;
    state.topProcSig = sig;
    tbody.innerHTML = '';
    top.forEach(p => {
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

    if (data.disk) {
        const root = data.disk.root || (data.disk.partitions || [])[0];
        if (root) {
            const spacePct = Number(root.percent) || 0;
            const inodePct = Number(root.inode_percent) || 0;
            if (spacePct > 90) {
                addIssue('critical', `Root filesystem / is nearly full: ${spacePct.toFixed(1)}% used (${formatBytes(root.free)} free)`, {
                    label: 'Fix Now: Vacuum Logs',
                    kind: 'remediate',
                    action: 'vacuum_journal'
                });
            } else if (spacePct > 80) {
                addIssue('warning', `Root filesystem / storage high: ${spacePct.toFixed(1)}% used (${formatBytes(root.free)} free)`, {
                    label: 'Vacuum Logs',
                    kind: 'remediate',
                    action: 'vacuum_journal'
                });
            }
            if (inodePct > 90) {
                addIssue('critical', `Root filesystem / inodes nearly exhausted: ${inodePct.toFixed(1)}% used (${formatCount(root.inode_free)} free)`, {
                    label: 'Fix Now: Clean Temp Files',
                    kind: 'remediate',
                    action: 'clean_tmp'
                });
            } else if (inodePct > 80) {
                addIssue('warning', `Root filesystem / inode usage high: ${inodePct.toFixed(1)}% used (${formatCount(root.inode_free)} free)`, {
                    label: 'Clean Temp Files',
                    kind: 'remediate',
                    action: 'clean_tmp'
                });
            }
        }
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
                <button type="button" class="btn btn-danger" onclick="killProcess(${proc.pid})">💀 Terminate Process</button>
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
        // Terminated PIDs must not linger in the persistent selection.
        pids.forEach(pid => state.procSelected.delete(pid));
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
            navHtml = `<button type="button" class="btn btn-sm btn-primary" onclick="switchSubTab('bottlenecks')">🔥 Open Bottleneck Finder</button>`;
        } else if (!fixList.length && legacyAction === 'view_logs') {
            navHtml = `<button type="button" class="btn btn-sm btn-primary" onclick="switchSubTab('log-inspector')">📋 Inspect Logs</button>`;
        } else if (!fixList.length && legacyAction === 'run_net_diag') {
            navHtml = `<button type="button" class="btn btn-sm btn-primary" onclick="switchSubTab('net-suite')">🌐 Open Network Suite</button>`;
        } else if (!fixList.length && legacyAction === 'view_processes') {
            navHtml = `<button type="button" class="btn btn-sm btn-primary" onclick="switchTab('processes')">📋 Open Process Manager</button>`;
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
    const search = document.getElementById('log-search-input').value.slice(0, 128);

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
                    <button type="button" class="btn btn-sm btn-danger" onclick="remediateAction('kill_process', '${p.pid}')">💀 Terminate Process</button>
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

    if (tabId === 'processes') fetchProcessList(true);
    if (tabId === 'vms') {
        await fetchVmCapabilities();
        fetchVms();
        fetchVmAuditLog();
    }
    if (tabId === 'services') fetchServices();
    if (tabId === 'troubleshoot') switchSubTab(state.currentSubTab || 'health-hub');
}

/* Process Filtering & Sorting */
async function fetchProcessList(force = false) {
    if (state.processListLoading) return;
    if (!force && Date.now() - state.processListLastFetch < 5000) return;
    state.processListLoading = true;
    try {
        const res = await fetch(`${API_BASE}/api/stats/processes?limit=500`);
        if (!res.ok) throw new Error(await readApiError(res));
        state.processesFull = await res.json();
        state.processListLastFetch = Date.now();
        if (state.currentTab === 'processes') filterProcesses();
    } catch (error) {
        console.error('Full process list unavailable:', error);
        // Keep the WebSocket top-N sample as a useful fallback.
    } finally {
        state.processListLoading = false;
    }
}

/* Rebuild the full process table. Called with force=true for user-driven
   changes (search, sort, fresh fetch) and force=false on telemetry ticks:
   then the expensive DOM rebuild (up to 500 rows) only happens when the
   visible data actually changed, so the table no longer flickers or resets
   scroll/selection every 2 seconds. */
function filterProcesses(force = true) {
    const tbody = document.getElementById('all-processes-body');
    const source = state.processesFull || statsData?.processes;
    if (!tbody || !source) return;

    let procs = [...source];
    const search = state.processSearch;
    const filter = state.processFilter;

    if (search) {
        procs = procs.filter(p => String(p.name || '').toLowerCase().includes(search) || String(p.pid).includes(search) || String(p.username || '').toLowerCase().includes(search));
    }

    if (filter === 'cpu') procs.sort((a, b) => b.cpu_percent - a.cpu_percent);
    else if (filter === 'mem') procs.sort((a, b) => b.memory_percent - a.memory_percent);
    else if (filter === 'pid') procs.sort((a, b) => a.pid - b.pid);
    else if (filter === 'name') procs.sort((a, b) => a.name.localeCompare(b.name));

    // Signature of exactly what would be painted; identical => skip DOM work.
    const sig = (source === state.processesFull ? 'F' : 'W')
        + procs.map(p => `${p.pid},${p.cpu_percent},${p.memory_percent},${p.memory_mb},${p.status},${p.threads},${p.create_time}`).join(';');
    if (!force && sig === state.procTableSig) return;
    state.procTableSig = sig;

    // Forget selections for PIDs that vanished from the dataset entirely.
    const visiblePids = new Set(source.map(p => p.pid));
    state.procSelected.forEach(pid => { if (!visiblePids.has(pid)) state.procSelected.delete(pid); });

    tbody.innerHTML = '';
    procs.forEach(p => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td><input type="checkbox" class="proc-check" value="${p.pid}" ${state.procSelected.has(p.pid) ? 'checked' : ''}></td>
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
                <button type="button" class="btn btn-sm btn-outline" onclick="showProcessDetail(${p.pid})">Inspect</button>
                <button type="button" class="btn btn-sm btn-danger" onclick="killProcess(${p.pid})">Kill</button>
            </td>
        `;
        tbody.appendChild(row);
    });
    syncProcSelectAll();
}

/* Keep the header "select all" checkbox consistent with the current row
   selection (checked rows survive re-renders via state.procSelected). */
function syncProcSelectAll() {
    const selectAll = document.getElementById('select-all-proc');
    if (!selectAll) return;
    const boxes = document.querySelectorAll('#all-processes-body .proc-check');
    const checked = document.querySelectorAll('#all-processes-body .proc-check:checked');
    selectAll.checked = boxes.length > 0 && boxes.length === checked.length;
    selectAll.indeterminate = checked.length > 0 && checked.length < boxes.length;
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
        html += `<button type="button" class="btn btn-sm btn-accent vm-console-btn" data-vm-console="${id}" data-vm-console-name="${name}" title="Open VM serial console">🖥️ Serial Console</button>`;
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
                <div class="icon">🖥️</div>
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
   VM SERIAL CONSOLE (lazy-loaded xterm.js + WebSocket)
   ========================================================================== */

let consoleDependenciesPromise = null;
function loadConsoleDependencies() {
    if (typeof Terminal !== 'undefined' && typeof FitAddon !== 'undefined') return Promise.resolve();
    if (consoleDependenciesPromise) return consoleDependenciesPromise;

    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css';
    document.head.appendChild(style);
    const scripts = [
        'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js',
        'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js',
    ];
    consoleDependenciesPromise = scripts.reduce((promise, src) => promise.then(() => new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.onload = resolve;
        script.onerror = () => reject(new Error('Could not load the serial console library.'));
        document.head.appendChild(script);
    })), Promise.resolve());
    return consoleDependenciesPromise;
}

async function openConsole(vmId, vmName) {
    state.consoleVmId = vmId;
    document.getElementById('console-vm-name').textContent = vmName || vmId;
    document.getElementById('console-conn-status').textContent = 'Connecting...';
    document.getElementById('console-conn-status').style.color = 'var(--warning)';
    document.getElementById('console-type-badge').textContent = '';

    const modal = document.getElementById('console-modal');

    // xterm is loaded only when the operator opens a serial console, keeping
    // the normal dashboard shell fast and allowing the CDN to fail gracefully.
    if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
        showToast('Loading serial console…', 'info');
        try {
            await loadConsoleDependencies();
        } catch (error) {
            consoleDependenciesPromise = null;
            showToast(error.message, 'error');
            return;
        }
    }
    modal.classList.add('show');
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
            if (data.type === 'serial') {
                document.getElementById('console-type-badge').textContent = 'Serial Console';
                document.getElementById('console-type-badge').className = 'badge badge-warning';
                document.getElementById('console-conn-status').textContent = 'Serial console connected';
            } else if (data.type === 'error') {
                document.getElementById('console-conn-status').textContent = `Error: ${data.message}`;
                document.getElementById('console-conn-status').style.color = 'var(--danger)';
            }
        } catch (e) {
            // Serial console bytes
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

// Guard against concurrent fetchServices() calls that can pile up from
// DOMContentLoaded init, tab switching, and the auto-refresh timer.
let servicesFetchInProgress = false;
let servicesLoadAttempted = false;

async function fetchServices(refreshPermissions = false) {
    // Prevent multiple concurrent fetches from stacking up
    if (servicesFetchInProgress) return;
    servicesFetchInProgress = true;
    
    // Show loading state only on the very first load (never re-show after error)
    const container = document.getElementById('service-list');
    if (container && !servicesLoadAttempted && servicesCache.length === 0) {
        container.innerHTML = '<p class="no-data"><span class="svc-loading-spinner"></span> Loading systemd services…</p>';
    }
    servicesLoadAttempted = true;
    
    try {
        // Only fetch capabilities if not already loaded or explicitly refreshing
        if (!serviceCapabilities || refreshPermissions) {
            try {
                await fetchServiceCapabilities();
            } catch (capError) {
                console.warn('Service capabilities check failed:', capError);
                // Continue anyway - capabilities check failure shouldn't block service listing
            }
        }
        
        const res = await fetch(`${API_BASE}/api/services`);
        if (!res.ok) throw new Error(await readApiError(res));
        servicesCache = await res.json();
        renderServices(servicesCache);
    } catch (e) {
        console.error('Error fetching services:', e);
        // Only show toast on the first failure (auto-refresh keeps retrying silently)
        if (servicesCache.length === 0) {
            showToast('Error fetching services: ' + e.message, 'error');
        }
        renderServicesUnavailable(e.message);
    } finally {
        servicesFetchInProgress = false;
    }
}

/* Classify a unit into one of the three UI buckets (active / failed / inactive). */
function svcClass(s) {
    if (s.active === 'failed' || s.sub === 'failed') return 'failed';
    if (s.active === 'active') return 'active';
    return 'inactive';
}

function svcStateLabel(s) {
    const cls = svcClass(s);
    const sub = s.sub === 'running' ? 'RUNNING' : String(s.sub || '').toUpperCase();
    if (cls === 'failed') return `FAILED · ${sub}`;
    if (cls === 'active') return `ACTIVE · ${sub}`;
    return `INACTIVE · ${sub}`;
}

function svcFilterSort(services) {
    const search = state.svcSearch.toLowerCase();
    let list = services.filter(s => {
        if (state.svcStateFilter === 'running' && s.active !== 'active') return false;
        if (state.svcStateFilter === 'stopped' && s.active === 'active') return false;
        if (state.svcStateFilter === 'failed' && s.active !== 'failed' && s.sub !== 'failed') return false;
        if (!search) return true;
        return (s.name || '').toLowerCase().includes(search)
            || (s.description || '').toLowerCase().includes(search)
            || (s.active || '').toLowerCase().includes(search)
            || (s.sub || '').toLowerCase().includes(search);
    });
    list.sort((a, b) => {
        if (state.svcSort === 'state') {
            const rank = { failed: 0, active: 1, inactive: 2 };
            return (rank[svcClass(a)] ?? 3) - (rank[svcClass(b)] ?? 3)
                || (a.name || '').localeCompare(b.name || '');
        }
        if (state.svcSort === 'load') return (a.load || '').localeCompare(b.load || '');
        return (a.name || '').localeCompare(b.name || '');
    });
    return list;
}

function svcToken(value) {
    return String(value ?? 'unknown').toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
}

function renderServices(services) {
    const container = document.getElementById('service-list');
    if (!container) return;

    // Cache latest inventory so filters/sorting re-render without a network call.
    state.svcLastData = services;

    // Suppress re-rendering while an action is in flight (see renderVms).
    if (state.svcPending.size > 0) return;

    // Do not keep selections for units that disappeared between refreshes.
    const knownNames = new Set(services.map(s => s.name));
    state.svcSelected.forEach(name => {
        if (!knownNames.has(name)) state.svcSelected.delete(name);
    });

    const countEl = document.getElementById('svc-count');
    if (countEl) {
        countEl.textContent = `${services.length} Service${services.length === 1 ? '' : 's'}`;
    }

    // KPI counts stay available, but the stylesheet renders them as one-line
    // chips instead of tall cards.
    const counts = { total: services.length, active: 0, failed: 0, inactive: 0, loaded: 0 };
    services.forEach(s => {
        const cls = svcClass(s);
        if (cls === 'failed') counts.failed++;
        else if (cls === 'active') counts.active++;
        else counts.inactive++;
        if (s.load === 'loaded') counts.loaded++;
    });
    ['total', 'active', 'failed', 'inactive', 'loaded'].forEach(key => {
        const el = document.getElementById(`svc-kpi-${key}`);
        if (el) el.textContent = counts[key];
    });

    if (!services.length) {
        container.innerHTML = `
            <div class="svc-empty-state">
                <div class="icon">⚙️</div>
                <h3>No systemd services found</h3>
                <p>This host exposes no <code>.service</code> units through the systemd manager.</p>
            </div>`;
        updateSvcBulkBar();
        return;
    }

    const visible = svcFilterSort(services);
    if (!visible.length) {
        container.innerHTML = `<div class="svc-empty-state"><div class="icon">🔍</div><h3>No matching services</h3><p>Adjust the search query or state filter to see more units.</p></div>`;
        updateSvcBulkBar();
        return;
    }

    const canControl = serviceCapabilities?.can_control ?? false;
    const rows = visible.map(s => {
        const cls = svcClass(s);
        const rawName = String(s.name || '');
        const name = escapeHtml(rawName);
        const description = escapeHtml(s.description || '—');
        const load = String(s.load || 'unknown');
        const startup = String(s.unit_file_state || 'runtime');
        const selected = state.svcSelected.has(rawName) ? 'selected' : '';
        const safeLoad = svcToken(load);
        const safeStartup = svcToken(startup);
        const stateLabel = svcStateLabel(s);
        return `
            <tr class="svc-card svc-row svc-${cls} ${selected}" data-svc-name="${escapeAttr(rawName)}">
                <td class="svc-select-cell">
                    <input type="checkbox" class="svc-checkbox" data-svc-select="${escapeAttr(rawName)}" ${selected ? 'checked' : ''}
                        aria-label="Select ${escapeAttr(rawName)}" title="Select ${escapeAttr(rawName)}">
                </td>
                <td class="svc-service-cell">
                    <span class="svc-service-name" title="${escapeAttr(rawName)}">${name}</span>
                    <span class="svc-service-desc" title="${escapeAttr(String(s.description || '—'))}">${description}</span>
                </td>
                <td><span class="svc-state ${cls}" title="${escapeAttr(stateLabel)}">${escapeHtml(stateLabel)}</span></td>
                <td><span class="svc-meta-value svc-load-${safeLoad}" title="Load state: ${escapeAttr(load)}">${escapeHtml(load)}</span></td>
                <td><span class="svc-meta-value svc-start-${safeStartup}" title="Startup: ${escapeAttr(startup)}">${escapeHtml(startup)}</span></td>
                <td class="svc-actions-cell">${renderSvcActions(s, canControl)}</td>
            </tr>`;
    }).join('');

    container.innerHTML = `
        <div class="svc-table-shell">
            <div class="svc-table-scroll">
                <table class="svc-table" aria-label="Systemd services and controls">
                    <thead>
                        <tr>
                            <th scope="col"><input type="checkbox" class="svc-select-all" data-svc-select-all aria-label="Select all visible services" title="Select all visible services"></th>
                            <th scope="col">Service</th>
                            <th scope="col">Status</th>
                            <th scope="col">Load</th>
                            <th scope="col">Startup</th>
                            <th scope="col">Actions</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <div class="svc-list-summary">
                <span>Showing <strong>${visible.length}</strong> of <strong>${services.length}</strong> services</span>
                <span>Select rows for bulk actions</span>
            </div>
        </div>`;

    initSvcDelegation(container);
    updateSvcBulkBar();
    updateSvcSelectAll();
}

function svcActionButton(action, icon, label, serviceName, title = label) {
    const name = escapeAttr(serviceName);
    const tone = { start: 'btn-success', stop: 'btn-warning', restart: 'btn-primary' }[action] || 'btn-outline';
    return `<button type="button" class="btn btn-sm ${tone} svc-action-btn svc-action-${action}" data-svc-action="${action}" data-svc-name="${name}" title="${escapeAttr(title)}" aria-label="${escapeAttr(label)} ${name}"><span class="svc-action-icon" aria-hidden="true">${icon}</span><span class="svc-action-label">${label}</span></button>`;
}

function renderSvcActions(s, canControl) {
    const name = String(s.name || '');
    const active = s.active === 'active';
    const failed = svcClass(s) === 'failed';
    if (!canControl) {
        const reason = serviceCapabilities?.message
            || 'Service controls are not configured — run systemd/install-service.sh to enable.';
        return `<div class="svc-actions"><span class="svc-controls-note" title="${escapeAttr(reason)}">🔒 Controls unavailable</span></div>`;
    }
    let html = '';
    if (!active) html += svcActionButton('start', '▶', 'Start', name, failed ? 'Start service (failed units will attempt recovery)' : 'Start service');
    if (active)  html += svcActionButton('stop', '⏹', 'Stop', name, 'Stop service');
    html += svcActionButton('restart', '↻', 'Restart', name, 'Restart service');
    html += svcActionButton('reload', '↺', 'Reload', name, 'Reload configuration without restarting');
    html += svcActionButton('enable', '◉', 'Enable', name, 'Enable service at boot');
    html += svcActionButton('disable', '○', 'Disable', name, 'Disable service at boot');
    html += `<button type="button" class="btn btn-sm btn-outline svc-action-btn svc-action-logs" data-svc-logs="${escapeAttr(name)}" title="View journal logs" aria-label="View journal logs for ${escapeAttr(name)}"><span class="svc-action-icon" aria-hidden="true">📜</span><span class="svc-action-label">Logs</span></button>`;
    return `<div class="svc-actions">${html}</div>`;
}

/* One delegated listener per container — survives every innerHTML rebuild. */
function initSvcDelegation(container) {
    if (!container || container.dataset.svcDelegated === '1') return;
    container.dataset.svcDelegated = '1';

    container.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-svc-action]');
        if (btn && container.contains(btn)) {
            e.preventDefault();
            e.stopPropagation();
            controlService(btn.dataset.svcName, btn.dataset.svcAction);
            return;
        }
        const logBtn = e.target.closest('[data-svc-logs]');
        if (logBtn && container.contains(logBtn)) {
            e.preventDefault();
            e.stopPropagation();
            openServiceLogs(logBtn.dataset.svcLogs);
            return;
        }
    });

    container.addEventListener('change', (e) => {
        const selectAll = e.target.closest('[data-svc-select-all]');
        if (selectAll && container.contains(selectAll)) {
            const visible = svcFilterSort(servicesCache);
            visible.forEach(service => {
                if (selectAll.checked) state.svcSelected.add(service.name);
                else state.svcSelected.delete(service.name);
            });
            renderServices(servicesCache);
            return;
        }

        const cb = e.target.closest('[data-svc-select]');
        if (!cb || !container.contains(cb)) return;
        if (cb.checked) state.svcSelected.add(cb.dataset.svcSelect);
        else state.svcSelected.delete(cb.dataset.svcSelect);
        updateSvcBulkBar();
        updateSvcSelectAll();
        const card = cb.closest('.svc-card');
        if (card) card.classList.toggle('selected', cb.checked);
    });
}

function markSvcPending(name, pending) {
    if (pending) state.svcPending.add(name);
    else state.svcPending.delete(name);
    const card = document.querySelector(`.svc-card[data-svc-name="${cssEscape(name)}"]`);
    if (card) card.classList.toggle('pending', pending);
}

function updateSvcSelectAll() {
    const selectAll = document.querySelector('[data-svc-select-all]');
    if (!selectAll) return;
    const visible = svcFilterSort(servicesCache);
    const selected = visible.filter(service => state.svcSelected.has(service.name)).length;
    selectAll.disabled = visible.length === 0;
    selectAll.checked = visible.length > 0 && selected === visible.length;
    selectAll.indeterminate = selected > 0 && selected < visible.length;
}

function updateSvcBulkBar() {
    const bar = document.getElementById('svc-bulk-bar');
    const count = document.getElementById('svc-selected-count');
    const n = state.svcSelected.size;
    if (count) count.textContent = n;
    if (bar) bar.hidden = n === 0;
    ['start', 'stop', 'restart', 'reload', 'enable', 'disable'].forEach(a => {
        const btn = document.getElementById(`svc-bulk-${a}`);
        if (btn) btn.disabled = n === 0 || !(serviceCapabilities?.can_control ?? false);
    });
}

function clearSvcSelection() {
    state.svcSelected.clear();
    updateSvcBulkBar();
    document.querySelectorAll('[data-svc-select], [data-svc-select-all]').forEach(cb => {
        cb.checked = false;
        cb.indeterminate = false;
    });
    document.querySelectorAll('.svc-card.selected').forEach(c => c.classList.remove('selected'));
}

async function controlService(name, action, opts = {}) {
    if (!serviceCapabilities?.can_control) {
        showToast('Service controls are not authorized. Run systemd/install-service.sh.', 'error');
        return false;
    }
    const destructive = ['stop', 'restart', 'disable'];
    if (!opts.skipConfirm && destructive.includes(action)) {
        const confirmed = await confirmAction({
            title: `⚠️ ${action.toUpperCase()} service`,
            message: `You are about to ${action} the systemd unit.`,
            target: `Target: ${name}`,
            confirmLabel: `Yes, ${action.toUpperCase()}`,
            confirmClass: action === 'restart' ? 'btn-warning' : 'btn-danger'
        });
        if (!confirmed) return false;
    }
    if (state.svcPending.has(name)) return false;
    markSvcPending(name, true);
    try {
        const res = await fetch(`${API_BASE}/api/services/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
        if (!res.ok) throw new Error(await readApiError(res));
        const result = await res.json();
        showToast(result.message, 'success');
        return true;
    } catch (e) {
        showToast(`Could not ${action} ${name}: ${e.message}`, 'error');
        return false;
    } finally {
        markSvcPending(name, false);
        if (!opts.bulk) {
            await fetchServices();
            scheduleSvcSettleRefresh();
        }
    }
}

/* systemd applies actions asynchronously; poll a few times so cards settle. */
function scheduleSvcSettleRefresh() {
    [1500, 4000, 8000].forEach(delay => setTimeout(() => {
        if (state.currentTab === 'services' && state.svcPending.size === 0) fetchServices();
    }, delay));
}

async function bulkSvcAction(action) {
    if (state.svcSelected.size === 0) return;
    const names = Array.from(state.svcSelected);
    const destructive = ['stop', 'restart', 'disable'];
    if (destructive.includes(action)) {
        const confirmed = await confirmAction({
            title: `⚠️ Bulk ${action.toUpperCase()}`,
            message: `You are about to ${action} ${names.length} service(s).`,
            target: `Targets: ${names.slice(0, 5).join(', ')}${names.length > 5 ? `, +${names.length - 5} more` : ''}`,
            confirmLabel: `Yes, ${action.toUpperCase()} all`,
            confirmClass: action === 'restart' ? 'btn-warning' : 'btn-danger'
        });
        if (!confirmed) return;
    }
    showToast(`Dispatching ${action} to ${names.length} service(s)…`, 'info');
    let ok = 0, failed = 0;
    for (const name of names) {
        const success = await controlService(name, action, { skipConfirm: true, bulk: true });
        if (success) ok++; else failed++;
    }
    showToast(`Bulk ${action}: ${ok} succeeded, ${failed} failed.`, failed ? 'error' : 'success');
    clearSvcSelection();
    fetchServices();
}

function setSvcAutoRefresh(intervalSeconds) {
    const seconds = parseInt(intervalSeconds, 10);
    state.svcRefreshMs = Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0;
    if (state.svcAutoTimer) {
        clearInterval(state.svcAutoTimer);
        state.svcAutoTimer = null;
    }
    if (state.svcRefreshMs > 0) {
        state.svcAutoTimer = setInterval(() => {
            if (state.currentTab === 'services' && state.svcPending.size === 0) fetchServices();
        }, state.svcRefreshMs);
    }
}

/* Rendered when /api/services itself fails (systemd bus unavailable, etc.). */
function renderServicesUnavailable(message) {
    const container = document.getElementById('service-list');
    if (!container) return;
    document.getElementById('svc-count').textContent = '0 Services';
    ['svc-kpi-total', 'svc-kpi-active', 'svc-kpi-failed', 'svc-kpi-inactive', 'svc-kpi-loaded']
        .forEach(id => { const el = document.getElementById(id); if (el) el.textContent = '0'; });
    container.innerHTML = `
        <div class="svc-empty-state">
            <div class="icon">⚠️</div>
            <h3>Systemd services unavailable</h3>
            <p>The systemd manager could not be reached on this host. This usually means the host has no
            <code>systemd</code> running (e.g. a container), or <code>systemctl</code> is not installed.
            Error: <code>${escapeHtml(message)}</code></p>
        </div>`;
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
const THEMES = ['midnight', 'aurora', 'ember', 'forest', 'nebula', 'graphite',
                'ocean', 'lagoon', 'meadow', 'desert', 'canyon', 'arctic', 'sakura'];

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
        const boxes = document.querySelectorAll('#all-processes-body .proc-check');
        boxes.forEach(c => {
            c.checked = e.target.checked;
            const pid = parseInt(c.value);
            if (e.target.checked) state.procSelected.add(pid);
            else state.procSelected.delete(pid);
        });
        syncProcSelectAll();
    });

    // Row checkboxes update the persistent selection set (delegated: rows are
    // rebuilt on data changes, so listeners must live on the container).
    document.getElementById('all-processes-body')?.addEventListener('change', (e) => {
        const box = e.target.closest('.proc-check');
        if (!box) return;
        const pid = parseInt(box.value);
        if (box.checked) state.procSelected.add(pid);
        else state.procSelected.delete(pid);
        syncProcSelectAll();
    });

    document.getElementById('kill-selected')?.addEventListener('click', () => {
        const pids = Array.from(state.procSelected);
        if (pids.length === 0) { showToast('No processes selected', 'warning'); return; }
        killSelectedProcesses(pids);
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
        logSearchTimer = setTimeout(() => fetchLogs(), 350);
    });
    document.getElementById('fetch-logs-btn')?.addEventListener('click', () => fetchLogs());

    document.getElementById('copy-logs-btn')?.addEventListener('click', async () => {
        const container = document.getElementById('logs-container');
        try {
            if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable in this browser.');
            await navigator.clipboard.writeText(container.textContent);
            showToast('Logs copied to clipboard', 'success');
        } catch (error) {
            showToast(error.message, 'error');
        }
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

    // Services Filters & bulk toolbar
    document.getElementById('service-filter')?.addEventListener('change', (e) => {
        state.svcStateFilter = e.target.value;
        renderServices(servicesCache);
    });
    document.getElementById('service-search')?.addEventListener('input', (e) => {
        state.svcSearch = e.target.value;
        clearTimeout(serviceSearchTimer);
        serviceSearchTimer = setTimeout(() => renderServices(servicesCache), 120);
    });
    document.getElementById('svc-sort')?.addEventListener('change', (e) => {
        state.svcSort = e.target.value;
        renderServices(servicesCache);
    });
    document.getElementById('svc-refresh-select')?.addEventListener('change', (e) => {
        setSvcAutoRefresh(e.target.value);
    });
    document.getElementById('refresh-services-btn')?.addEventListener('click', () => fetchServices(true));
    document.getElementById('svc-bulk-start')?.addEventListener('click', () => bulkSvcAction('start'));
    document.getElementById('svc-bulk-stop')?.addEventListener('click', () => bulkSvcAction('stop'));
    document.getElementById('svc-bulk-restart')?.addEventListener('click', () => bulkSvcAction('restart'));
    document.getElementById('svc-bulk-reload')?.addEventListener('click', () => bulkSvcAction('reload'));
    document.getElementById('svc-bulk-enable')?.addEventListener('click', () => bulkSvcAction('enable'));
    document.getElementById('svc-bulk-disable')?.addEventListener('click', () => bulkSvcAction('disable'));
    document.getElementById('svc-bulk-clear')?.addEventListener('click', clearSvcSelection);

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



    // Process Modal Close (original)
    document.getElementById('close-modal')?.addEventListener('click', () => {
        document.getElementById('process-modal').classList.remove('show');
    });
    document.getElementById('process-modal')?.addEventListener('click', (e) => {
        if (e.target === document.getElementById('process-modal')) {
            e.target.classList.remove('show');
        }
    });

    setupModalAccessibility();

    // Layout Width Toggle (Fluid vs Capped Centered)
    const layoutToggleBtn = document.getElementById('layout-toggle-btn');
    if (layoutToggleBtn) {
        const setFluid = (isFluid) => {
            document.body.classList.toggle('layout-fluid', isFluid);
            layoutToggleBtn.setAttribute('aria-pressed', isFluid);
            layoutToggleBtn.title = isFluid ? "Switch to Centered Layout" : "Switch to Fluid Layout";
            // Dispatch resize to trigger charts redraw
            window.dispatchEvent(new Event('resize'));
        };
        
        // Initial load from localStorage
        const isFluidSaved = localStorage.getItem('monitorx-layout-fluid') === 'true';
        setFluid(isFluidSaved);
        
        layoutToggleBtn.addEventListener('click', () => {
            const currentFluid = document.body.classList.contains('layout-fluid');
            const nextFluid = !currentFluid;
            localStorage.setItem('monitorx-layout-fluid', nextFluid);
            setFluid(nextFluid);
        });
    }

    // Startup initialization. The WebSocket sends the first snapshot; keep a
    // delayed REST fallback only for blocked/very slow WebSocket upgrades.
    connectWebSocket();
    setTimeout(() => { if (!statsData) fetchStats(); }, 5000);
    fetchVmCapabilities();
    setVmAutoRefresh(document.getElementById('vm-refresh-select')?.value ?? 2);
    // Defer services loading until user visits the tab (avoids blocking on
    // systemctl subprocesses at page load, which can take 10+ seconds when
    // systemd is slow or unavailable).
    setSvcAutoRefresh(document.getElementById('svc-refresh-select')?.value ?? 10);
});
