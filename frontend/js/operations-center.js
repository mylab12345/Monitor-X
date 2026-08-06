/* Local operations intelligence: incident triage, history, operator preferences,
   and alert-rule / webhook management. */
(() => {
  const api = '/api/operations';
  const el = id => document.getElementById(id);
  let overview;

  const esc = value => { const d = document.createElement('div'); d.textContent = value ?? ''; return d.innerHTML; };
  const openModal = id => { const m = document.getElementById(id); if (m) m.classList.add('show'); };
  const closeModal = id => { const m = document.getElementById(id); if (m) m.classList.remove('show'); };
  const closeModals = () => document.querySelectorAll('.modal.show').forEach(m => m.classList.remove('show'));

  async function load() {
    const range = el('ops-range').value;
    try {
      const res = await fetch(`${api}/overview?range=${range}`);
      if (!res.ok) throw new Error('Operations history unavailable');
      overview = await res.json(); render();
    } catch (err) { el('ops-summary').textContent = err.message; }
  }
  function render() {
    const open = overview.incidents.filter(x => x.status === 'open');
    el('ops-summary').textContent = open.length ? `${open.length} item${open.length === 1 ? '' : 's'} need attention now` : 'All monitored thresholds are within their configured limits';
    el('ops-incidents').innerHTML = open.length ? open.map(i => `<div class="ops-incident ${i.severity}"><span><b>${esc(i.title)}</b> · ${Number(i.value).toFixed(1)}% <small>opened ${new Date(i.timestamp).toLocaleTimeString()}</small></span><button type="button" class="btn btn-sm btn-outline" data-ack="${i.id}">Acknowledge</button></div>`).join('') : '<div class="ops-empty">✓ No active threshold incidents. Continue monitoring live telemetry below.</div>';
    el('ops-incidents').querySelectorAll('[data-ack]').forEach(b => b.onclick = async () => { await fetch(`${api}/incidents/${b.dataset.ack}/acknowledge`, {method:'POST'}); load(); });
    draw(overview.history);
  }
  function draw(rows) {
    const canvas = el('ops-history-chart'), ctx = canvas.getContext('2d'), rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width * devicePixelRatio)); canvas.height = Math.floor(rect.height * devicePixelRatio); ctx.scale(devicePixelRatio, devicePixelRatio);
    ctx.clearRect(0,0,rect.width,rect.height); if (!rows.length) { ctx.fillStyle='#9aa9b9'; ctx.fillText('Collecting historical samples…', 12, 30); return; }
    const keys = [['cpu','#43b8ff'],['memory','#58d68d'],['disk','#bd83ff']];
    keys.forEach(([key,color]) => { ctx.beginPath(); rows.forEach((r,n) => { const x = n / Math.max(1,rows.length-1) * rect.width, y = rect.height - 7 - Math.min(100, r[key] || 0) / 100 * (rect.height-14); n ? ctx.lineTo(x,y) : ctx.moveTo(x,y); }); ctx.strokeStyle=color; ctx.lineWidth=1.7; ctx.stroke(); });
    el('ops-detail').innerHTML = `<span style="color:#43b8ff">— CPU</span> &nbsp;<span style="color:#58d68d">— Memory</span> &nbsp;<span style="color:#bd83ff">— Disk</span> · ${rows.length.toLocaleString()} samples in selected range · retained locally for 30 days`;
  }

  /* ── Alert rules manager ─────────────────────────────────────────────── */
  async function openRules() {
    openModal('rules-modal');
    await Promise.all([loadRules(), loadWebhook()]);
  }
  async function loadRules() {
    const box = el('rules-list'); if (!box) return;
    try {
      const res = await fetch(`${api}/alert-rules`);
      const rules = await res.json();
      if (!rules.length) { box.innerHTML = '<p class="no-data">No alert rules configured. Create one below.</p>'; return; }
      box.innerHTML = rules.map(r => `
        <div class="rule-item ${r.enabled ? '' : 'disabled'}">
          <span class="rule-toggle" title="${r.enabled ? 'Click to disable' : 'Click to enable'}">${r.enabled ? '🟢' : '⚪'}</span>
          <span class="rule-meta"><b>${esc(r.name)}</b><small>${esc(r.metric)} ${esc(r.operator)} ${r.threshold} · cooldown ${r.cooldown_minutes}m</small></span>
          <button type="button" class="btn btn-sm btn-outline" data-rule-toggle="${r.id}">${r.enabled ? 'Disable' : 'Enable'}</button>
          <button type="button" class="btn btn-sm btn-danger" data-rule-del="${r.id}">Delete</button>
        </div>`).join('');
      box.querySelectorAll('[data-rule-toggle]').forEach(b => b.onclick = async () => {
        const id = b.dataset.ruleToggle;
        const cur = rules.find(r => r.id === id);
        await fetch(`${api}/alert-rules/${id}`, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ enabled: !cur.enabled }) });
        loadRules();
      });
      box.querySelectorAll('[data-rule-del]').forEach(b => b.onclick = async () => {
        const id = b.dataset.ruleDel;
        if (!confirm('Delete this alert rule?')) return;
        await fetch(`${api}/alert-rules/${id}`, { method: 'DELETE' });
        loadRules();
      });
    } catch (e) { box.innerHTML = '<p class="no-data">Could not load alert rules.</p>'; }
  }
  async function createRule() {
    const name = (el('rule-name').value || '').trim();
    const metric = el('rule-metric').value;
    const threshold = Number(el('rule-threshold').value);
    const cooldown = Number(el('rule-cooldown').value || 15);
    if (!name) { alert('Enter a rule name'); return; }
    if (!Number.isFinite(threshold) || threshold < 0) { alert('Enter a valid threshold'); return; }
    const res = await fetch(`${api}/alert-rules`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ name, metric, threshold, cooldown_minutes: cooldown, enabled: true }) });
    if (!res.ok) { alert('Could not create this rule. Check the metric name and threshold.'); return; }
    el('rule-name').value = ''; el('rule-threshold').value = ''; el('rule-cooldown').value = '15';
    await loadRules();
  }

  /* ── Webhook config ──────────────────────────────────────────────────── */
  async function loadWebhook() {
    try {
      const res = await fetch(`${api}/webhook`);
      const cfg = await res.json();
      el('webhook-url').value = cfg.url || '';
      el('webhook-enabled').checked = !!cfg.enabled;
    } catch (e) { /* ignore */ }
  }
  async function saveWebhook() {
    const cfg = { url: (el('webhook-url').value || '').trim(), enabled: el('webhook-enabled').checked };
    const res = await fetch(`${api}/webhook`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg) });
    if (res.ok) alert(cfg.enabled && cfg.url ? 'Webhook saved and enabled.' : 'Webhook disabled.');
    else alert('Could not save webhook configuration.');
  }
  async function testWebhook() {
    const btn = el('webhook-test-btn');
    btn.disabled = true; btn.textContent = 'Sending…';
    try {
      const res = await fetch(`${api}/webhook/test`, { method: 'POST' });
      const body = await res.json();
      alert(res.ok ? body.message : `Test failed: ${body.detail || 'unknown error'}`);
    } catch (e) { alert('Test failed: could not reach server.'); }
    btn.disabled = false; btn.textContent = 'Send test';
  }

  document.addEventListener('DOMContentLoaded', () => {
    el('ops-range').onchange = load;
    el('ops-rule-btn').onclick = openRules;
    el('rules-modal-close').onclick = () => closeModal('rules-modal');
    document.getElementById('rules-modal')?.addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal('rules-modal'); });
    el('rule-create-btn').onclick = createRule;
    el('webhook-save-btn').onclick = saveWebhook;
    el('webhook-test-btn').onclick = testWebhook;

    const focus = localStorage.getItem('monitorx-focus') === 'true'; document.body.classList.toggle('focus-mode', focus); el('focus-mode-btn').setAttribute('aria-pressed', focus);
    el('focus-mode-btn').onclick = () => { const active = document.body.classList.toggle('focus-mode'); localStorage.setItem('monitorx-focus', active); el('focus-mode-btn').setAttribute('aria-pressed', active); };
    load(); setInterval(load, 30000); window.addEventListener('resize', () => overview && draw(overview.history));
  });
})();
