/* Smoke test: VM Guest Insights frontend (SSH processes/users/root-disk UI).
   Run: node tests/smoke-vm-insights.js  (requires jsdom: npm i --no-save jsdom)

   Verifies the Insights modal renders the three sections from a stubbed API,
   that a hostile guest process name cannot inject markup (XSS-escape), and
   that the fleet overview panel renders LIVE/OFFLINE cards.
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'frontend/js/app.js'), 'utf8');
const css = fs.readFileSync(path.join(ROOT, 'frontend/css/vm-insights.css'), 'utf8');

const failures = [];
const assert = (cond, msg) => {
    if (!cond) failures.push(msg);
    else console.log('  ✓', msg);
};

// --- Stubbed API payload mirroring backend/main.py collect_vm_insights() ---
const INSIGHTS_PAYLOAD = {
    vm: 'web-01', configured: true, collected_at: '2026-08-13T12:00:00', host: '10.0.0.5',
    processes: {
        ok: true, count: 3, truncated: false,
        processes: [
            { pid: 512, ppid: 1, user: 'www-data', cpu_percent: 12.5, memory_percent: 2.4, memory_mb: 200, etime: '02:11:45', name: 'nginx' },
            { pid: 900, ppid: 512, user: 'postgres', cpu_percent: 3.1, memory_percent: 5.5, memory_mb: 402, etime: '1-02:03:04', name: 'postgres' },
            // Hostile name must be rendered as text, never as markup.
            { pid: 666, ppid: 1, user: 'evil', cpu_percent: 1.0, memory_percent: 1.0, memory_mb: 10, etime: '00:00:01', name: '<img src=x onerror=alert(1)>shell' },
        ],
    },
    users: {
        ok: true,
        sessions: [{ user: 'deploy', tty: 'pts/1', login_time: '2026-08-13 10:44', from: '192.168.1.20' }],
        sessions_truncated: false,
        accounts: [{ name: 'root', uid: 0, home: '/root', shell: '/bin/bash' }, { name: 'deploy', uid: 1000, home: '/home/deploy', shell: '/bin/bash' }],
        account_entries_total: 2, accounts_truncated: false,
    },
    root_disk: {
        ok: true,
        root: { device: '/dev/vda1', mountpoint: '/', size_kb: 20511356, used_kb: 12345678, avail_kb: 7100000, percent: 64 },
        filesystems: [
            { device: '/dev/vda1', mountpoint: '/', size_kb: 20511356, used_kb: 12345678, avail_kb: 7100000, percent: 64, pseudo: false },
            { device: 'tmpfs', mountpoint: '/dev/shm', size_kb: 1024000, used_kb: 0, avail_kb: 1024000, percent: 0, pseudo: true },
        ],
        truncated: false,
    },
};

const CONFIG_PAYLOAD = {
    vm: 'web-01', configured: true,
    config: { host: '10.0.0.5', port: 22, user: 'deploy', identity_file: null },
    discovered_addresses: ['10.0.0.5'], ssh_available: true,
};

const OVERVIEW_PAYLOAD = {
    configured: 2,
    vms: [
        { vm: 'web-01', vm_name: 'web-01', ok: true, collected_at: '2026-08-13T12:00:00',
          processes: { ok: true, count: 3 }, users: { ok: true, sessions: 1, accounts: 2 },
          root_disk: { ok: true, device: '/dev/vda1', mountpoint: '/', percent: 64, size_kb: 1, used_kb: 1, avail_kb: 1 } },
        { vm: 'db-01', vm_name: 'db-01', ok: false, error: 'SSH connection failed: No route to host' },
    ],
};

(async () => {
    const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
    const { window } = dom;

    window.fetch = async (url) => {
        const u = String(url);
        if (u.includes('/insights/config')) return { ok: true, status: 200, json: async () => CONFIG_PAYLOAD };
        if (u.match(/\/api\/vms\/insights/)) return { ok: true, status: 200, json: async () => OVERVIEW_PAYLOAD };
        if (u.includes('/insights')) return { ok: true, status: 200, json: async () => INSIGHTS_PAYLOAD };
        return { ok: false, status: 404, json: async () => ({ detail: 'not found' }) };
    };
    const fakeCtx = new Proxy({}, { get: () => () => ({}), set: () => true });
    window.HTMLCanvasElement.prototype.getContext = () => fakeCtx;

    window.eval(appJs + '\n;window.__appState = state;');
    window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
    await new Promise(r => setTimeout(r, 60));
    const $ = (sel) => window.document.querySelector(sel);

    console.log('— Insights modal markup —');
    assert(!!$('#vm-insights-modal'), 'insights modal exists in the DOM');
    assert(css.includes('vm-insights-tab') && css.includes('vmi-card'), 'vm-insights.css ships the modal + overview styles');
    assert(html.includes('vm-insights-overview'), 'fleet overview panel is present on the VMs tab');

    console.log('— Open modal & render sections —');
    await window.eval('openVmInsights("web-01", "web-01")');
    await new Promise(r => setTimeout(r, 80));
    assert($('#vm-insights-modal').classList.contains('show'), 'modal opens');
    assert($('#vm-insights-host').value === '10.0.0.5', 'connection form prefilled from stored profile');

    // Processes pane (default) — with XSS escape verification.
    const procBody = $('#vm-insights-proc-body');
    assert(procBody.querySelectorAll('tr').length === 3, `process table renders 3 rows (${procBody.querySelectorAll('tr').length})`);
    assert(!procBody.querySelector('img'), 'hostile process name did NOT inject an <img> element');
    assert(procBody.innerHTML.includes('&lt;img'), 'hostile name is HTML-escaped in the table');
    assert(procBody.textContent.includes('shell'), 'hostile name still readable as plain text');

    // Users pane.
    await window.eval('switchInsightsTab("users")');
    const sessions = $('#vm-insights-sessions-body').querySelectorAll('tr').length;
    const accounts = $('#vm-insights-accounts-body').querySelectorAll('tr').length;
    assert(sessions === 1, `one login session rendered (${sessions})`);
    assert(accounts === 2, `two user accounts rendered (${accounts})`);
    assert($('#vm-insights-sessions-body').textContent.includes('192.168.1.20'), 'session source address shown');

    // Root disk pane.
    await window.eval('switchInsightsTab("root_disk")');
    const rootCard = $('#vm-insights-root-card');
    assert(rootCard.textContent.includes('64%'), 'root usage percentage rendered');
    assert(rootCard.textContent.includes('/dev/vda1'), 'root device rendered');
    const fsRows = $('#vm-insights-fs-body').querySelectorAll('tr').length;
    assert(fsRows === 1, `pseudo filesystems filtered from the table (${fsRows} real row)`);

    // Badges reflect section health.
    assert($('#vmi-badge-processes').textContent === '3', 'process count badge set');
    assert($('#vmi-badge-root-disk').textContent === '64%', 'root-disk badge set');

    console.log('— Fleet overview —');
    await window.eval('fetchVmInsightsOverview(true)');
    await new Promise(r => setTimeout(r, 60));
    const grid = $('#vm-insights-overview-grid');
    assert(grid.querySelectorAll('.vmi-card').length === 2, 'overview renders one card per configured VM');
    assert(grid.querySelectorAll('.vmi-card-status.ok').length === 1, 'healthy VM shows LIVE');
    assert(grid.querySelectorAll('.vmi-card-status.error').length === 1, 'unreachable VM shows OFFLINE');
    assert(grid.textContent.includes('No route to host'), 'overview surfaces the SSH error');

    console.log('— Error section rendering —');
    await window.eval(`renderInsightsProcesses({ ok: false, error: 'SSH connection failed: refused' })`);
    assert($('#vm-insights-proc-body').textContent.includes('SSH connection failed'), 'failed section shows its error box');

    window.eval('closeVmInsights()');
    assert(!$('#vm-insights-modal').classList.contains('show'), 'modal closes cleanly');

    if (failures.length) {
        console.error('\nFAILURES:');
        failures.forEach(f => console.error('  ✗', f));
        process.exit(1);
    }
    console.log('\nAll VM Guest Insights smoke checks passed.');
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
