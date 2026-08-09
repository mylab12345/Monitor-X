/* Smoke test: services tab rewrite + themes + sizing classes.
   Run: node tests/smoke-services.js  (requires jsdom installed via npm i --no-save jsdom)
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'frontend/index.html'), 'utf8');

const mockServices = [
    { name: 'ssh.service', load: 'loaded', active: 'active', sub: 'running', description: 'OpenBSD Secure Shell server' },
    { name: 'cron.service', load: 'loaded', active: 'active', sub: 'running', description: 'Regular background program processing daemon' },
    { name: 'networking.service', load: 'loaded', active: 'failed', sub: 'failed', description: 'Raise network interfaces' },
    { name: 'apache2.service', load: 'loaded', active: 'inactive', sub: 'dead', description: 'The Apache HTTP Server' },
    { name: 'getty@tty1.service', load: 'loaded', active: 'active', sub: 'running', description: 'Getty on tty1' },
];

const failures = [];
const assert = (cond, msg) => { if (!cond) failures.push(msg); };

const dom = new JSDOM(html, {
    url: 'http://localhost/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
});

const { window } = dom;
window.fetch = async (url, opts) => {
    if (url === '/api/services/capabilities') {
        return { ok: true, status: 200, json: async () => ({ can_control: true, mode: 'sudo', message: 'Service controls are available (sudo policy).' }) };
    }
    if (url === '/api/services') {
        return { ok: true, status: 200, json: async () => mockServices };
    }
    if (url.startsWith('/api/services/')) {
        return { ok: true, status: 200, json: async () => ({ success: true, message: 'ok' }) };
    }
    if (String(url).startsWith('/api/troubleshoot/logs')) {
        return { ok: true, status: 200, json: async () => ({ logs: [{ text: 'sample log line' }] }) };
    }
    if (url === '/api/stats/vms') {
        return { status: 404, ok: false, json: async () => ({ detail: 'n/a' }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
};

window.WebSocket = class { constructor() { } close() { } };
window.matchMedia = () => ({ matches: false });
window.confirm = () => true;
window.CSS = { escape: (s) => s };
window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
window.cancelAnimationFrame = () => {};
window.IntersectionObserver = class {
    constructor(cb) { this.cb = cb; }
    observe() { }
    unobserve() { }
    disconnect() { }
};

const scripts = ['js/nasa-enhance.js', 'js/app.js', 'js/nexus-hud.js'];
for (const s of scripts) {
    const code = fs.readFileSync(path.join(ROOT, 'frontend', s), 'utf8');
    window.eval(code);
}

const doc = window.document;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    // Let startup init run (fetchServices etc.)
    await sleep(300);

    // KPIs populated
    const kpiTotal = doc.getElementById('svc-kpi-total');
    assert(kpiTotal && kpiTotal.textContent === '5', `KPI total = ${kpiTotal && kpiTotal.textContent} (want 5)`);
    assert(doc.getElementById('svc-kpi-active').textContent === '3', `KPI active = ${doc.getElementById('svc-kpi-active').textContent} (want 3)`);
    assert(doc.getElementById('svc-kpi-failed').textContent === '1', `KPI failed = ${doc.getElementById('svc-kpi-failed').textContent} (want 1)`);
    assert(doc.getElementById('svc-kpi-inactive').textContent === '1', `KPI inactive = ${doc.getElementById('svc-kpi-inactive').textContent} (want 1)`);
    assert(doc.getElementById('svc-kpi-loaded').textContent === '5', `KPI loaded = ${doc.getElementById('svc-kpi-loaded').textContent} (want 5)`);
    assert(doc.getElementById('svc-count').textContent === '5 Services', `count = ${doc.getElementById('svc-count').textContent}`);

    // Cards rendered
    const cards = doc.querySelectorAll('.svc-card');
    assert(cards.length === 5, `card count = ${cards.length} (want 5)`);
    const sshCard = doc.querySelector('.svc-card[data-svc-name="ssh.service"]');
    assert(sshCard, 'ssh.service card exists');
    assert(sshCard.classList.contains('svc-active'), 'ssh card has svc-active class');
    const failedCard = doc.querySelector('.svc-card.svc-failed');
    assert(failedCard, 'failed card present');
    assert(doc.querySelectorAll('.svc-state.active').length === 3, '3 active state pills');

    // All actions present on a running service (Stop/Restart/Reload/Enable/Disable/Logs)
    const sshActions = sshCard.querySelectorAll('[data-svc-action]');
    const actions = Array.from(sshActions).map(b => b.dataset.svcAction);
    assert(actions.includes('stop') && !actions.includes('start'), `running service actions: ${actions.join(',')}`);
    assert(['restart', 'reload', 'enable', 'disable'].every(a => actions.includes(a)), 'full action set present');
    assert(sshCard.querySelector('[data-svc-logs]'), 'logs button present');

    // Inactive service gets Start instead of Stop
    const apacheCard = doc.querySelector('.svc-card[data-svc-name="apache2.service"]');
    const apacheActions = Array.from(apacheCard.querySelectorAll('[data-svc-action]')).map(b => b.dataset.svcAction);
    assert(apacheActions.includes('start') && !apacheActions.includes('stop'), `inactive service actions: ${apacheActions.join(',')}`);

    // Filter: failed only
    doc.getElementById('service-filter').value = 'failed';
    doc.getElementById('service-filter').dispatchEvent(new window.Event('change'));
    await sleep(50);
    assert(doc.querySelectorAll('.svc-card').length === 1 && doc.querySelector('.svc-card.svc-failed'), 'failed filter shows 1 failed card');
    doc.getElementById('service-filter').value = 'all';
    doc.getElementById('service-filter').dispatchEvent(new window.Event('change'));

    // Search
    const search = doc.getElementById('service-search');
    search.value = 'ssh';
    search.dispatchEvent(new window.Event('input'));
    await sleep(300);
    assert(doc.querySelectorAll('.svc-card').length === 1, `search 'ssh' -> ${doc.querySelectorAll('.svc-card').length} cards`);
    search.value = '';
    search.dispatchEvent(new window.Event('input'));
    await sleep(300);

    // Selection + bulk bar
    const cb = doc.querySelector('.svc-checkbox');
    cb.checked = true;
    cb.dispatchEvent(new window.Event('change', { bubbles: true }));
    const bulkBar = doc.getElementById('svc-bulk-bar');
    assert(bulkBar.hidden === false, 'bulk bar visible after selection');
    assert(doc.getElementById('svc-selected-count').textContent === '1', 'selected count = 1');
    assert(doc.getElementById('svc-bulk-start').disabled === false, 'bulk start enabled');
    // Bulk dispatch (stop is destructive -> confirm modal first)
    const beforeFetch = window.fetch;
    let posted = [];
    window.fetch = async (url, opts) => {
        if (String(url).startsWith('/api/services/') && opts?.method === 'POST') {
            posted.push(String(url));
            return { ok: true, status: 200, json: async () => ({ success: true, message: 'ok' }) };
        }
        return beforeFetch(url, opts);
    };
    doc.getElementById('svc-bulk-stop').click();
    await sleep(50);
    doc.getElementById('confirm-modal-confirm').click();
    await sleep(300);
    assert(posted.some(u => u.includes('/stop')), `bulk stop posted: ${posted.join(' | ')}`);
    window.fetch = beforeFetch;

    // Theme picker contains all 13 themes
    const themeOptions = Array.from(doc.querySelectorAll('.theme-option')).map(o => o.dataset.theme);
    ['midnight','aurora','ember','forest','nebula','graphite','ocean','lagoon','meadow','desert','canyon','arctic','sakura']
        .forEach(t => assert(themeOptions.includes(t), `theme option ${t} present`));
    assert(themeOptions.length === 13, `theme options = ${themeOptions.length} (want 13)`);

    // Theme switch works for a new theme
    doc.querySelector('.theme-option[data-theme="sakura"]').click();
    await sleep(50);
    assert(doc.body.classList.contains('theme-sakura'), 'sakura theme applied to body');

    // Logs modal opens (re-query: innerHTML was rebuilt by the searches above)
    const freshSshCard = doc.querySelector('.svc-card[data-svc-name="ssh.service"]');
    freshSshCard.querySelector('[data-svc-logs]').click();
    await sleep(200);
    assert(doc.getElementById('service-logs-modal').classList.contains('show'), 'logs modal opened');
    assert(doc.getElementById('service-logs-output').textContent.includes('sample log line'), 'log content loaded');

    console.log(failures.length ? 'FAILURES:\n' + failures.join('\n') : 'ALL CHECKS PASSED ✓');
    process.exit(failures.length ? 1 : 0);
})();
