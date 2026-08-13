# MonitorX — Codebase Review

**Reviewed:** 2026-08-13 · branch `arena/019ff999-monitor-x` (base `301236c`)
**Scope:** `backend/main.py` (5,492 LOC), `frontend/` (4,418 LOC JS + 6,477 LOC CSS), deploy + test tooling.

Everything below was **verified by execution** in the sandbox (running server, live fd counts, `pip-audit`), not by reading alone. Findings are ordered by severity. Nothing here has been changed in the code — this is a review; say the word and I'll implement any subset.

**Baseline health:** the existing test suite passes (7/7), `backend/main.py` compiles clean, the server boots and serves telemetry correctly. The issues below are real but the project is in decent working order.

---

## P0 — Fix first

### 1. SQLite connections are never closed (fd leak on a 2-second loop)

`with sqlite3.connect(...) as conn:` is the single most common SQLite misuse in Python. **The context manager commits or rolls back a transaction — it does *not* close the connection.** The code comments explicitly assert the opposite:

```python
# backend/main.py:2309
return conn  # Note: callers must use 'with _ops_conn() as conn:' for guaranteed close
# backend/main.py:2588
with sqlite3.connect(str(DB_PATH)) as conn:  # Fixed: context manager guarantees close even on error (prevents resource leak)
```

Both comments are wrong. Proof:

```
LEAK CONFIRMED: connection still open after `with` block exits
```

Measured against the **live running server** (`/api/operations/audit`, which is one connect per request):

| | total fds | fds on the ops DB |
|---|---|---|
| baseline | 25 | 5 |
| after 300 sequential requests | 35 | **17** |

Concurrent load (40 parallel requests × 3 rounds) holds 11–15 DB fds open at once. Connections are only reclaimed when CPython's GC happens to finalize them — that is luck, not resource management, and it degrades under exactly the concurrency a monitoring dashboard sees.

This affects **16 call sites** (`grep -n "_ops_conn()"`) plus 3 raw `sqlite3.connect` sites, and it runs on the `broadcast_stats` loop every `STATS_INTERVAL` (2s default) — 43,200 connects/day that depend on GC to close.

**Fix.** Wrap with `contextlib.closing`, or make `_ops_conn` a `@contextmanager` that closes in a `finally`:

```python
from contextlib import contextmanager

@contextmanager
def _ops_conn():
    conn = sqlite3.connect(str(OPERATIONS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:          # keeps transaction semantics
            yield conn
    finally:
        conn.close()        # actually closes
```

Call sites need no change. Better still, hold **one** long-lived connection for the writer loop instead of reconnecting (and re-running four PRAGMAs) every frame.

### 2. Vulnerable dependency pins — 12 known CVEs

`pip-audit` against `requirements.txt`:

```
Found 12 known vulnerabilities in 2 packages
jinja2    3.1.4   PYSEC-2026-1471/1472/1475   -> fix 3.1.6
starlette 0.37.2  PYSEC-2026-161/248/249/1941/1943/2280/2281 -> fix 1.3.1
```

`starlette` is pulled in transitively by the `fastapi==0.111.0` pin, so it can't be bumped without moving FastAPI (current: 0.141.1). Every pin is 18+ months stale; `psutil==5.9.8` vs 7.2.2 also matters here because psutil is this app's core data source and has had meaningful Linux fixes since.

**Fix.** Bump `jinja2>=3.1.6` immediately (trivial, no API change), then upgrade FastAPI to a current release to clear starlette. Add `pip-audit` to CI so this doesn't silently rot again. Note the app imports `Jinja2Templates` but the `templates` object is never actually used to render — if you drop that dead import you can drop the jinja2 dependency entirely and eliminate 3 of the 12 CVEs for free.

### 3. World-readable state and audit logs at predictable `/tmp` paths

```python
DB_PATH   = Path("/tmp/monitorx_metrics.db")   # :2584
AUDIT_LOG = Path("/tmp/monitorx-audit.log")    # :2648, written at :3094 and :3183
```

Verified on disk: `-rw-r--r-- (644)` in a `1777` sticky directory. Two distinct problems:

- **Disclosure** — any local user reads your host's full CPU/RAM/disk history and the kill-audit trail (who killed which PID when).
- **Pre-creation / symlink hijack** — a local attacker creates `/tmp/monitorx_metrics.db` (or the audit log) *before* MonitorX starts and owns the file MonitorX then writes to. Because the audit log is opened with a plain `open(..., "a")`, an attacker-planted symlink redirects those appends. The audit trail is the one file that must not be attacker-controlled.

Note the inconsistency: the operations DB *is* configurable (`MONITORX_OPERATIONS_DB`) and defaults sensibly to `BASE_DIR`, but this second, older store is hardcoded to `/tmp`.

**Fix.** Move both under `BASE_DIR` (or `MONITORX_STATE_DIR`), create with `0o600`, and open the audit log with `os.open(..., O_APPEND|O_CREAT|O_NOFOLLOW, 0o600)`.

---

## P1 — Correctness and performance

### 4. `signal` module shadowed by a parameter in `kill_process`

```python
async def kill_process(pid: int, signal: int = Query(15)):   # :3072
```

The parameter shadows the module-level `import signal`. Inside this function `signal` is an `int`, so any `signal.SIGTERM`-style reference raises `AttributeError`. Demonstrated:

```
kill_process(123): AttributeError: 'int' object has no attribute 'SIGCHLD'
```

Today the body only passes the int through to `proc.send_signal(signal)`, so it works *by accident*. It's a trap for the next edit — and `_fix_reap_zombies` at :4889 legitimately uses `signal.SIGCHLD`, so the two idioms coexist in one file. Rename the param to `sig` (keep the wire name via `Query(15, alias="signal")` so the API contract is unchanged).

### 5. Two parallel time-series stores, both written every frame

The app maintains **two** independent metric histories:

| store | table | retention | rows at steady state | written |
|---|---|---|---|---|
| `monitorx-operations.db` | `metric_history` | 30 days | ~1,296,000 | `to_thread` (good) |
| `/tmp/monitorx_metrics.db` | `metrics` | 7 days | ~302,400 | **inline, blocking** |

They store nearly the same columns and serve two endpoints (`/api/operations/overview`, `/api/historical`). This is duplicated write load, duplicated retention logic, and duplicated leak surface for no benefit.

The second one is also written **synchronously inside `collect_all_stats`** (:2240), on the event loop, including a `DELETE ... WHERE ts < ?` full-table scan on **every** frame — note the *other* store deliberately throttles its identical cleanup to every 10 minutes (`_HISTORY_CLEANUP_INTERVAL`) precisely because "the DELETE scans the whole table." The same lesson wasn't applied here. Measured stall is small on an empty DB (~1.9ms) but grows with table size, and it blocks every concurrent request while it runs.

**Fix.** Delete the `/tmp` store and back `/api/historical` with `metric_history`. If you keep it: move the write into `asyncio.to_thread` and throttle the DELETE.

### 6. Blocking `psutil` scans inside `async def` handlers

Ten `psutil.process_iter` / `net_connections` calls sit in `async def` functions with no `to_thread` (`:822, :4035, :4431, :4489, :4815, :4872, :4897`). Measured in this 81-process sandbox:

```
_scan_process_states: process_iter(['status']):  81 procs,  3.4 ms
bottlenecks: process_iter(8 attrs):              81 procs,  6.9 ms
net_connections(inet):                                      6.1 ms
```

`process_iter` cost scales with process count *and* attribute count (each attribute is `/proc` file reads). A real server at 500–1000 processes puts these in the 50–300ms range — every one of which freezes the telemetry broadcast and all concurrent HTTP requests. The codebase already knows the right pattern (`asyncio.to_thread(persist_snapshot_and_evaluate_alerts, ...)`, `_collect_process_stats_sync`); it just isn't applied consistently.

**Fix.** Wrap each in `await asyncio.to_thread(...)`, following the existing `_collect_process_stats_sync` precedent.

### 7. No Content-Security-Policy

`SecurityHeadersMiddleware` sets `X-Content-Type-Options`, `Referrer-Policy` and `Permissions-Policy` but **no CSP** and no `X-Frame-Options`. For an app that renders host-controlled strings (process names, service descriptions, log lines, journal output) through ~85 `innerHTML` assignments, CSP is the defence-in-depth layer that turns a missed escape into a non-event.

Credit where due: the escaping discipline is genuinely good — `escapeHtml`/`escapeAttr` in `app.js`, `esc()` in `operations-center.js`, and I found no unescaped host-controlled string on the paths I audited (process names, usernames, service names, ports, incident titles all escape correctly). CSP protects the *next* line of code, not the current one.

**Fix.** Add `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'`. Inline `style="..."` attributes are used heavily, so either keep `style-src 'self' 'unsafe-inline'` initially or migrate those to classes. Only 3 inline `onclick=` handlers exist in `index.html` (plus generated ones in JS template strings) — worth migrating to delegated listeners so you can eventually drop `'unsafe-inline'` for scripts.

### 8. Session cookie is the token itself

```python
response.set_cookie(AUTH_COOKIE_NAME, MONITORX_AUTH_TOKEN, httponly=True, ...)  # :367
```

The cookie value *is* the long-lived shared secret. So: no expiry independent of the 12h `max_age`, no revocation without restarting the service and invalidating every session, and the secret is replayed on every request. The `httponly` + `samesite="strict"` + `hmac.compare_digest` choices are all correct — the weakness is storing the credential itself rather than a derived session id.

**Fix.** Issue a random session id (`secrets.token_urlsafe(32)`) kept in a server-side dict/table mapping to an expiry. Lets you implement real logout and rotation. `samesite="strict"` already gives solid CSRF coverage for the 23 state-changing endpoints — worth keeping regardless.

---

## P2 — Maintainability

### 9. Single-file backend at 5,492 lines

`backend/main.py` holds config, auth middleware, libvirt lifecycle, 60+ routes, the remediation engine (~20 `_fix_*` functions), the ops/alerting store, webhooks, and the WebSocket console proxy. The one health-check handler `troubleshoot_health_check` spans **~790 lines** (:3429–4221).

This is the root cause of several findings above: the duplicate metric stores and the inconsistent `to_thread` usage are what happens when related logic can't see itself. Suggested split, in dependency order so it can be done incrementally:

```
backend/
  config.py        # env parsing + constants
  security.py      # auth middleware, headers, sessions
  collectors/      # cpu, memory, disk, network, gpu, process, thermal
  integrations/    # libvirt, systemd, virsh
  operations/      # sqlite store, alert rules, webhooks
  routes/          # thin FastAPI routers per domain
  main.py          # app assembly only
```

### 10. 120 broad `except Exception` handlers, 52 silent `pass`

Failures are swallowed wholesale, including in the persistence path:

```python
except Exception:
    logger.debug("Could not persist lightweight metrics snapshot", exc_info=True)
```

At the default `INFO` level that is invisible — the metrics store could be failing every 2 seconds in production and the operator would never know. `init_db()` is worse: bare `except Exception: pass`, so a schema failure surfaces later as a mysteriously empty `/api/historical`.

**Fix.** Catch specific exceptions (`sqlite3.Error`, `psutil.Error`, `OSError`); log at `warning` for anything that indicates a real fault. Also swap the 7 f-string log calls for `%s` lazy formatting.

### 11. CSS: 10 stylesheets, 6,477 lines, 27 `!important`

Ten separate stylesheets load on every page (228K CSS + 204K JS uncompressed, ~432K total), layered as successive override files — `modern-overrides.css`, `quality-overrides.css` (943 lines, 8 `!important`), `layout-fit.css`, `dashboard-refresh.css`. The naming tells the story: each is a patch layer over the previous rather than a fix at the source. Cascade order is now load-bearing and any change risks whack-a-mole.

**`frontend/css/theme.css` is dead code** — 3 lines, not referenced by `index.html`, and its `--bg`/`--card`/`--accent` variable names don't even match the real theme system in `themes.css`. Safe to delete.

**Fix.** Delete `theme.css`; fold the override layers back into `styles.css` + `themes.css`; concatenate/minify for production. GZip is on (good), but 10 round-trips of override CSS is still avoidable.

### 12. README contradicts itself on theme count

Three different numbers for the same feature:

- Line 8: "a **thirteen-theme picker**" — then lists 13 names
- Line 58: "MonitorX ships with **nine complete visual themes**" — table lists 9
- Line 176: "themes.css # Multi-theme system (9 themes + picker styles)"

Ground truth is **13** (`body.theme-*` in `themes.css`): midnight, aurora, ember, forest, nebula, graphite, ocean, lagoon, meadow, desert, canyon, arctic, sakura. The line-58 table is missing lagoon, meadow, canyon, sakura.

Relatedly, `.opencode/MEMORY.md` — explicitly written as the file an agent should "read FIRST" — is materially stale: it states `Database | None (all ephemeral)` (there are two SQLite stores), `Auth | None` (token auth exists), `Port 8080 (0.0.0.0)` (default is `127.0.0.1`), and `main.py ~2940 lines` (5,492). Stale agent-facing docs actively cause wrong changes.

### 13. Test coverage is thin for a tool that kills processes

`tests/test_contracts.py` is 7 tests, mostly "this string is absent from the source." Genuinely valuable — the auth test (`/api/health` public, `/api/stats/cpu` 401→200) is a real integration test. But there is **zero** coverage of:

- the owner-guard on process kill (the highest-risk code in the repo)
- remediation actions and the `_fix_*` dispatch table
- alert rule evaluation, cooldowns, and incident dedup logic
- VM action → virsh argv mapping (README documents this exact thing breaking twice in past releases)

The argv-mapping case is the strongest argument for tests here: README says a `--no-pkttyagent` mismatch made *every* VM control silently fail in a shipped release. That's a three-line unit test asserting `_virsh_command()` output matches the sudoers policy string.

Also, `tests/*.js` (jsdom smoke tests) have no `package.json` — there's no way to run them without knowing the incantation, and CI can't discover them.

---

## Suggested order

1. **`_ops_conn` close fix** (#1) — small, contained, removes a live resource leak
2. **`jinja2` bump + drop unused Jinja2Templates** (#2) — clears 3 CVEs, near-zero risk
3. **Move `/tmp` state to `BASE_DIR` with `0600`** (#3)
4. **Rename the `signal` param** (#4) — one line
5. **`to_thread` the psutil scans** (#6) — biggest latency win under real load
6. **Delete the duplicate `/tmp` store** (#5) — subsumes part of #3
7. **Add CSP** (#7), then session ids (#8)
8. Docs (#12) and tests (#13) alongside whichever of the above you touch

Items 1–4 are roughly an hour together and carry the most risk reduction per line changed.
