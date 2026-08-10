/* Smoke test: root-only storage card + Processes tab render coalescing.
   Run: node tests/smoke-rootstorage-processes.js  (requires jsdom: npm i --no-save jsdom)
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'frontend/js/app.js'), 'utf8');

const failures = [];
const assert = (cond, msg) => {
    if (!cond) failures.push(msg);
    else console.log('  ✓', msg);
};

const mockProcesses = [
    { pid: 101, name: 'alpha', cpu_percent: 12.5, memory_percent: 3.2, memory_mb: 120.5, status: 'running', username: 'root', threads: 4, create_time: '2026-08-10 01:00:00' },
    { pid: 202, name: 'beta', cpu_percent: 4.1, memory_percent: 1.1, memory_mb: 55.0, status: 'sleeping', username: 'www-data', threads: 1, create_time: '2026-08-10 01:05:00' },
    { pid: 303, name: 'gamma', cpu_percent: 0.0, memory_percent: 0.4, memory_mb: 12.0, status: 'zombie', username: 'root', threads: 1, create_time: '2026-08-10 01:10:00' },
];

const makeFrame = (diskPercent) => ({
    timestamp: '2026-08-10T04:00:00',
    cpu: { percent_total: 3.3, percent_per_core: [2, 4, 3, 4], count_logical: 4, load_1min: 0.1, load_5min: 0.2, load_15min: 0.3, frequency_current: 2400 },
    memory: { percent: 42, used: 1 << 30, available: 2 << 30, buffers: 1 << 28, cached: 1 << 28, swap_percent: 1 },
    disk: {
        root: {
            device: '/dev/vda', mountpoint: '/', fstype: 'ext4',
            total: 20 * 1024 ** 3, used: diskPercent / 100 * 20 * 1024 ** 3,
            free: (100 - diskPercent) / 100 * 20 * 1024 ** 3, percent: diskPercent,
            inode_total: 5714800, inode_used: 25565, inode_free: 5689235, inode_percent: 0.4,
        },
        partitions: [],
        read_bytes_sec: 1024, write_bytes_sec: 2048,
    },
    network: { interfaces: {}, connections_count: 3, rx_bytes_sec: 100, tx_bytes_sec: 200 },
    gpu: null, processes: mockProcesses, system: { hostname: 'testhost', uptime_str: '1:00:00', platform: 'Linux' },
    vms: null, thermal: { available: false },
});

(async () => {
    const dom = new JSDOM(html, { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true });
    const { window } = dom;

    // Minimal browser API stubs ------------------------------------------------
    window.fetch = async (url) => {
        if (String(url).startsWith('/api/stats/processes')) {
            return { ok: true, json: async () => mockProcesses };
        }
        return { ok: false, status: 404, json: async () => ({ detail: 'not found' }) };
    };
    const fakeGradient = { addColorStop() { } };
    const fakeCtx = new Proxy({}, {
        get: (t, prop) => {
            if (prop === 'createLinearGradient') return () => fakeGradient;
            if (prop === 'canvas') return {};
            return () => { };
        },
        set: () => true,
    });
    window.HTMLCanvasElement.prototype.getContext = () => fakeCtx;

    // Evaluate the real app.js inside the page. `const state` does not leak to
    // the global object from eval'd code, so export a reference in-band.
    window.eval(appJs + '\n;window.__appState = state;');
    window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true }));
    await new Promise(r => setTimeout(r, 50));

    const $ = (sel) => window.document.querySelector(sel);

    console.log('— Root storage card —');
    const frame = makeFrame(4.4);
    window.eval(`updateDashboard(${JSON.stringify(frame)})`);

    assert($('#disk-percent').textContent === '4.4%', `#disk-percent shows root usage (${$('#disk-percent').textContent})`);
    assert(!!$('.root-disk'), 'disk card renders the root filesystem block');
    const diskHtml = $('#disk-list').innerHTML;
    assert(diskHtml.includes('Free:'), 'block shows free space figure');
    assert(diskHtml.includes('Inodes'), 'block shows inode usage block');
    assert(diskHtml.includes('/dev/vda'), 'block labels the root device');
    assert(!diskHtml.includes('disk-item'), 'no per-partition rows remain (root-only)');
    assert($('#disk-read-speed').textContent === '1 KB/s', 'I/O read speed still rendered');

    const sigBefore = $('#disk-list').dataset.sig;
    const htmlBefore = $('#disk-list').innerHTML;
    const sameFrame = makeFrame(4.4);
    window.eval(`updateDashboard(${JSON.stringify(sameFrame)})`);
    assert($('#disk-list').dataset.sig === sigBefore && $('#disk-list').innerHTML === htmlBefore,
        'identical frame skips the disk DOM rebuild (smooth telemetry)');

    window.eval(`updateDashboard(${JSON.stringify(makeFrame(9.9))})`);
    assert($('#disk-percent').textContent === '9.9%' && $('#disk-list').dataset.sig !== sigBefore,
        'changed frame re-renders the disk block');

    console.log('— Processes tab —');
    await window.eval('switchTab("processes")');
    await new Promise(r => setTimeout(r, 50));
    let rows = window.document.querySelectorAll('#all-processes-body tr');
    assert(rows.length === 3, `process table renders all fetched rows (${rows.length})`);

    // Telemetry tick with identical data must not rebuild the table.
    const tbodyHtml = $('#all-processes-body').innerHTML;
    window.eval(`updateDashboard(${JSON.stringify(makeFrame(4.4))})`);
    assert($('#all-processes-body').innerHTML === tbodyHtml, 'telemetry tick with unchanged data skips table rebuild');

    // Selection persists across forced re-renders.
    const firstBox = window.document.querySelector('#all-processes-body .proc-check');
    firstBox.click(); // change event -> adds to state.procSelected
    window.eval('filterProcesses(true)');
    const after = [...window.document.querySelectorAll('#all-processes-body .proc-check:checked')].map(c => c.value);
    assert(after.length === 1 && after[0] === '101', `selection survives table re-render (${after})`);

    // select-all reflects partial selection, then selects all.
    assert($('#select-all-proc').indeterminate === true, 'select-all shows indeterminate for partial selection');
    assert(Array.from(window.__appState.procSelected).join(',') === '101', 'procSelected tracks the checked PID');

    // Sorted order follows the active sort (cpu desc default: alpha first).
    rows = window.document.querySelectorAll('#all-processes-body tr td:nth-child(3)');
    assert(rows[0].textContent === 'alpha', 'default CPU sort keeps top consumer first');

    if (failures.length) {
        console.error('\nFAILURES:');
        failures.forEach(f => console.error('  ✗', f));
        process.exit(1);
    }
    console.log('\nAll root-storage & processes-tab smoke checks passed.');
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
