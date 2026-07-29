/* ============================================================================
   MonitorX — NASA Mission-Control Enhancements (v2.2)
   Adds: MET/UTC clocks, live telemetry ticker, flight-control boot sequence.
   Pure progressive enhancement — guards every element so app.js is untouched
   and the dashboard still works if this file fails to load.
   ============================================================================ */
(function () {
    'use strict';
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var read = function (id) {
        var el = document.getElementById(id);
        return el ? el.textContent.replace(/\s+/g, ' ').trim() : '--';
    };

    /* ── Flight-control boot sequence ──────────────────────────────────── */
    var boot = document.getElementById('nasa-boot');
    var bootFill = document.getElementById('nasa-boot-fill');
    var bootStatus = document.getElementById('nasa-boot-status');
    var bootSteps = [
        'INITIALIZING SUBSYSTEMS…',
        'ESTABLISHING DATALINK…',
        'CALIBRATING TELEMETRY ARRAY…',
        'SYNCING GROUND STATION CLOCK…',
        'ALL SYSTEMS NOMINAL'
    ];
    if (boot) {
        var step = 0;
        if (bootFill) bootFill.style.width = '8%';
        var bootTimer = setInterval(function () {
            step++;
            if (step < bootSteps.length) {
                if (bootStatus) bootStatus.textContent = bootSteps[step];
                if (bootFill) bootFill.style.width = (8 + (step / (bootSteps.length - 1)) * 92) + '%';
            }
        }, 320);
        setTimeout(function () {
            clearInterval(bootTimer);
            if (bootFill) bootFill.style.width = '100%';
            if (bootStatus) bootStatus.textContent = bootSteps[bootSteps.length - 1];
            boot.classList.add('done');
            setTimeout(function () { if (boot && boot.parentNode) boot.parentNode.removeChild(boot); }, 650);
        }, 1700);
    }

    /* ── Mission Elapsed Time (MET) + UTC clock ─────────────────────────── */
    var metEl = document.getElementById('met-clock');
    var utcEl = document.getElementById('utc-clock');
    var metStart = Date.now();

    function tickClocks() {
        if (metEl) {
            var s = Math.floor((Date.now() - metStart) / 1000);
            var h = Math.floor(s / 3600); s %= 3600;
            var m = Math.floor(s / 60); s %= 60;
            metEl.textContent = 'T+ ' + pad(h) + ':' + pad(m) + ':' + pad(s);
        }
        if (utcEl) {
            var d = new Date();
            utcEl.textContent = pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
        }
    }
    tickClocks();
    setInterval(tickClocks, 1000);

    /* ── Live telemetry ticker (reads values rendered by app.js) ────────── */
    var track = document.getElementById('nasa-ticker-track');
    function buildTicker() {
        if (!track) return;
        var items = [
            ['HOST', read('hostname')],
            ['CPU', read('cpu-total')],
            ['MEM', read('ram-percent')],
            ['LOAD', read('cpu-load')],
            ['DISK', read('disk-percent')],
            ['NET ↓', read('net-rx-speed')],
            ['NET ↑', read('net-tx-speed')],
            ['GPU', read('gpu-total')],
            ['PROC', document.querySelectorAll('#top-processes-body tr').length + ' ACTIVE'],
            ['DOCKER', read('container-count')],
            ['VM', read('vm-count')],
            ['WS', read('ws-status-text')],
            ['LINK', read('uptime').replace('Uptime: ', '')]
        ];
        var html = '';
        for (var i = 0; i < items.length; i++) {
            html += '<span class="tk"><b>' + items[i][0] + '</b> ' + items[i][1] + '</span>';
        }
        // duplicate the run so the marquee loops seamlessly (-50% translate)
        track.innerHTML = html + html;
    }
    buildTicker();
    setInterval(buildTicker, 2000);

    /* ── Re-label the WebSocket status as a "datalink" when connected ───── */
    var wsText = document.getElementById('ws-status-text');
    var wsDot = document.querySelector('#ws-status .status-dot');
    function paintLink() {
        if (!wsText) return;
        var connected = wsDot && wsDot.classList.contains('connected');
        if (connected) wsText.textContent = 'DATALINK · LOCKED';
    }
    paintLink();
    setInterval(paintLink, 2000);
})();
