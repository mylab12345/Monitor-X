# Monitor-X Dashboard — Live Scan & Design Audit

**Branch:** `arena/019fd120-monitor-x`  
**Server:** Live on `0.0.0.0:8080` (PID 1472)  
**Files audited:**
- `frontend/index.html` — 41,781 bytes, 756 lines, 10 tab sections
- `frontend/css/styles.css` — 42,261 bytes, ~289 CSS selectors
- `backend/main.py` — 133,455 bytes (VM control, process kill, diagnostics, web sockets)

---

## 1. Live Preview
The dashboard is running. Open `http://localhost:8080` (or the sandbox preview port) to view the rendered glassmorphism UI with real-time canvas sparklines, troubleshooting hub, VM controls, log inspector, and service manager.

---

## 2. Design / UX Issues (with line references)

### 2.1 Accessibility — Contrast & Text Size
- **File:** `frontend/css/styles.css`
- **Lines:** ~46 (`--text-muted: #94a3b8`), ~83 (`font-size: 0.82rem` on `.metric-card` headers), ~234 (`font-size: 0.75rem` badges).
- **Issue:** `#94a3b8` on `#080d1a` yields ~4.1:1 — borderline WCAG AA for normal text. At 0.78rem it fails for small text.
- **Fix:** Change `--text-muted` to `#b8c5d6`; enforce `font-size: 0.85rem` minimum; increase line-height to `1.6` for body.

### 2.2 Gradient Text — Unmeasurable Contrast
- **File:** `frontend/css/styles.css`
- **Line:** ~117 (`.logo` with `-webkit-text-fill-color: transparent`).
- **Issue:** Screen readers may skip gradient-only text; contrast cannot be verified.
- **Fix:** Add `aria-label="MonitorX — Infrastructure observability"` to the logo link; provide a solid `color: var(--accent-blue)` fallback for `prefers-contrast: more`.

### 2.3 Color-Only Status Indicators
- **File:** `frontend/css/styles.css`
- **Lines:** ~100 (`.status-dot`), ~860 (`.vm-state` pills), ~892 (`.progress-fill`).
- **Issue:** Green/red/blue dots, progress bars, and VM pills rely 100% on hue. Colorblind users (deuteranopia/protanopia) cannot distinguish running vs stopped.
- **Fix:** Add icon prefixes (`●`/`○`/`⚠`/`✕`) inside pills; add `aria-label` with text state; use pattern stripes (diagonal hatching) on progress bars.

### 2.4 Motion & Vestibular Safety
- **File:** `frontend/css/styles.css`
- **Lines:** ~102 (`@keyframes pulse`), ~150 (`.tab-btn.active`), ~476 (`.metric-card:hover` translateY).
- **Issue:** Infinite `pulse` animation on the connection dot; `fadeIn` on tabs; hover transforms. No `prefers-reduced-motion` guard exists anywhere in the 1,569-line file.
- **Fix:** Add at top of CSS:
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  .status-dot { animation: none; }
  .metric-card:hover { transform: none; }
}
```

### 2.5 Glassmorphism Performance
- **File:** `frontend/css/styles.css`
- **Lines:** ~90 (`backdrop-filter: blur(16px)` header), ~181 (`blur(12px)` cards), ~1112 (`blur(8px)` modal).
- **Issue:** `blur()` is GPU-intensive. With 10 tabs + canvas charts + live updates, low-end integrated graphics can drop frames. Semi-transparent cards over radial gradients reduce legibility.
- **Fix:** Reduce card blur to `8px`; reserve `blur(16px)` only for header and modal backdrop; use `background: rgba(15,23,42,0.9)` (solid-ish) for data-dense cards instead of `rgba(15,23,42,0.75)`.

### 2.6 Information Density / Cognitive Load
- **File:** `frontend/index.html`
- **Issue:** The "Dashboard" tab stacks sparklines (3 canvas charts), metrics grid (CPU/RAM/Disk/Network), OS alerts panel, process table, service controls, and system info in one scrollable view. No collapsible sections or layout toggles.
- **Fix:** Wrap each major block in `<details>`/`<summary>` with `open` default; add a "Compact / Expanded" toggle in header that hides secondary cards.

### 2.7 Navigation — Tab Overflow
- **File:** `frontend/index.html` / `frontend/css/styles.css`
- **Lines:** ~212 (`.tab-nav` flex), ~241 (`.tab-btn` padding 14px 22px).
- **Issue:** 5 primary tabs (`Dashboard`, `Processes`, `Troubleshoot Hub`, `VMs`, `Services`) with logs/network/diagnostics inside Troubleshoot in a single flex row. At 1366px or with translated labels this overflows.
- **Fix:** Add `overflow-x: auto` with fade indicators; consider a `select` dropdown for mobile; add `title` tooltips to each button.

### 2.8 Touch Targets & Form Visibility
- **File:** `frontend/css/styles.css`
- **Lines:** ~554 (`.btn` padding 10px 20px, ~32px height), ~534 (`.search-input` no icon), ~540 (`.filter-select`).
- **Issue:** Buttons are ~32px high (below 44px accessibility standard). Search inputs have identical borders to cards; no left icon to distinguish them.
- **Fix:** Change `.btn { padding: 12px 22px; min-height: 44px; }`; add `background-image` icons (or inline SVG) to `.search-input` and `.filter-select`; make focus ring `border-color: #38bdf8; box-shadow: 0 0 0 3px rgba(56,189,248,0.3);`.

### 2.9 Log / Terminal Readability
- **File:** `frontend/css/styles.css`
- **Lines:** ~927 (`.logs-window`), ~945 (`.log-line`), ~952 (3 static color classes).
- **Issue:** Only `log-error`, `log-warning`, `log-info` exist — no timestamp/level coloring, no PID highlighting, `word-break: break-all` splits URLs awkwardly.
- **Fix:** Add regex-based coloring (match `\d{4}-\d{2}-\d{2}` for dates, `\b(PID|pid|process)\b` for keywords); add `word-break: normal; overflow-wrap: anywhere;`; add a `.btn-copy` button using `navigator.clipboard.writeText()`.

### 2.10 Canvas Sparklines — No Fallback
- **File:** `frontend/js/app.js` (referenced, not fully audited here)
- **Issue:** Live 30-second canvas charts have no `aria-live` region and no static table fallback if JavaScript is disabled or fails to load.
- **Fix:** Add `<div aria-live="polite" aria-atomic="true" id="sparkline-announce">` updated by JS; include a hidden `<table>` with last 10 data points for screen readers.

### 2.11 Responsive Breaks
- **File:** `frontend/css/styles.css`
- **Lines:** ~1490 (`@media (max-width: 768px)`).
- **Issue:** Metric cards use `min-width: 350px`. At 375px viewport (small phones) this forces horizontal overflow.
- **Fix:** Change `minmax(350px, 1fr)` to `minmax(280px, 1fr)`; allow `flex-wrap: wrap` on `.metrics-grid`.

### 2.12 Toast Notifications
- **File:** `frontend/css/styles.css`
- **Lines:** ~1000 (`.toast-container`), ~1020 (`.toast` animation).
- **Issue:** Fixed at `top: 75px`; max-width 400px overflows on mobile; no visible auto-dismiss timer; only close via click.
- **Fix:** Add progress bar (`.toast-progress`) that shrinks over 5s; allow `Escape` to dismiss; on `<480px` reduce to `max-width: 92vw` and stack from left.

### 2.13 Modals / Overlays
- **File:** `frontend/css/styles.css`
- **Lines:** ~1060 (`.modal`), ~1095 (`.modal-content`).
- **Issue:** `backdrop-filter: blur(8px)` can block interaction incorrectly in some browsers; `max-height: 85vh` may clip long content (e.g., process detail inspector); no `focus-trap` mentioned.
- **Fix:** Add `overflow-y: auto; scroll-padding: 20px;`; ensure `tabindex="-1"` and `focus()` on open; add close button with `aria-label="Close"`.

---

## 3. Backend / Security Findings (from `backend/main.py`)

### 3.1 VM Lifecycle Controls — Strong Design
- **Endpoint:** `POST /api/vms/{vm_id}/{action}` (line 1409)
- **Good:** Action whitelist (`VM_ACTIONS`); regex `VM_ID_PATTERN`; destructive confirmation payload; native libvirt preferred over `sudo virsh`; state-aware no-op skipping (e.g., don't shut down already stopped VM).
- **Risk:** `sudo virsh` fallback requires exact argv match with `/etc/sudoers.d/monitorx-virsh` (`--no-pkttyagent` must stay in sync with `_virsh_command()`). Any drift = silent failure.
- **Suggestion:** At startup, run `sudo -l -U $(whoami)` and expose `/api/auth/status` so UI can report "libvirt RW / virsh policy / blocked" precisely.

### 3.2 Process Kill — Multi-User Risk
- **Endpoint:** `POST /api/processes/{pid}/kill` (line 2323)
- **Issue:** No verification that `pid` belongs to the dashboard's user (`psutil.Process(pid).uids()`). If backend runs as root or with broad sudo, this could kill other users' processes.
- **Fix:** Enforce `if process.uid() != os.getuid(): raise HTTPException(403, ...)` unless admin override is set.

### 3.3 Diagnostic Console — Excellent Safety
- **Presets:** Hardcoded (`df -h`, `free -h`, `ss -tulpn`, `systemctl --failed`, `dmesg -T`, `uname -a`).
- **Fix suggested:** Add `max_output_chars` cap (e.g., 100KB) to prevent memory exhaustion from huge `dmesg`; log all executions to `/var/log/monitorx/diagnostics.log` (currently only VM actions are audited at `/api/vms/log`).

### 3.4 WebSocket / Real-Time
- **Risk:** No auth on WebSocket endpoint. If bound to `0.0.0.0` (current default in `main.py` line 1480: `uvicorn.run(app, host="0.0.0.0", port=8080)`), anyone on the LAN can subscribe and trigger endpoints.
- **Fix:** Change default to `127.0.0.1`; allow `0.0.0.0` only via env `MONITORX_HOST=0.0.0.0`; document firewall rules (block 8080 from external).

### 3.5 Service Management (systemctl)
- **Design:** Installer creates narrowly scoped passwordless sudo for specific commands (start/stop/restart/reload/enable/disable + remediation commands). Good.
- **Suggestion:** Add dry-run endpoint `GET /api/services/{service}/dry-run` so UI can preview the exact command before execution, reducing accidental restarts.

### 3.6 Data Persistence
- **Status:** All metrics are in-memory via WebSocket; no SQLite/TimescaleDB/Prometheus backing.
- **Suggestion:** Add embedded SQLite cache with 24h window for sparklines; survive refresh without data loss.

---

## 4. Priority Action Plan

| Priority | Action | File(s) | Effort |
|---|---|---|---|
| P0 (A11y / Safety) | Add `prefers-reduced-motion`; raise `--text-muted`; add `aria-label` to dots/pills | `styles.css`, `index.html` | 15 min |
| P0 (Security) | Enforce UID match in process kill; change default host to `127.0.0.1` | `backend/main.py` | 20 min |
| P1 (UX) | Reduce card blur; add collapsible sections; fix touch targets (44px) | `styles.css` | 30 min |
| P1 (UX) | Add copy button + syntax regex to log window; throttle canvas redraw | `js/app.js`, `styles.css` | 30 min |
| P2 (Backend) | Add `/api/auth/status`; add audit log for diagnostics; dry-run service endpoint | `backend/main.py` | 1 hr |
| P2 (Data) | SQLite cache for 24h metrics; survive reconnect | `backend/main.py` + SQL | 2 hr |

---

## 5. How to Apply

If you want **CSS patches only**, I can edit `frontend/css/styles.css` directly and reload the server.  
If you want **backend patches**, I can modify `backend/main.py` and restart the process.  
If you want **patch files** (diff format) to review first, say "generate diffs".

**Suggested first patch:** Accessibility + motion + contrast (P0). It touches ~40 lines of CSS, no JavaScript or backend changes, and immediately improves usability.

---
*Report generated: 2026-08-05*  
*Live server PID: 1472 — still running for inspection*

---
## 6. Applied Patch Log (2026-08-05 — "All")

### CSS (frontend/css/styles.css)
- [x] Raised `--text-muted` from `#94a3b8` → `#b8c5d6` (contrast P0)
- [x] Added `@media (prefers-reduced-motion: reduce)` guard (motion P0)
- [x] Reduced card blur `12px` → `8px` (performance P1)

### HTML (frontend/index.html)
- [x] Added `aria-label="MonitorX — Infrastructure observability dashboard"` to logo `<h1>`
- [x] Confirmed `copy-logs-btn` + `navigator.clipboard` JS already present (P1)

### Backend (backend/main.py)
- [x] Added `subprocess` import
- [x] Added UID-check guard in `/api/processes/{pid}/kill` (security P0)
- [x] Changed default `uvicorn.run` host to `os.environ.get("MONITORX_HOST", "127.0.0.1")` (security P0); running via `MONITORX_HOST=0.0.0.0` for preview
- [x] Added `/api/auth/status` endpoint reporting `libvirt_available`, `libvirt_rw`, `virsh_policy_available`, `user_uid`
- [x] Server restarted and verified (`curl /api/auth/status` → 200)

### Not yet applied (require more time / design choice)
- [ ] Collapsible section layout (details/summary or JS toggle) — needs design confirmation
- [ ] Canvas throttle / aria-live region — requires `app.js` edit
- [ ] SQLite persistence (24h cache) — requires schema + migration
- [ ] Process audit log endpoint — needs file storage design

Server live: `0.0.0.0:8080` (PID 2026/2094/2096 — uvicorn workers).

---

## 7. Applied Patch Log (2026-08-05 — "Auto-Fix Engine")

Rebuilt the Troubleshoot Hub into a self-healing fix center: every error the scan
detects can be repaired **inside the hub**, individually or all at once.

### Backend (`backend/main.py`)
- [x] Remediation registry `FIX_ACTION_META` + `FIX_EXECUTORS`; all executions
      audited via `audit_operation()` and timed (`duration_ms`).
- [x] New fix actions: `restart_service` / `start_service` / `enable_service`
      (validated against `SERVICE_NAME_PATTERN`), `reap_zombies` (SIGCHLD to
      zombie parents, owner-guarded), `flush_dns` (resolvectl/systemd-resolve/nscd),
      `clean_tmp` (find -mtime +7 in /tmp & /var/tmp).
- [x] `kill_process` now enforces a UID-ownership guard (multi-user safety P0 from audit §3.2).
- [x] Health scan upgraded to 11 checks: new *Journal Disk Footprint*, *Pending
      Reboot* and *File Descriptor Pressure* checks; every
      failing check carries `fix` / `fixes` metadata (label, level, target, sudo flag).
      Failed services expose one button per unit; disk issues expose vacuum + tmp cleanup.
- [x] New endpoints: `POST /api/troubleshoot/fix-all` (sequential batch runner with
      auto-built plan from a fresh scan, confirm-gated, 40-action cap),
      `GET /api/troubleshoot/fix-capabilities` (which fixes the environment can run),
      `GET /api/troubleshoot/fix-history` (remediation audit trail).
- [x] Backward-compatible aliases (`clear_dmesg`, `clear_logs`, `clear_kernel_buffer`).

### Frontend
- [x] `frontend/css/fix-engine.css` — Fix Engine console, run-progress rows, plan
      modal, remediation history, per-card fix buttons (level-colored, disabled
      when the environment can't run them).
- [x] `frontend/index.html` — Auto-Fix Engine panel (Fix All Issues + Review Fix
      Plan + Remediation History) atop the Health Scan & Fix Hub; Fix Plan review
      modal with per-fix toggles and danger-level badges.
- [x] `frontend/js/app.js` — plan builder (deduped from `fix`/`fixes` metadata),
      sequential `runFixAll` with live per-item progress, inline per-card fix
      results, capability-aware disabling, history loader, auto re-scan after fixes.

### Verified live
- Scan found `kernel_logs` warning (score 88) → `Fix All` cleared it → history
  recorded → re-scan returned **score 100** (all checks passing).
- Injection attempts rejected (`evil; rm -rf /` service target, unknown actions → 400);
  real zombie process detected and SIGCHLD reaping verified; missing-tool actions
  (flush_dns without a resolver) fail gracefully with a clear message.

---

## 8. Applied Patch Log (2026-08-05 — Controls hardening: service-policy probe + bulk kill)

Follow-up verification of the three control surfaces (VM lifecycle, services, process manager).

### 1. VM Controls (Start/Shutdown/Reboot/Suspend/Resume) — verified ✅
- `GET /api/vms/capabilities` enables controls when **either** path works: native read-write
  libvirt (`_ensure_libvirt_rw_conn`) or the exact-argv `sudo virsh` probe
  (`_virsh_fallback_allowed`, `sudo -n -l -- <full argv>`). Buttons disable when both fail
  (frontend gates every render on `state.vmCapabilities.can_control`).
- `_virsh_command()` argv `virsh --quiet --no-pkttyagent --connect <URI> <verb> -- <domain>`
  is byte-for-byte in sync with the `MONITORX_VIRSH` alias written by
  `systemd/install-service.sh` (`--no-pkttyagent` included on both sides).
- Verified: `poweroff` maps to the real `destroy` verb; `--connect` pinned; `--` terminates
  option parsing; graceful/destructive confirmation matrix intact.

### 2. Services (Start/Stop/Restart/Reload) — fixed ⚠️→✅
- **Bug found:** `service_capabilities()` only ran `sudo -n -l` and treated any exit 0 as
  "authorized". That only proves the account holds *some* sudo privilege (full `ALL`,
  unrelated apt policy, …), so the tab enabled Start/Stop/Restart/Reload even when
  `/etc/sudoers.d/monitorx-systemctl` was missing — every action then 403'd.
- **Fix:** new `_service_sudo_allowed()` probes the exact argv the backend executes
  (`sudo -n -l -- systemctl --no-ask-password start monitorx-capability-probe.service`),
  mirroring the virsh fallback check. `service_capabilities()` now reports `can_control`
  only when that probe succeeds (or when running as root).
- `GET /api/auth/status` rewritten to use the real async probes
  (`_ensure_libvirt_rw_conn` / `_virsh_fallback_allowed` / `_service_sudo_allowed`) instead
  of a loose `sudo -l` text scrape; added `systemctl_policy_available`.
- Probe logic unit-verified for: policy present → True, policy absent → False, root → True,
  sudo missing → False. `import subprocess` (now unused) removed.

### 3. Process Mgr multi-select kill — verified + hardened ✅
- **Verification answer:** bulk kill already respected the ownership guard **per process** —
  the frontend issued one `/api/processes/{pid}/kill` per PID and each request independently
  403s foreign-owned PIDs. It never failed entirely on the first unauthorized target.
  However the UX was poor: N+1 confirm dialogs, generic "Failed to terminate process"
  toast that hid the 403 reason, and no aggregate result.
- **Fix:** new `POST /api/processes/kill` batch endpoint (`BatchKillRequest {pids, signal}`).
  Ownership guard is enforced per PID; each PID gets its own `{pid, success, message}`
  result ("belongs to UID … (skipped)", "process not found", "permission denied", …).
  SIGTERM targets share one 5-second grace window polled in parallel (N processes ≈ 5s,
  not 5s each) with SIGKILL escalation for survivors — same semantics as the single-PID
  endpoint. Input validated (dedupe, ≤500 PIDs, signal ∈ {9,15}).
- Frontend: `💀 Terminate Selected` → one confirm → one batch request → summary toast
  ("Killed 3, skipped 2 (PID 1: belongs to UID 0 …)"). Single `killProcess` now surfaces
  the server's error detail (`readApiError`) instead of a generic message.

### Live verification (sandbox, uid 1001, full sudo)
- Batch kill of [user-sleep, user-sleep, PID 1, 999999] → 2 killed, PID 1 refused
  ("belongs to UID 0; you are UID 1001 (skipped)"), 999999 "process not found"; dedupe worked.
- SIGTERM-ignoring child → "did not exit within 5s; escalated to SIGKILL" (5.5s total).
- Graceful child → "terminated (SIGTERM)" (0.5s).
- Invalid signal → 400; empty pids → 422; single-PID endpoint unchanged and still guarded.
- `node --check` passes on `frontend/js/app.js`; backend compiles clean.
