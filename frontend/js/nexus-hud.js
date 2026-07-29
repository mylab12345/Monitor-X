/* ===========================================================================
   MonitorX — NEXUS HUD ENHANCEMENTS (v2.3)
   Progressive JS layer loaded AFTER nasa-enhance.js / app.js.
   Adds (without breaking existing selectors/logic):
     - Parallax twinkling starfield
     - IntersectionObserver section reveal
     - Metric value delta flash (green/red) on numeric updates
     - Command palette (⌘K / Ctrl+K) with tabs, tools, refresh, theme
     - Keyboard shortcuts cheatsheet ('?')
     - Floating Action Buttons (scroll-to-top, Cmd)
     - Top-of-viewport scroll progress bar
     - Override toasts with new animated stack + auto-dismiss progress
     - Table row state tinting (failed/active services/zombies)
     - Gentle parallax on the starfield (mousemove)
   =========================================================================== */
(function () {
    'use strict';

    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const isMobile = matchMedia('(max-width: 768px)').matches;

    /* ── Inject new stylesheet link ──────────────────────────────────────── */
    // (already linked via index.html in this patch; no-op if missing)
    function ensureCss() {
        if (document.querySelector('link[href*="nexus-hud.css"]')) return;
        const l = document.createElement('link');
        l.rel = 'stylesheet';
        l.href = '/static/css/nexus-hud.css';
        document.head.appendChild(l);
    }
    ensureCss();

    /* ── Parallax twinkling starfield ────────────────────────────────────── */
    function injectStars() {
        if (document.querySelector('.nx-stars')) return;
        const layer = document.createElement('div');
        layer.className = 'nx-stars';
        layer.setAttribute('aria-hidden', 'true');
        const count = isMobile ? 40 : 90;
        const frag = document.createDocumentFragment();
        for (let i = 0; i < count; i++) {
            const s = document.createElement('i');
            const x = Math.random() * 100;
            const y = Math.random() * 100;
            const size = Math.random() * 1.6 + 0.6;
            const dur  = (Math.random() * 4 + 3).toFixed(2);
            const delay = (Math.random() * 5).toFixed(2);
            s.style.left = x + '%';
            s.style.top  = y + '%';
            s.style.width  = size + 'px';
            s.style.height = size + 'px';
            s.style.animationDuration = dur + 's';
            s.style.animationDelay    = delay + 's';
            s.style.opacity = (Math.random() * 0.6 + 0.2).toFixed(2);
            // Color variety
            const hue = Math.random();
            if (hue < 0.7)      s.style.background = 'rgba(200,230,255,.9)';
            else if (hue < 0.9) s.style.background = 'rgba(180,200,255,.9)';
            else                s.style.background = 'rgba(200,255,220,.9)';
            frag.appendChild(s);
        }
        layer.appendChild(frag);
        document.body.appendChild(layer);
        // Mousemove parallax
        if (!prefersReducedMotion && !isMobile) {
            let tx = 0, ty = 0, cx = 0, cy = 0;
            document.addEventListener('mousemove', (e) => {
                tx = (e.clientX / window.innerWidth  - .5) * 14;
                ty = (e.clientY / window.innerHeight - .5) * 14;
            });
            const raf = () => {
                cx += (tx - cx) * 0.05;
                cy += (ty - cy) * 0.05;
                layer.style.transform = `translate3d(${cx}px, ${cy}px, 0)`;
                requestAnimationFrame(raf);
            };
            requestAnimationFrame(raf);
        }
    }
    injectStars();

    /* ── Scroll progress bar ─────────────────────────────────────────────── */
    const prog = document.createElement('div');
    prog.className = 'nx-progress';
    prog.setAttribute('aria-hidden', 'true');
    document.body.appendChild(prog);
    function updateProgress() {
        const h = document.documentElement;
        const scrolled = h.scrollTop;
        const max = h.scrollHeight - h.clientHeight;
        const pct = max > 0 ? (scrolled / max) * 100 : 0;
        prog.style.width = pct + '%';
    }
    window.addEventListener('scroll', updateProgress, { passive: true });
    updateProgress();

    /* ── Floating Action Buttons (scroll-to-top + cmd palette) ──────────── */
    const fabTop = document.createElement('button');
    fabTop.className = 'nx-fab top';
    fabTop.title = 'Scroll to top';
    fabTop.setAttribute('aria-label', 'Scroll to top');
    fabTop.innerHTML = '▲';
    document.body.appendChild(fabTop);
    fabTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

    const fabCmd = document.createElement('button');
    fabCmd.className = 'nx-fab cmd';
    fabCmd.title = 'Command Palette (Ctrl/⌘ + K)';
    fabCmd.setAttribute('aria-label', 'Open command palette');
    fabCmd.innerHTML = '⌘<kbd>K</kbd>';
    document.body.appendChild(fabCmd);
    fabCmd.addEventListener('click', openCmdPalette);

    function toggleFabs() {
        const show = window.scrollY > 260;
        fabTop.classList.toggle('show', show);
        fabCmd.classList.add('show'); // cmd FAB always visible
    }
    fabCmd.classList.add('show');
    window.addEventListener('scroll', toggleFabs, { passive: true });
    toggleFabs();

    /* ── Section reveal (IntersectionObserver) ──────────────────────────── */
    function setupReveals() {
        const targets = document.querySelectorAll(
            '.charts-row, .metrics-grid, .containers-panel, .pods-panel, .issues-panel, .top-processes, .troubleshoot-header-card, .sub-tab-nav, .troubleshoot-grid, .vm-kpi-row, .vm-controls-bar, #vm-list, .vm-audit-panel, .service-controls, .process-controls'
        );
        targets.forEach((el, i) => {
            el.classList.add('nx-reveal');
            if (prefersReducedMotion) el.classList.add('in');
        });
        if (prefersReducedMotion) return;
        const io = new IntersectionObserver((entries) => {
            entries.forEach((e) => {
                if (e.isIntersecting) {
                    // stagger slightly
                    const delay = (Array.from(e.target.parentElement?.children || [e.target]).indexOf(e.target) % 6) * 60;
                    setTimeout(() => e.target.classList.add('in'), delay);
                    io.unobserve(e.target);
                }
            });
        }, { threshold: 0.08 });
        targets.forEach((el) => io.observe(el));
    }
    // Run on load + also after tab switches reveal new content
    document.addEventListener('DOMContentLoaded', setupReveals);
    setTimeout(setupReveals, 600);
    document.addEventListener('click', (e) => {
        const t = e.target.closest('.tab-btn, .sub-tab-btn');
        if (t) setTimeout(setupReveals, 80);
    });

    /* ── Metric value flash on numeric change ────────────────────────────── */
    function parseNumber(str) {
        if (str == null) return null;
        const m = String(str).replace(/,/g, '').match(/-?\d+(\.\d+)?/);
        return m ? parseFloat(m[0]) : null;
    }
    const trackedValues = new Map(); // id -> last number
    function scanAndFlash() {
        document.querySelectorAll('.metric-value, .chart-value').forEach((el) => {
            const id = el.id || 'el-' + Math.random().toString(36).slice(2,7);
            if (!el.id) el.dataset.tid = id;
            const key = el.id ? ('#'+el.id) : ('.'+el.dataset.tid);
            const num = parseNumber(el.textContent);
            if (num == null) return;
            const prev = trackedValues.get(key);
            if (prev != null && Math.abs(num - prev) > 0.01) {
                el.classList.remove('nx-flash-up', 'nx-flash-down');
                // force reflow to restart animation
                void el.offsetWidth;
                el.classList.add(num > prev ? 'nx-flash-up' : 'nx-flash-down');
                setTimeout(() => el.classList.remove('nx-flash-up', 'nx-flash-down'), 900);
            }
            trackedValues.set(key, num);
        });
    }
    setInterval(scanAndFlash, 2000);

    /* ── Command Palette (⌘K / Ctrl+K) ──────────────────────────────────── */
    const PALETTE = [
        { group: 'Navigation', items: [
            { label: 'Dashboard',           icon: '📊', keys: ['g','d'], action: () => switchTab('dashboard') },
            { label: 'Processes',           icon: '📋', keys: ['g','p'], action: () => switchTab('processes') },
            { label: 'Troubleshoot Hub',    icon: '🔧', keys: ['g','t'], action: () => switchTab('troubleshoot') },
            { label: 'VMs (Libvirt)',       icon: '🐳', keys: ['g','v'], action: () => switchTab('vms') },
            { label: 'Systemd Services',    icon: '⚙️', keys: ['g','s'], action: () => switchTab('services') },
        ]},
        { group: 'Actions', items: [
            { label: 'Refresh Dashboard',   icon: '🔄', keys: ['r'],     action: () => click('#refresh-btn, #refresh-containers-btn') },
            { label: 'Run Diagnostic Scan', icon: '⚡', keys: ['D'],     action: () => { switchTab('troubleshoot'); click('#run-full-scan-btn'); } },
            { label: 'Toggle Theme',        icon: '🌓', keys: ['t'],     action: () => click('#theme-toggle') },
            { label: 'Toggle Live Log Tail',icon: '📡', keys: ['L'],     action: () => { switchTab('troubleshoot'); switchSubtab('log-inspector'); toggle('#log-autotail-toggle'); } },
            { label: 'Show Keyboard Shortcuts', icon: '⌨️', keys: ['?'], action: openHelp },
        ]},
        { group: 'Troubleshoot', items: [
            { label: 'Log Inspector',       icon: '📋', action: () => { switchTab('troubleshoot'); switchSubtab('log-inspector'); } },
            { label: 'Network Suite',       icon: '🌐', action: () => { switchTab('troubleshoot'); switchSubtab('net-suite'); } },
            { label: 'Bottleneck Finder',   icon: '🔥', action: () => { switchTab('troubleshoot'); switchSubtab('bottlenecks'); } },
            { label: 'Terminal & Presets',  icon: '⚡', action: () => { switchTab('troubleshoot'); switchSubtab('command-runner'); } },
            { label: 'Health Scan & Fix',   icon: '🏥', action: () => { switchTab('troubleshoot'); switchSubtab('health-hub'); } },
        ]},
    ];

    const paletteEl = document.createElement('div');
    paletteEl.className = 'nx-cmdk';
    paletteEl.setAttribute('role', 'dialog');
    paletteEl.setAttribute('aria-modal', 'true');
    paletteEl.innerHTML = `
        <div class="nx-cmdk-panel" role="document">
            <div class="nx-cmdk-head">
                <span class="nx-cmdk-ico">⌘</span>
                <input type="text" placeholder="Type a command or search… (e.g. logs, vm, docker, refresh)" aria-label="Command search">
                <kbd>esc</kbd>
            </div>
            <div class="nx-cmdk-list"></div>
            <div class="nx-cmdk-foot">
                <span><kbd>↑↓</kbd> Navigate</span>
                <span><kbd>↵</kbd> Run</span>
                <span><kbd>⌘K</kbd> Open/Close</span>
            </div>
        </div>`;
    document.body.appendChild(paletteEl);

    const pInput = paletteEl.querySelector('input');
    const pList  = paletteEl.querySelector('.nx-cmdk-list');
    let pActive = 0;
    let pFlat = [];

    function renderPalette(query = '') {
        const q = query.trim().toLowerCase();
        pList.innerHTML = '';
        pFlat = [];
        let visibleIdx = 0;
        PALETTE.forEach(group => {
            const items = group.items.filter(it =>
                !q ||
                it.label.toLowerCase().includes(q) ||
                group.group.toLowerCase().includes(q)
            );
            if (!items.length) return;
            const g = document.createElement('div');
            g.className = 'nx-cmdk-group';
            g.textContent = group.group;
            pList.appendChild(g);
            items.forEach(it => {
                const row = document.createElement('div');
                row.className = 'nx-cmdk-item';
                row.dataset.idx = String(pFlat.length);
                const kbds = (it.keys || []).map(k => `<kbd>${k}</kbd>`).join('');
                row.innerHTML = `
                    <span class="icn">${it.icon || '▸'}</span>
                    <span class="lbl">${it.label}</span>
                    ${kbds ? `<span class="kbs">${kbds}</span>` : ''}
                `;
                row.addEventListener('click', () => { it.action(); closeCmdPalette(); });
                pList.appendChild(row);
                pFlat.push({ row, action: it.action });
                if (visibleIdx === pActive) row.classList.add('active');
                visibleIdx++;
            });
        });
        if (!pFlat.length) {
            const empty = document.createElement('div');
            empty.className = 'nx-cmdk-empty';
            empty.textContent = 'No commands match "' + query + '".';
            pList.appendChild(empty);
        }
    }
    function setActive(i) {
        if (!pFlat.length) { pActive = 0; return; }
        pActive = Math.max(0, Math.min(i, pFlat.length - 1));
        pFlat.forEach((p, idx) => p.row.classList.toggle('active', idx === pActive));
        const active = pFlat[pActive]?.row;
        if (active) active.scrollIntoView({ block: 'nearest' });
    }
    function openCmdPalette() {
        paletteEl.classList.add('open');
        pInput.value = '';
        pActive = 0;
        renderPalette('');
        setTimeout(() => pInput.focus(), 30);
    }
    function closeCmdPalette() {
        paletteEl.classList.remove('open');
    }
    pInput.addEventListener('input', () => { pActive = 0; renderPalette(pInput.value); });
    pInput.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown') { e.preventDefault(); setActive(pActive + 1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(pActive - 1); }
        else if (e.key === 'Enter') { e.preventDefault(); pFlat[pActive]?.action(); closeCmdPalette(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeCmdPalette(); }
    });
    paletteEl.addEventListener('click', (e) => { if (e.target === paletteEl) closeCmdPalette(); });
    fabCmd.addEventListener('click', openCmdPalette);

    /* ── Keyboard shortcuts ─────────────────────────────────────────────── */
    // ⌘K / Ctrl+K opens palette; '?' opens help; 'g' then key switches tab; 'r' refreshes; 't' theme
    let gPrefix = false;
    let gTimer = null;
    document.addEventListener('keydown', (e) => {
        const tag = (e.target.tagName || '').toLowerCase();
        const inField = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable;

        // Cmd/Ctrl+K always opens palette (unless in modal field? Allow too, useful)
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            paletteEl.classList.contains('open') ? closeCmdPalette() : openCmdPalette();
            return;
        }
        if (paletteEl.classList.contains('open')) return; // palette handles its own keys
        if (inField) return;

        if (e.key === '?') { e.preventDefault(); openHelp(); return; }
        if (e.key === 'Escape') { closeHelp(); return; }

        // 'g' prefix for navigation
        if (gPrefix) {
            clearTimeout(gTimer);
            gPrefix = false;
            const k = e.key.toLowerCase();
            if (k === 'd') switchTab('dashboard');
            else if (k === 'p') switchTab('processes');
            else if (k === 't') switchTab('troubleshoot');
            else if (k === 'v') switchTab('vms');
            else if (k === 's') switchTab('services');
            return;
        }
        if (e.key === 'g') {
            gPrefix = true;
            clearTimeout(gTimer);
            gTimer = setTimeout(() => { gPrefix = false; }, 900);
            return;
        }
        if (e.key.toLowerCase() === 'r' && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            click('#refresh-btn');
            showToast('Refreshing dashboard…', 'info', { icon: '🔄', duration: 1400 });
        }
        if (e.key.toLowerCase() === 't') { click('#theme-toggle'); }
        if (e.key === 'D' && !e.ctrlKey && !e.metaKey) { switchTab('troubleshoot'); click('#run-full-scan-btn'); }
    });

    /* ── Help / Keyboard shortcuts modal ────────────────────────────────── */
    const helpEl = document.createElement('div');
    helpEl.className = 'nx-help';
    helpEl.innerHTML = `
        <div class="nx-help-panel" role="dialog" aria-modal="true">
            <h3>⌨️  FLIGHT DECK SHORTCUTS</h3>
            <div class="nx-help-grid">
                <div class="row"><span>Open command palette</span><kbd>⌘ / Ctrl + K</kbd></div>
                <div class="row"><span>Show this help</span><kbd>?</kbd></div>
                <div class="row"><span>Close dialogs</span><kbd>Esc</kbd></div>
                <div class="row"><span>Refresh dashboard</span><kbd>r</kbd></div>
                <div class="row"><span>Toggle theme</span><kbd>t</kbd></div>
                <div class="row"><span>Go to Dashboard</span><kbd>g d</kbd></div>
                <div class="row"><span>Go to Processes</span><kbd>g p</kbd></div>
                <div class="row"><span>Go to Troubleshoot</span><kbd>g t</kbd></div>
                <div class="row"><span>Go to VMs</span><kbd>g v</kbd></div>
                <div class="row"><span>Go to Services</span><kbd>g s</kbd></div>
                <div class="row"><span>Run diagnostic scan</span><kbd>Shift + D</kbd></div>
                <div class="row"><span>Scroll to top</span><kbd>Home</kbd></div>
            </div>
        </div>`;
    document.body.appendChild(helpEl);
    function openHelp() { helpEl.classList.add('open'); }
    function closeHelp() { helpEl.classList.remove('open'); }
    helpEl.addEventListener('click', (e) => { if (e.target === helpEl) closeHelp(); });

    /* ── Tab / subtab helpers (uses existing UI clicks) ─────────────────── */
    function switchTab(name) {
        const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
        if (btn) btn.click();
    }
    function switchSubtab(name) {
        const btn = document.querySelector(`.sub-tab-btn[data-subtab="${name}"]`);
        if (btn) btn.click();
    }
    function click(sel) {
        const el = document.querySelector(sel);
        if (el) el.click();
    }
    function toggle(sel) {
        const el = document.querySelector(sel);
        if (el) el.click();
    }

    /* ── Toast system upgrade (wrap existing window.showToast if present) ── */
    const toastContainer = document.getElementById('toast-container') || (() => {
        const c = document.createElement('div');
        c.className = 'toast-container';
        c.id = 'toast-container';
        document.body.appendChild(c);
        return c;
    })();
    const iconMap = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
    function showToast(message, type = 'info', opts = {}) {
        const t = document.createElement('div');
        t.className = `toast ${type}`;
        const icon = opts.icon || iconMap[type] || '▸';
        const dur = opts.duration || 3200;
        t.innerHTML = `
            <span class="toast-icon">${icon}</span>
            <span class="toast-msg">${message}</span>
            <button class="toast-close" aria-label="Close">×</button>
            <span class="toast-progress" style="animation-duration:${dur}ms;color:currentColor"></span>
        `;
        toastContainer.appendChild(t);
        const close = () => {
            t.classList.add('removing');
            setTimeout(() => t.remove(), 260);
        };
        t.querySelector('.toast-close').addEventListener('click', close);
        setTimeout(close, dur);
        return t;
    }
    // Expose globally, and if app.js defined its own, use ours as primary but keep compatibility
    window.showToast = showToast;
    // Intercept any existing calls that push HTML strings to the container (defensive)
    const mo = new MutationObserver((muts) => {
        muts.forEach(m => m.addedNodes.forEach(n => {
            if (n.nodeType === 1 && n.classList.contains('toast') && !n.querySelector('.toast-progress')) {
                // upgrade legacy toast
                const msg = n.textContent.trim();
                const type = n.classList.contains('success') ? 'success' :
                             n.classList.contains('error')   ? 'error'   :
                             n.classList.contains('warning') ? 'warning' : 'info';
                n.remove();
                showToast(msg, type);
            }
        }));
    });
    mo.observe(toastContainer, { childList: true });

    /* ── Table row state tinting ─────────────────────────────────────────── */
    function tintRows() {
        // Services table: color failed/active rows
        document.querySelectorAll('#services-body tr').forEach(tr => {
            const txt = tr.textContent.toLowerCase();
            tr.classList.remove('nx-row-ok', 'nx-row-warn', 'nx-row-err');
            if (txt.includes('failed')) tr.classList.add('nx-row-err');
            else if (txt.includes('active (running)')) tr.classList.add('nx-row-ok');
            else if (txt.includes('inactive') || txt.includes('dead')) tr.classList.add('nx-row-warn');
        });
        // Processes: red for zombie state, orange for sleeping-blocked/D/uninterruptible
        document.querySelectorAll('#all-processes-body tr, #top-processes-body tr').forEach(tr => {
            const txt = tr.textContent;
            tr.classList.remove('nx-row-ok', 'nx-row-warn', 'nx-row-err');
            if (/zombie|z\b/i.test(txt)) tr.classList.add('nx-row-err');
            else if (/D\b|disk sleep|uninterruptible/i.test(txt)) tr.classList.add('nx-row-warn');
        });
        // Ports table: ok highlight
        document.querySelectorAll('#ports-table-body tr').forEach(tr => {
            tr.classList.remove('nx-row-ok', 'nx-row-warn', 'nx-row-err');
        });
    }
    setInterval(tintRows, 3500);
    setTimeout(tintRows, 1500);

    /* ── Boot sequence: fade overlay sooner so new chrome appears snappier ─ */
    setTimeout(() => {
        const boot = document.getElementById('nasa-boot');
        if (boot) boot.classList.add('done');
        setTimeout(() => boot && boot.remove(), 800);
    }, 1500);

    /* ── Welcome toast on first load ─────────────────────────────────────── */
    setTimeout(() => {
        if (sessionStorage.getItem('nx-welcomed')) return;
        sessionStorage.setItem('nx-welcomed', '1');
        showToast('NEXUS HUD online · Press ⌘/Ctrl+K for command · ? for shortcuts', 'info', { duration: 4500, icon: '🛰️' });
    }, 1800);

})();
