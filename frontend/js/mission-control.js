/* ============================================================================
   MonitorX — MISSION CONTROL (v2.4)
   Flight-control GO/NO-GO poll board driven by live telemetry.

   Nine flight-controller stations (BOOSTER, GUIDO, TELMU, INCO, EECOM,
   FIDO, SURGEON, GC, CAPCOM) each vote GO / CAUTION / NO-GO from real
   metrics. The worst vote sets the MISSION STATUS lamp; any NO-GO raises
   the MASTER ALARM.

   Pure progressive enhancement:
     - Opens its own WebSocket to /ws (never touches app.js state).
     - Every DOM access is guarded; if the board markup is missing the
       module silently no-ops and the dashboard keeps working.
     - Datalink loss degrades stations to STANDBY instead of failing.
   ============================================================================ */
(function () {
    'use strict';

    var board = document.getElementById('mission-board');
    var stationsEl = document.getElementById('mission-stations');
    var lampEl = document.getElementById('ms-lamp');
    var textEl = document.getElementById('ms-text');
    var subEl = document.getElementById('mission-board-sub');
    var doyEl = document.getElementById('doy-clock');
    if (!board || !stationsEl) return;   // markup absent — no-op

    /* ── Station definitions ───────────────────────────────────────────── */
    var STATIONS = [
        { id: 'booster', call: 'BOOSTER', name: 'Storage · Propellant' },
        { id: 'guido',   call: 'GUIDO',   name: 'CPU · Guidance' },
        { id: 'telmu',   call: 'TELMU',   name: 'Memory · Life Support' },
        { id: 'inco',    call: 'INCO',    name: 'Network · Comms' },
        { id: 'eecom',   call: 'EECOM',   name: 'Thermal · Electrical' },
        { id: 'fido',    call: 'FIDO',    name: 'Processes · Flight Dyn' },
        { id: 'surgeon', call: 'SURGEON', name: 'GPU · Accelerators' },
        { id: 'gc',      call: 'GC',      name: 'Services · Ground Ctrl' },
        { id: 'capcom',  call: 'CAPCOM',  name: 'Datalink · WebSocket' }
    ];

    var LEVEL = { STANDBY: 0, GO: 1, CAUTION: 2, NOGO: 3 };
    var LEVEL_WORD = { STANDBY: 'STANDBY', GO: 'GO', CAUTION: 'CAUTION', NOGO: 'NO-GO' };

    /* ── Build station cards once ──────────────────────────────────────── */
    var cards = {};
    (function build() {
        var frag = document.createDocumentFragment();
        STATIONS.forEach(function (s) {
            var card = document.createElement('div');
            card.className = 'mc-station standby';
            card.id = 'mc-station-' + s.id;
            card.setAttribute('role', 'listitem');
            card.innerHTML =
                '<div class="mc-st-top">' +
                    '<span class="mc-call">' + s.call + '</span>' +
                    '<span class="mc-lamp" aria-hidden="true"></span>' +
                '</div>' +
                '<div class="mc-name">' + s.name + '</div>' +
                '<div class="mc-value">--</div>' +
                '<div class="mc-detail">awaiting telemetry</div>' +
                '<div class="mc-status-word">STANDBY</div>';
            frag.appendChild(card);
            cards[s.id] = {
                el: card,
                value: card.querySelector('.mc-value'),
                detail: card.querySelector('.mc-detail'),
                word: card.querySelector('.mc-status-word')
            };
        });
        stationsEl.appendChild(frag);
    })();

    function paint(id, level, value, detail) {
        var c = cards[id];
        if (!c) return;
        c.el.className = 'mc-station ' + level.toLowerCase();
        c.value.textContent = value;
        c.detail.textContent = detail || '';
        c.word.textContent = LEVEL_WORD[level];
    }

    /* ── Threshold helpers ─────────────────────────────────────────────── */
    function band(pct, caution, nogo) {
        if (pct == null || isNaN(pct)) return 'STANDBY';
        if (pct >= nogo) return 'NOGO';
        if (pct >= caution) return 'CAUTION';
        return 'GO';
    }
    function fmt1(n) { return (n == null || isNaN(n)) ? '--' : (Math.round(n * 10) / 10).toString(); }

    /* ── Telemetry state ───────────────────────────────────────────────── */
    var lastFrameAt = 0;
    var ws = null;
    var wsReconnectTimer = null;
    var prevNetErrs = null;      // cumulative err+drop counters (for rate)
    var netErrRate = 0;          // errors+drops per second
    var zombieCount = null;      // from bottlenecks poll (null = unknown)
    var failedServices = null;   // from services poll (null = unknown)
    var servicesError = false;

    /* ── Per-frame evaluators (run on every WS snapshot) ──────────────── */
    function evalBooster(d) {
        var parts = (d.disk && d.disk.partitions) || [];
        if (!parts.length) return paint('booster', 'STANDBY', '--', 'no partitions reported');
        var worst = parts[0];
        parts.forEach(function (p) {
            var load = Math.max(p.percent || 0, p.inode_percent || 0);
            var worstLoad = Math.max(worst.percent || 0, worst.inode_percent || 0);
            if (load > worstLoad) worst = p;
        });
        var pct = Math.max(worst.percent || 0, worst.inode_percent || 0);
        paint('booster', band(pct, 80, 92), fmt1(pct) + '%',
              'peak at ' + (worst.mountpoint || worst.device || '?'));
    }

    function evalGuido(d) {
        var cpu = d.cpu || {};
        var pct = cpu.percent_total != null ? cpu.percent_total : cpu.percent;
        paint('guido', band(pct, 75, 90), fmt1(pct) + '%',
              'load 1m ' + fmt1(cpu.load_1min) + ' · ' + (cpu.count_logical || '?') + ' cores');
    }

    function evalTelmu(d) {
        var mem = d.memory || {};
        var pct = mem.percent;
        var swap = mem.swap_percent || 0;
        var level = band(pct, 80, 92);
        if (swap >= 80) level = 'NOGO';
        else if (swap >= 50 && LEVEL[level] < LEVEL.CAUTION) level = 'CAUTION';
        paint('telmu', level, fmt1(pct) + '%', 'swap ' + fmt1(swap) + '% used');
    }

    function evalInco(d) {
        var ifs = (d.network && d.network.interfaces) || {};
        var errs = 0;
        Object.keys(ifs).forEach(function (k) {
            var i = ifs[k] || {};
            errs += (i.errin || 0) + (i.errout || 0) + (i.dropin || 0) + (i.dropout || 0);
        });
        var now = Date.now();
        if (prevNetErrs !== null) {
            var dt = (now - prevNetErrs.t) / 1000;
            if (dt > 0.5) netErrRate = Math.max(0, (errs - prevNetErrs.n)) / dt;
        }
        prevNetErrs = { n: errs, t: now };
        var level = netErrRate > 0.5 ? 'NOGO' : (netErrRate > 0.05 ? 'CAUTION' : 'GO');
        var rx = d.network ? d.network.rx_bytes_sec : 0;
        var tx = d.network ? d.network.tx_bytes_sec : 0;
        paint('inco', level, fmt1(netErrRate) + ' err/s',
              'rx ' + humanBytes(rx) + '/s · tx ' + humanBytes(tx) + '/s');
    }

    function evalEecom(d) {
        var t = d.thermal;
        if (!t || !t.available || t.peak_c == null) {
            return paint('eecom', 'STANDBY', 'N/A', 'no sensors exposed');
        }
        paint('eecom', band(t.peak_c, 75, 90), fmt1(t.peak_c) + '°C',
              (t.temperatures ? t.temperatures.length : 0) + ' zones · ' +
              (t.fans ? t.fans.length : 0) + ' fans');
    }

    function evalFido(d) {
        // Primary signal: full zombie scan from the bottlenecks poll.
        if (zombieCount !== null) {
            var level = zombieCount === 0 ? 'GO' : (zombieCount <= 4 ? 'CAUTION' : 'NOGO');
            return paint('fido', level, zombieCount + ' stuck',
                          zombieCount === 0 ? 'trajectory clean' : 'zombie/hung processes detected');
        }
        // Fallback before first poll: scan the top-process sample.
        var procs = d.processes || [];
        var z = 0;
        procs.forEach(function (p) { if (p && /zombie/i.test(p.status || '')) z++; });
        paint('fido', z === 0 ? 'GO' : (z <= 4 ? 'CAUTION' : 'NOGO'),
              z + ' in sample', 'full sweep pending');
    }

    function evalSurgeon(d) {
        var gpus = d.gpu;
        if (!gpus || !gpus.length) return paint('surgeon', 'STANDBY', 'N/A', 'no GPU hardware');
        var util = 0, memMax = 0;
        gpus.forEach(function (g) {
            var u = g.utilization != null ? g.utilization : (g.gpu_util != null ? g.gpu_util : g.util);
            if (u != null) util = Math.max(util, u);
            if (g.memory_percent != null) memMax = Math.max(memMax, g.memory_percent);
        });
        paint('surgeon', band(util, 85, 96), fmt1(util) + '%',
              gpus.length + ' device(s) · vmem ' + fmt1(memMax) + '%');
    }

    function evalGc() {
        if (failedServices === null) {
            return paint('gc', 'STANDBY', '--', servicesError ? 'bus unreachable' : 'poll pending');
        }
        if (failedServices === 0) return paint('gc', 'GO', '0 failed', 'all units nominal');
        paint('gc', 'NOGO', failedServices + ' failed', 'systemd units need attention');
    }

    function evalCapcom() {
        var open = ws && ws.readyState === WebSocket.OPEN;
        var age = Date.now() - lastFrameAt;
        if (open && lastFrameAt && age < 8000) {
            paint('capcom', 'GO', 'LOCKED', 'frame age ' + (age / 1000).toFixed(1) + 's');
        } else if (open) {
            paint('capcom', 'CAUTION', 'DEGRADED', 'no frames for ' + Math.round(age / 1000) + 's');
        } else {
            paint('capcom', 'NOGO', 'LOS', 'signal lost · reacquiring');
        }
    }

    function evaluateFrame(d) {
        evalBooster(d); evalGuido(d); evalTelmu(d); evalInco(d);
        evalEecom(d); evalFido(d); evalSurgeon(d); evalGc(); evalCapcom();
        paintMissionStatus();
    }

    /* ── Mission status rollup + MASTER ALARM ──────────────────────────── */
    var lastAnnounced = '';
    function paintMissionStatus() {
        var worst = 'GO', caution = 0, nogo = 0, standby = 0;
        STATIONS.forEach(function (s) {
            var cls = cards[s.id].el.className;
            if (cls.indexOf('nogo') !== -1) { nogo++; }
            else if (cls.indexOf('caution') !== -1) { caution++; }
            else if (cls.indexOf('standby') !== -1) { standby++; }
        });
        worst = nogo ? 'NOGO' : (caution ? 'CAUTION' : 'GO');

        board.classList.remove('mission-go', 'mission-caution', 'mission-nogo');
        var msg;
        if (worst === 'NOGO') {
            board.classList.add('mission-nogo');
            msg = 'MASTER ALARM · NO-GO AT ' + nogo + ' STATION' + (nogo > 1 ? 'S' : '');
        } else if (worst === 'CAUTION') {
            board.classList.add('mission-caution');
            msg = 'CAUTION · ' + caution + ' STATION' + (caution > 1 ? 'S' : '') + ' OFF-NOMINAL';
        } else {
            board.classList.add('mission-go');
            msg = standby === STATIONS.length ? 'ACQUIRING TELEMETRY' : 'ALL STATIONS GO · MISSION NOMINAL';
        }
        if (lampEl) lampEl.className = 'ms-lamp ' + worst.toLowerCase();
        if (textEl) textEl.textContent = msg;
        if (subEl) {
            var stamp = new Date();
            subEl.textContent = 'POLL ' + stamp.toISOString().substr(11, 8) + 'Z · ' +
                                STATIONS.length + ' FLIGHT STATIONS REPORTING';
        }
        // Screen-reader announcement only when the rollup itself changes.
        if (msg !== lastAnnounced) {
            lastAnnounced = msg;
            board.setAttribute('aria-label', 'Flight control loop status: ' + msg);
        }
    }

    /* ── Datalink (own WebSocket, isolated from app.js) ────────────────── */
    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        try {
            var proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(proto + '://' + window.location.host + '/ws');
        } catch (e) { scheduleReconnect(); return; }
        ws.onmessage = function (ev) {
            lastFrameAt = Date.now();
            try { evaluateFrame(JSON.parse(ev.data)); } catch (e) { /* ignore bad frame */ }
        };
        ws.onopen = function () { evalCapcom(); };
        ws.onclose = function () { evalCapcom(); scheduleReconnect(); };
        ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }
    function scheduleReconnect() {
        if (wsReconnectTimer) return;
        wsReconnectTimer = setTimeout(function () { wsReconnectTimer = null; connect(); }, 3000);
    }

    /* CAPCOM freshness re-check between frames (cheap, 2s cadence) */
    setInterval(function () { evalCapcom(); paintMissionStatus(); }, 2000);

    /* ── Slow polls: full zombie sweep + failed systemd units ──────────── */
    function pollBottlenecks() {
        fetch('/api/troubleshoot/bottlenecks').then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.json();
        }).then(function (d) {
            var stuck = (d && d.stuck_processes) || [];
            zombieCount = stuck.length;
            if (lastFrameAt) evalFido({ processes: [] });
            paintMissionStatus();
        }).catch(function () { /* keep previous value */ });
    }

    function pollServices() {
        fetch('/api/services').then(function (r) {
            if (!r.ok) return r.json().then(function (b) { throw new Error(b && b.detail || ('http ' + r.status)); });
            return r.json();
        }).then(function (data) {
            servicesError = false;
            var units = Array.isArray(data) ? data : (data.services || []);
            failedServices = 0;
            units.forEach(function (u) {
                var sub = String(u.sub_state || u.substate || '').toLowerCase();
                var active = String(u.active_state || u.activestate || '').toLowerCase();
                if (sub === 'failed' || active === 'failed') failedServices++;
            });
            evalGc(); paintMissionStatus();
        }).catch(function () {
            servicesError = true; failedServices = null;
            evalGc(); paintMissionStatus();
        });
    }

    pollBottlenecks(); setInterval(pollBottlenecks, 15000);
    pollServices(); setInterval(pollServices, 30000);

    /* ── DOY mission clock (UTC day-of-year, NASA ground-console format) ── */
    function pad(n, w) { w = w || 2; return String(n).padStart(w, '0'); }
    function tickDoy() {
        if (!doyEl) return;
        var d = new Date();
        var start = Date.UTC(d.getUTCFullYear(), 0, 1);
        var doy = Math.floor((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - start) / 86400000) + 1;
        doyEl.textContent = pad(doy, 3) + '/' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
    }
    tickDoy(); setInterval(tickDoy, 1000);

    /* ── Helpers ───────────────────────────────────────────────────────── */
    function humanBytes(n) {
        if (n == null || isNaN(n)) return '0 B';
        var k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        var i = Math.min(sizes.length - 1, Math.floor(Math.log(Math.max(1, n)) / Math.log(k)));
        return (n / Math.pow(k, i)).toFixed(i ? 1 : 0) + ' ' + sizes[i];
    }

    connect();
})();
