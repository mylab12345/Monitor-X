# MonitorX — Cybersecurity & Code Quality Audit

**Audit date:** 2026-08-15
**Scope:** Full repository (`backend/main.py` 6,408 lines, `frontend/` JS/HTML/CSS, `Dockerfile`, `docker-compose.yml`, `systemd/`, `tests/`, shell scripts)
**Auditor role:** Cyber-security expert level review (malware/spyware hunt, CVE/dependency check, vulnerability analysis, bug hunting, code smells)

---

## 1. Executive Summary

| Category | Result |
|---|---|
| **Malware / spyware / trojan / backdoor** | ✅ **NONE FOUND** — no suspicious network beacons, obfuscated payloads, credential harvesting, hidden callbacks, or covert exfiltration anywhere in the tree (details in §3) |
| **Known CVEs in dependencies** | ✅ **NONE FOUND** — `pip-audit` against the current OSV database reports no known vulnerabilities for the pinned ranges (§4) |
| **High-severity security findings** | ⚠️ **3** (§5.1) |
| **Medium-severity security findings** | ⚠️ **7** (§5.2) |
| **Low-severity findings** | ⚠️ **8** (§5.3) |
| **Functional bugs discovered** | **2 confirmed by runtime proof** (§6.1) |
| **Code smells** | **12** (§6.2) |
| **Automated test suite** | 52/52 passing (incl. security regression tests) |
| **Prior hardening** | The codebase already contains extensive hardening from an earlier review (2026-08-13): argv-based command execution, O_NOFOLLOW state files, session cookies instead of raw-token cookies, libvirt URI allowlist, per-PID ownership guards on kills. This audit builds on that. |

**Bottom line:** This is a legitimately useful, locally-scoped Linux monitoring tool with unusually good security hygiene for its class. The dangerous findings are **deployment-configuration and boundary-control gaps** (insecure Docker default, missing WebSocket Origin checks, SSRF-capable network probes, a CSP that contradicts the frontend's own CDN dependencies), not planted malware or obvious RCE. All findings below are actionable in a few hours of work.

---

## 2. Methodology

1. **Full manual source review** of all 34 files (backend 6.4k lines read end-to-end; every API route, subprocess call, and data sink traced).
2. **Automated scanners:** `bandit 1.9.4` (52 findings, 0 HIGH), `pip-audit 2.10.1` (dependency CVE check against OSV DB), Python 3.11 runtime proofs for suspected bugs.
3. **Test suite:** `pytest` — 52/52 passed (needed `httpx` installed for the TestClient-based tests).
4. **Malware indicators scan:** eval/`new Function`, string-`setTimeout`/`setInterval`, `atob`/`fromCharCode`, long base64 blobs, hex-escaped strings, external URLs in CSS, hardcoded credentials/private keys, `.git` history review (single commit), obfuscation patterns.
5. **Threat-model driven route review:** every endpoint classified for auth, SSRF, command injection, info disclosure, DoS.

---

## 3. Malware / Spyware / Trojan / Backdoor Analysis — NONE FOUND

Indicators checked and **all clean**:

- **No covert network callbacks.** The only outbound network calls are: (a) optional alert webhooks to an operator-configured URL (`_fire_webhook_sync`), (b) `dig @8.8.8.8` in the DNS diagnostics tool, (c) `ping 8.8.8.8` health check, (d) VM Insights SSH to operator-configured guest hosts. No hardcoded C2-style endpoints, no phone-home on startup, no user-agent/beacon strings.
- **No obfuscated code.** No `eval`, `new Function`, string-executed timers, `atob`, `String.fromCharCode`, hex-escaped payloads, or oversized base64 blobs in any JS/Python file.
- **No credential harvesting.** The only "login" is the dashboard's own token check (`hmac.compare_digest`, constant-time). No keylogging, clipboard reads, or form-jacking.
- **No data exfiltration.** No `fetch`/`XMLHttpRequest` to third-party origins from frontend code (the CDN loads are xterm.js/Google Fonts — see finding S-06 — not telemetry sends). State/audit data stays local (SQLite, 0600 files).
- **No persistence backdoors.** systemd unit, sudoers files, and `setup.sh` are transparent and reviewed; no reverse shells, no cron tricks, no `/etc/ld.so.preload` style tampering.
- **No hardcoded secrets, private keys, or tokens** committed anywhere (`.gitignore` correctly excludes `.env`, `*.db`, `*.log`; `.env.example` ships only empty placeholders).
- **History is clean:** a single git commit, no hidden branches or deleted payloads in history.

---

## 4. Dependency / CVE Analysis — NONE FOUND (with caveats)

`pip-audit -r requirements.txt` (full transitive resolution) → **"No known vulnerabilities found"** against the current OSV database.

Reviewed pins: `fastapi>=0.141,<0.142`, `starlette>=1.3.1`, `uvicorn[standard]>=0.30.1`, `websockets>=12.0`, `psutil>=5.9.8`, `py3nvml>=0.2.7`, `libvirt-python==11.3.0` (Dockerfile only). The requirements comment explicitly documents capping above the fastapi 0.111.0/starlette advisory range — correct call. (Follow-up cleanup pass on 2026-08-15 removed the unused `aiofiles` pin.)

**Caveats (hygiene, not vulns):**
- `requirements.txt` uses open-ended ranges (`>=`), so builds are non-reproducible — the same Docker build next month can pull different (possibly vulnerable) wheels. Consider `pip-compile` + hashes (CWE-1357). `pip-audit` can only vouch for today's resolution.
- `py3nvml` is a low-activity project (NVML wrapper); it's only imported under `try/except` so a broken/forked release degrades gracefully — acceptable, but worth pinning tightly.
- No `npm` lockfile/`package.json` exists for the JS smoke tests — irrelevant at runtime (frontend ships as static files, no bundled deps).

---

## 5. Security Findings

Severity key: 🔴 High · 🟠 Medium · 🟡 Low. All include evidence (`file:line`), CWE, and remediation.

### 5.1 🔴 High

#### S-01 — Insecure Docker default: dashboard published on all interfaces with **empty** auth token
- **Location:** `docker-compose.yml` (`ports: "8080:8080"`, `MONITORX_AUTH_TOKEN: "${MONITORX_AUTH_TOKEN:-}"`, `MONITORX_HOST: "0.0.0.0"`); auth gate in `backend/main.py:415-438`.
- **Detail:** With no `MONITORX_AUTH_TOKEN` exported on the host, the container binds `0.0.0.0:8080` and `AuthMiddleware` **passes every request**. The README's `MONITORX_AUTH_TOKEN="change-this" docker compose up` example masks that the *default* compose file is wide open. An unauthenticated remote attacker then has: full live telemetry + process tables + listening-port inventory, process kill (same-uid), service start/stop (via sudoers policy on native installs), VM lifecycle + serial console (when libvirt is mounted), the entire Troubleshoot suite (including destructive remediations), and the diagnostic command allowlist.
- **CWE-1188 (Insecure Default) / CWE-284 (Improper Access Control).**
- **Fix:** Make compose fail-closed — e.g. `MONITORX_AUTH_TOKEN: "${MONITORX_AUTH_TOKEN:?Set a strong token or run behind a trusted proxy}"`, or bind `127.0.0.1:8080:8080` by default and require an explicit override to publish.

#### S-02 — WebSocket endpoints never validate the `Origin` header (Cross-Site WebSocket Hijacking)
- **Location:** `backend/main.py:3752` (`/ws`), `3783` (`/ws/vm-console/{vm_id}`); auth helper `_websocket_authenticated` (`:405-413`) checks only bearer/cookie.
- **Detail:** Browsers do **not** enforce same-origin on WebSocket handshakes — the server must. With auth disabled (the localhost default), **any website the operator visits can open `ws://localhost:8080/ws`** (full host telemetry, process names, usernames, IP tables) and — much worse — **`ws://localhost:8080/ws/vm-console/<vm>`** and drive an interactive serial console into the hypervisor's guests (keystroke-level access, no auth). With auth enabled, the `SameSite=strict` session cookie is not attached cross-site, so this is currently contained — but only accidentally. The console proxy also allocates a PTY + subprocess per connection with no per-client cap.
- **CWE-1385 (Origin Validation Error) / CWE-406.**
- **Fix:** In both WS handlers, parse `websocket.headers.get("origin")` and reject unless it matches the expected scheme+host (`request.base_url` origin) or is absent/`null` (non-browser clients). Add a max-connections-per-IP bound.

#### S-03 — Network troubleshooting endpoints are unauthenticated SSRF / internal-network scanners
- **Location:** `backend/main.py:5216` (`/api/troubleshoot/ping`), `5249` (`port-check` → `asyncio.open_connection(host, port)`), `5290` (`dns-lookup`), `5327` (`network-ports` → `ss -tulpn` equivalent); only regex `^[a-zA-Z0-9.-]+$` on host — **no internal-address filtering**.
- **Detail:** These deliberately probe arbitrary hosts/ports *from the server's network position*. When the dashboard is exposed (S-01 default), this is an unauthenticated port scanner + reachability oracle for the whole LAN, and a blind SSRF against loopback services and cloud metadata (`169.254.169.254`). Note the project already built a proper SSRF guard for the webhook (`_reject_ssrf_target`, `:3410`) — the same logic was not applied here. `network-ports` also discloses every listening port + PID + process name.
- **CWE-918 (SSRF).**
- **Fix:** Reject loopback/link-local/private/reserved targets (reuse `_reject_ssrf_target`), and gate the suite behind auth even when the rest of the app is open, or clearly mark it "requires auth token".

### 5.2 🟠 Medium

#### S-04 — Webhook SSRF guard bypassable by redirect and DNS-rebinding (TOCTOU)
- **Location:** `backend/main.py:3410-3433` (`_reject_ssrf_target` resolves at save time), `3274-3300` (`_fire_webhook_sync` re-resolves at send time with `urllib.request.urlopen`).
- **Detail:** (a) The check resolves `hostname` when the URL is saved, but delivery re-resolves seconds/minutes later → a name can answer public at save-time and internal at send-time (DNS rebinding). (b) `urlopen` follows HTTP redirects by default, and redirect targets are **never re-validated** — a public URL that 302s to `http://169.254.169.254/...` or `http://127.0.0.1:xxxx` defeats the guard entirely.
- **CWE-918 (SSRF).**
- **Fix:** Resolve once at delivery and reject internal results; use a no-redirect handler (`urllib.request.HTTPRedirectHandler` override or `http.client` with `redirect=False` semantics) and re-check each hop.

#### S-05 — Process detail API discloses environment variables of arbitrary processes
- **Location:** `backend/main.py:3937-3950` — `environ = dict(list(proc.environ().items())[:20])` returned by `/api/processes/{pid}`.
- **Detail:** When MonitorX runs as root (native installs, some setups), this returns the first 20 env vars of **any** process — commonly containing DB passwords, cloud keys, tokens. When unprivileged, it leaks the env of every same-uid process. The UI doesn't render it, but the API is unauthenticated in the default config. Combined with S-01/S-03, an attacker gets secret-harvesting on the host.
- **CWE-200 (Exposure of Sensitive Information).**
- **Fix:** Drop `environ` from the response, or return only non-secret-looking keys (e.g. `PATH`, `HOME`, `SHELL`, `LANG`) with `SECRET`/`KEY`/`TOKEN`/`PASSWORD`-named keys redacted; document it as admin-only.

#### S-06 — CSP contradicts the frontend's own third-party resources (broken feature + supply-chain risk)
- **Location:** CSP in `backend/main.py:452-466` (`script-src 'self'`; `style-src 'self' 'unsafe-inline'`; `font-src 'self' data:`; `connect-src 'self' ws: wss:`) **vs.** `frontend/index.html:8-10` (Google Fonts) and `frontend/js/app.js:2543-2547` (xterm.js + addon-fit from `cdn.jsdelivr.net`, **no `integrity`/SRI**).
- **Detail:** The middleware's own comment says the dashboard "must not be able to load or exfiltrate to third-party origins" — yet the frontend loads scripts/styles/fonts from three CDNs. Net effect: (a) the CSP **blocks all of them**, so the VM serial console never loads xterm (feature broken whenever served through FastAPI, which is the default), and (b) if someone "fixes" the console by relaxing CSP, they'd silently allow third-party code with full dashboard privileges (kill processes, drive VMs) and no SRI — a classic supply-chain RCE if the CDN is compromised. Google Fonts also leaks every operator's IP to Google.
- **CWE-829 (Untrusted inclusion) / CWE-353 (failed security check).**
- **Fix:** Self-host xterm.js (+ addon) and the Inter/JetBrains Mono/Orbitron fonts under `/static/`, keep the strict CSP, and add `integrity=` SRI attributes if any third-party load remains.

#### S-07 — No rate limiting / lockout on the token login
- **Location:** `backend/main.py:491-509` (`/api/auth/login`).
- **Detail:** The only protection on `MONITORX_AUTH_TOKEN` is its entropy. There is no attempt cap, exponential backoff, or lockout — an attacker with network access (S-01) can brute-force the token at HTTP speed. `hmac.compare_digest` prevents timing leaks, which is good, but doesn't stop enumeration.
- **CWE-307 (Improper Restriction of Excessive Authentication Attempts).**
- **Fix:** Add per-IP+per-account attempt limiting (in-memory sliding window is fine for a single-node dashboard; e.g. 5 failures → 30s block), and/or require a reverse proxy for network exposure.

#### S-08 — User-supplied regex in the log inspector → ReDoS on the event loop
- **Location:** `backend/main.py:5176-5184` — `search` param compiled with `re.compile(search, re.IGNORECASE)` and applied to up to 1,000 journal lines (`re.search`, blocking).
- **Detail:** Python's `re` has no timeout. A payload like `(a+)+$` against long log lines can catastrophically backtrack; since this runs synchronously inside the async handler, one request can stall the entire telemetry loop and all dashboards. The 128-char cap helps but doesn't prevent it (short patterns still blow up on long inputs).
- **CWE-1333 (Inefficient Regular Expression Complexity).**
- **Fix:** Use a safe engine (e.g. `regex` module with a timeout) or pre-compile and reject `(a+)+`-style nested-quantifier patterns; at minimum run the matching in a thread with a timeout and escape by default.

#### S-09 — Destructive remediations reachable without a second confirmation / scoped too broadly
- **Location:** `backend/main.py:5612-6130` — notably `_fix_kill_top_cpu` (`:5727`, blocklist `ESSENTIAL_NAMES` at `:5732` **omits** postgres/mysqld/nginx/apache/redis), `_fix_clean_tmp` (`:5855`, `find /tmp /var/tmp … -delete` as root), `_fix_rotate_logs` (`:5965`, `target` accepts any existing absolute path → `sudo logrotate --force <path>`), `_fix_renew_dhcp_lease` (`:6033`, unvalidated interface name passed to nmcli/dhclient argv).
- **Detail:** `kill_top_cpu` as root can SIGKILL a database or web server that isn't on the short blocklist. `clean_tmp` deletes **all** files >7 days old in world-writable temp dirs — other users' data included. These are one `POST /api/troubleshoot/remediate` (or the UI's Fix-All runner, which executes fixes sequentially via that endpoint) away from an unauthenticated attacker in the S-01 configuration. Remediation was clearly designed for a single-operator local console. (Note: the redundant batch `POST /api/troubleshoot/fix-all` endpoint was removed in the 2026-08-15 cleanup pass — the UI never used it; it executed fixes one-by-one through `/api/troubleshoot/remediate`.)
- **CWE-284 / CWE-306 (missing authorization for sensitive functions).**
- **Fix:** Extend the blocklist (all service managers' children, common DB/web servers), expand `clean_tmp` to age+size and owner-aware deletion, validate `rotate_logs`/`dhcp` targets strictly, and gate all `level: critical` fixes behind an authenticated session even on localhost binds.

#### S-10 — Trust of `X-Forwarded-Proto` from any client for the session cookie `Secure` flag
- **Location:** `backend/main.py:499-508`.
- **Detail:** `secure=(request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")`. Any client can send `X-Forwarded-Proto: https` and receive a cookie that a real browser will only transmit over TLS — then the operator is surprised when the cookie never arrives over the plain-HTTP reverse proxy. Conversely, behind a proxy that fails to set it, the cookie isn't Secure. Minor, but it's exactly the class of proxy-header trust bug (CWE-807) that bites later.
- **Fix:** Use `ProxyHeadersMiddleware`/`trustedhosts` semantics or a documented env var (`MONITORX_BEHIND_PROXY=1`) to decide `secure`.

### 5.3 🟡 Low

- **S-11 — `POST /api/audit` accepts arbitrary `action`/`detail`** (`:3581-3585`) — anyone can forge audit entries (log spoofing). Validate against a small allowlist or require auth.
- **S-12 — VM console WS: no origin check + no per-IP/concurrency cap** (`:3783`) — each connection spawns a PTY + `virsh console` subprocess; trivial local DoS via many sockets.
- **S-13 — Insights SSH uses `StrictHostKeyChecking=accept-new`** (`:2270`) — first-connect MITM accepted silently. Acceptable for a trusted-hypervisor admin tool; consider `known_hosts` management + `UpdateHostKeys=yes` policy, and document it.
- **S-14 — `/ws` receive loop has no idle timeout or per-message cap** (`:3762-3773`) — a stalled client holds a slot forever; `ConnectionManager` has no max connections. Add `receive` timeout + max-connections.
- **S-15 — Alert webhooks silently never fire from periodic evaluation** — see bug B-01 (this is both a functional bug and a "monitoring you think works, doesn't" security-operations issue).
- **S-16 — Session dictionary grows with logins** (`_sessions`, `:348`) — pruned only on the *next* login; a flood of logins with valid tokens grows memory until expiry. Minor; add periodic pruning.
- **S-17 — `SERVICE_NAME_PATTERN` allows `..`** and `-o`-style names (`:4169`) — argv-based so not injectable, but `systemctl` argument confusion is worth the stricter `^[A-Za-z0-9._@-]+\.service$` without consecutive dots.
- **S-18 — Health check runs `ping 8.8.8.8` and `dig @8.8.8.8` on every scan** (`:4616`, `:5206`) — outbound traffic to Google from an unauthenticated endpoint; makes the tool visible on the network and can be abused as a reflector. Gate behind auth or make it configurable.

---

## 6. Bugs & Code Smells

### 6.1 Confirmed functional bugs

**B-01 — Alert webhooks never fire from the alert evaluator (proven).**
`persist_snapshot_and_evaluate_alerts` runs inside `asyncio.to_thread` (`backend/main.py:3116`) and calls `asyncio.create_task(_notify_webhook(...))` (`:3240`). `asyncio.create_task` requires a running loop **in the calling thread**; in a worker thread it raises `RuntimeError: no running event loop` (verified with a Python 3.11 reproduction). The `except Exception` at `:3241` swallows it with a warning, so **every threshold alert's webhook notification is dropped** — only the manual `/api/operations/webhook/test` works. Operators will believe alerts are being pushed to Slack/Discord when they never are.
*Fix:* return the webhook job to the loop thread — e.g. `asyncio.run_coroutine_threadsafe(_notify_webhook(...), main_loop)` with a stored reference to the main loop, or fire the webhook inline in the thread.

**B-02 — `dhclient -r` release step dropped in DHCP renewal (`args[1:]` bug).**
`backend/main.py:6051-6055`: the candidate tuple is `("dhclient", ["-r", iface])`, but the command is built as `[path, *args[1:]]`, so `-r` (release) is silently stripped and the tool runs `dhclient <iface>` twice (renew twice) instead of release-then-renew. Harmless-ish, but the code doesn't do what it says and the `-r` branch is dead.
*Fix:* `[path, *args]`.

**B-03 — `get_process_detail` blocks the event loop ~100 ms per request.**
`backend/main.py:3963`: `proc.cpu_percent(interval=0.1)` is called synchronously inside the async handler (via the `safe()` helper). Under concurrent process-inspector requests this stalls the shared loop that also serves telemetry. Move to `asyncio.to_thread` or use `interval=None` with the cached value.

### 6.2 Code smells

1. **Broad `except Exception: pass`/`continue`** in ~30 places (e.g. `:1202-1212`, `:3525-3535`) — hides real failures; at minimum `logger.debug` on unexpected exception types (many already do, some don't).
2. **f-string logging** (`logger.error(f"Error broadcasting stats: {e}")`, `:3123`) — should be lazy `%s` args (minor perf, and can blow up if `e` is huge).
3. **`troubleshoot_logs` duplicates dmesg fallback logic** twice (health check and log inspector) — extract a shared `_read_kernel_logs()`.
4. **Duplicated libvirt state-map dict** (`state_map` at `:2831` and again in `_read_domain_state` at `:2054`) — single source of truth wanted.
5. **`_virsh_command` returns `[]` when sudo is missing** and callers must check empty list — prefer `Optional[List[str]]` with explicit `None`.
6. **Mixed sync/async I/O in async endpoints**: `get_audit` opens the file synchronously (`:3570`); `_load_insights_configs` does blocking `os.open` inside async paths (mitigated by locks but inconsistent).
7. **Magic numbers**: `VM_ACTION_TIMEOUT`, retry loops (`range(10)` + `0.5`) duplicated between single and batch kill — extract constants.
8. **`launch.sh`/`setup.sh` echo "http://localhost:8080"` hardcoded** even when `MONITORX_HOST=0.0.0.0` — misleading operator message.
9. **Frontend**: `state` object mixes concerns (vm + services + filters + logs); several render functions reach 200+ lines; `renderVms` rebuilds cards wholesale on every 2s tick (mitigated by signatures in other components but not here).
10. **`requirements.txt` ranges** (see §4) — no lockfile, no hashes, non-reproducible.
11. **`AUTH_EXEMPT_PATHS` uses exact match** — `/api/health/` (trailing slash) is not exempt while `/api/health` is; harmless today, brittle.
12. **`vm_action_log`/`_HEALTH_HISTORY` bounded but unexported** — fine, but audit events are split across three stores (SQLite `operations_audit`, flat-file audit log, in-memory rings) with no unified view; consolidation would simplify the ops story.

### 6.3 What the tests cover / gaps

Coverage is strong where it matters: SQLite handle leaks, state-file permissions + symlink attacks, cookie/session semantics, CSP/headers, SSH argv hardening + injection rejection, kill ownership guards, VM ID validation, webhook URL validation, container-tooling removal contracts. **Gaps:** no test exercises the WebSocket Origin check (because none exists), no test for the webhook-in-thread bug (B-01), no test asserting CDN URLs are absent from the frontend, no ReDoS test for the log search, no test that `get_process_detail` redacts env secrets, and the browser smoke tests require `jsdom` but no `package.json`/CI wiring exists to run them.

---

## 7. Prioritized Remediation Plan

| # | Action | Effort | Fixes |
|---|---|---|---|
| 1 | Compose must fail closed: require `MONITORX_AUTH_TOKEN` when publishing ports | 15 min | S-01 |
| 2 | Validate `Origin` on `/ws` and `/ws/vm-console/*`; cap concurrent console connections | 1 h | S-02, S-12, S-14 |
| 3 | Apply `_reject_ssrf_target` logic to ping/port-check/dns-lookup; block internal targets | 1 h | S-03 |
| 4 | Webhook delivery: no redirects + resolve-and-verify at send time | 1 h | S-04 |
| 5 | Remove `environ` from process detail (or redact secret-like keys) | 30 min | S-05 |
| 6 | Self-host xterm.js + fonts; keep strict CSP; add SRI if CDNs remain | 2 h | S-06 |
| 7 | Rate-limit `/api/auth/login` (per-IP sliding window) | 45 min | S-07 |
| 8 | Safe regex handling in log inspector (timeout + pattern guard) | 45 min | S-08 |
| 9 | Tighten remediation: extend kill blocklist, owner-aware tmp cleanup, validate rotate/dhcp targets, auth-gate critical fixes | 2-3 h | S-09, S-11 |
| 10 | Fix B-01 (webhook thread bug), B-02 (`args[1:]`), B-03 (event-loop block) | 1 h | §6.1 |
| 11 | Add regression tests for 1-10; wire jsdom smoke tests into CI | 2 h | §6.3 |
| 12 | Pin dependencies with `pip-compile` + hashes | 30 min | §4 |

---

## 8. Verdict

- **Malware/backdoor/spyware:** Clean. This is a legitimate system-monitoring tool with no covert behavior.
- **Exploitability today:** The highest-impact issue is **S-01** — anyone who runs the published `docker compose up -d` without explicitly setting a token deploys an unauthenticated host-control panel on `0.0.0.0:8080`. Localhost-only native installs (the default `./launch.sh`) are meaningfully safer, but still exposed to **S-02/S-03** from any webpage the operator opens.
- **Overall posture:** Above-average for a self-hosted admin dashboard; the remaining work is boundary hardening (origins, SSRF, rate limits, fail-closed defaults) plus the webhook/console functional fixes — no architectural rewrites needed.
