/* Local operations intelligence: incident triage, history and operator preferences. */
(() => {
  const api = '/api/operations';
  const el = id => document.getElementById(id);
  let overview;
  const esc = value => { const d = document.createElement('div'); d.textContent = value ?? ''; return d.innerHTML; };

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
    el('ops-incidents').innerHTML = open.length ? open.map(i => `<div class="ops-incident ${i.severity}"><span><b>${esc(i.title)}</b> · ${Number(i.value).toFixed(1)}% <small>opened ${new Date(i.timestamp).toLocaleTimeString()}</small></span><button class="btn btn-sm btn-outline" data-ack="${i.id}">Acknowledge</button></div>`).join('') : '<div class="ops-empty">✓ No active threshold incidents. Continue monitoring live telemetry below.</div>';
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
  async function addRule() {
    const metric = prompt('Metric to monitor: cpu, memory, disk, net_rx, or net_tx', 'cpu'); if (!metric) return;
    const threshold = Number(prompt('Trigger threshold (percentage for CPU/memory/disk; bytes/s for network)', '90')); if (!Number.isFinite(threshold)) return;
    const name = prompt('Rule name', `${metric} threshold`) || `${metric} threshold`;
    const res = await fetch(`${api}/alert-rules`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, metric, threshold, cooldown_minutes:15, enabled:true})});
    if (!res.ok) return alert('Could not create this rule. Check the metric name and threshold.'); load();
  }
  document.addEventListener('DOMContentLoaded', () => {
    el('ops-range').onchange = load; el('ops-rule-btn').onclick = addRule;
    const focus = localStorage.getItem('monitorx-focus') === 'true'; document.body.classList.toggle('focus-mode', focus); el('focus-mode-btn').setAttribute('aria-pressed', focus);
    el('focus-mode-btn').onclick = () => { const active = document.body.classList.toggle('focus-mode'); localStorage.setItem('monitorx-focus', active); el('focus-mode-btn').setAttribute('aria-pressed', active); };
    load(); setInterval(load, 30000); window.addEventListener('resize', () => overview && draw(overview.history));
  });
})();
