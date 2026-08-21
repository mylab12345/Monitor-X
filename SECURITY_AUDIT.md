# MonitorX — Security Audit and Remediation Report

**Audit date:** 2026-08-16
**Scope:** Entire tracked repository: FastAPI backend, static frontend, Docker/systemd deployment, shell installers, tests, and Python dependencies.
**Method:** Source review, trust-boundary and data-flow analysis, malware/secret indicators, subprocess and browser-sink review, dependency CVE scan, static security analysis, and non-destructive adversarial regression tests.

## Executive result

| Area | Result after remediation |
|---|---|
| Known dependency CVEs | **None detected** by `pip-audit` against the current advisory database |
| Bandit findings | **0 high, 0 medium**; 45 low-confidence/low-severity defensive-code findings remain |
| Malware / spyware / backdoors | **None found** |
| Hardcoded credentials / private keys | **None found** |
| Automated tests | **59 passed** |
| Python/JavaScript syntax | **Passed** |
| Critical/high findings from the prior review | **Remediated** |

No finite audit can prove that future CVEs or every possible defect do not exist. This report records what was checked, what was changed, and the residual risks rather than making an absolute security guarantee.

## Threat model exercised

The review treated all of these as hostile inputs:

- unauthenticated Internet/LAN clients reaching a published container port;
- a malicious website opened in the operator's browser (cross-site WebSocket attacks);
- hostnames that resolve to loopback, RFC1918, link-local, metadata, reserved, multicast, or mixed public/private addresses;
- webhook redirects and DNS-rebinding between validation and delivery;
- malformed and catastrophic-backtracking log-search strings;
- process IDs, service names, VM identifiers, interface names, logrotate paths, and diagnostic command payloads;
- malicious guest serial output and libvirt XML;
- login floods, session floods, and WebSocket/console connection exhaustion.

Testing was non-destructive. No process, VM, service, network interface, package, or host log was changed by the audit.

## Remediations completed

### Deployment and authentication

1. **Docker now fails closed.** `docker-compose.yml` requires `MONITORX_AUTH_TOKEN`; an empty environment can no longer publish an unauthenticated host-control dashboard on `0.0.0.0:8080`.
2. **Login throttling added.** Failed attempts are limited per direct peer, return `429` with `Retry-After`, and limiter memory is bounded.
3. **Session storage bounded.** Expired and oldest sessions are pruned with a configurable maximum.
4. **Proxy-header trust removed.** Arbitrary `X-Forwarded-Proto` no longer controls the cookie's `Secure` flag. HTTPS or explicit `MONITORX_COOKIE_SECURE=true` controls it.
5. **Critical remediation authorization added.** Critical auto-fixes require authentication to be configured and the request to be authenticated, even for otherwise local/no-token installations.

### WebSockets and VM console

1. Both WebSocket routes now enforce same-origin browser upgrades while still allowing non-browser clients that omit `Origin`.
2. Telemetry WebSockets have a global connection cap, receive idle timeout, and inbound message-size bound.
3. VM serial consoles have a strict concurrent-connection cap and clean up capacity on all startup/transport/normal exit paths.
4. Error close reasons no longer expose raw host exception details.

### SSRF and outbound traffic

1. Ping, TCP port-check, and DNS diagnostics reject loopback, private, link-local, metadata, reserved, multicast, unspecified, and otherwise non-global targets after DNS resolution.
2. Webhooks are re-resolved at delivery time, reject mixed/non-public answers, connect to the validated IP, retain the original hostname for TLS SNI and `Host`, and refuse redirects. This closes redirect SSRF and the DNS validation/send race.
3. Webhook URLs reject credentials, control characters, whitespace, invalid schemes, and malformed hosts.
4. Hardcoded Google DNS/ping probes were removed. No automatic third-party connectivity beacon runs by default. Operators may explicitly set `MONITORX_CONNECTIVITY_TARGET`.

### Browser and supply-chain hardening

1. Google Fonts and jsDelivr runtime dependencies were removed.
2. The VM console now uses a small, local, dependency-free terminal surface. Guest output is inserted with `textContent`, never HTML, and ANSI control sequences are discarded rather than interpreted as markup.
3. The strict same-origin CSP remains intact and now agrees with actual frontend behavior.
4. No frontend runtime URL sends telemetry to a third party. The only external URL visible in HTML is the operator-facing webhook placeholder.

### Sensitive data and parser safety

1. Process environment variables were removed from the process-detail API. This prevents disclosure of database passwords, cloud keys, and service tokens.
2. The entire process-detail collection is offloaded from the event loop, fixing the 100 ms synchronous CPU-sampling stall.
3. Libvirt XML now uses `defusedxml`.
4. Alert-rule updates use fully static, parameterized SQL statements. No SQL identifiers are constructed dynamically.
5. Client-created audit events are restricted to a small action allowlist with bounded fields.

### Command and remediation safety

1. Log search is literal, not an attacker-controlled Python regular expression, eliminating catastrophic regex backtracking.
2. Service names reject option-like/path/traversal forms, colons, and consecutive dots.
3. DHCP interface targets must match a strict syntax and be currently active.
4. The dropped `dhclient -r` argument bug was fixed.
5. Log rotation accepts only `/etc/logrotate.conf`, not an arbitrary absolute config path.
6. Temp cleanup stays on the target filesystems and deletes only regular, stale, non-trivial files owned by the MonitorX service UID. It does not delete other users' temp data.
7. The top-CPU kill protection list now includes common databases, web servers, caches, container daemons, and network services.
8. Existing subprocess execution remains argv-only with bounded timeouts; no shell interpreter, `eval`, or dynamic code execution is used.

### Functional and quality fixes

1. Alert webhook jobs are returned from the SQLite worker thread and scheduled by the main event loop. Threshold notifications no longer fail with `RuntimeError: no running event loop`.
2. Logging added in this pass uses lazy formatting.
3. Launch output reflects the configured host and port instead of always claiming `localhost:8080`.
4. Seven new regression contracts pin origin checks, SSRF blocking, dependency self-hosting, compose fail-closed behavior, process-environment removal, literal search, and DHCP argv correctness.

## Malware, spyware, and secret review

No evidence of malware, spyware, credential harvesting, covert persistence, obfuscated payloads, reverse shells, cryptominers, or command-and-control behavior was found.

Expected privileged/network behavior is explicit and operator-driven:

- host telemetry from `psutil` and optional NVML/libvirt;
- argv-only system diagnostics and approved remediation commands;
- optional SSH probes to operator-configured VM guests;
- optional webhook POSTs to an operator-configured, public endpoint;
- optional connectivity probes only when an operator configures a target.

No `eval`, `exec`, shell subprocess, hidden base64 payload, private key, access token, or hardcoded password was found in tracked source.

## Verification evidence

Commands run from the repository root:

```text
.venv/bin/pytest -q
59 passed

.venv/bin/pip-audit -r requirements.txt
No known vulnerabilities found

.venv/bin/bandit -q -r backend -f json
HIGH: 0, MEDIUM: 0, LOW: 45

.venv/bin/ruff check --select E9,F63,F7,F82 backend tests
All checks passed

python -m compileall -q backend tests
node --check frontend/js/*.js frontend/vendor/*.js
node tests/smoke-services.js
node tests/smoke-rootstorage-processes.js
node tests/smoke-vm-insights.js
Passed
```

The 45 Bandit-low items are primarily broad exception handling in best-effort hardware/hypervisor/system telemetry and warnings about fixed argv subprocess calls. They are not ignored as vulnerabilities: failures are intentionally isolated so an unavailable sensor, vanished process, old virsh, or permission denial cannot crash the dashboard. The one `/tmp` suppression documents an intentional cleanup feature with ownership/type/age/size/depth/filesystem restrictions; MonitorX does not store state there.

Docker and ShellCheck binaries were not present in the audit environment, so `docker compose config` and ShellCheck could not be executed. Compose syntax and shell scripts were manually reviewed; Python and JavaScript runtime tests passed.

## Follow-up hardening and performance pass (2026-08-21)

- **Troubleshoot Hub health scan is now cached.** A full scan runs a dozen-plus
  subprocess diagnostics, so repeat visits reused a fresh snapshot within
  `MONITORX_HEALTH_TTL` seconds (default 10). `?refresh=1` forces a live scan;
  the "Run Diagnostic Scan" button and post-fix re-scans force refresh, while
  returning to the hub uses the cache for instant loads. Concurrent requests
  are coalesced behind a lock so a burst of tab switches cannot stampede the
  host with parallel subprocess runs. The scan never silently serves stale
  data: the response reports `cached`/`cached_age_ms` and the UI shows a badge.
- **The scanner is now failure-isolated end to end.** An unexpected exception
  in the process-state walk (the only previously unguarded sensor read) is
  caught, and a top-level guard returns a well-formed degraded scan instead of
  a 500, so a single sensor/subprocess glitch can no longer blank the hub.
- **`pip-audit` re-run:** no known vulnerabilities in `requirements.txt` or
  transitive runtime deps. (The environment's build-tool `setuptools` reports
  advisory IDs, but setuptools is not a runtime dependency of this application.)

## Auto-Fix Engine expansion (2026-08-21)

Six more remediation tools were added to the Troubleshoot Hub, all following
the existing security model (argv-only, non-interactive sudo, bounded
timeouts, reversible, owner/input-guarded):

- **stop_service / disable_service** — systemctl `stop`/`disable` for a target
  unit, reusing the existing validated service-action path and sudo policy.
- **clear_swap** — cycles swap off/on to reclaim used swap, but only executes
  when the swapped-out pages fit in available RAM with >=512 MiB headroom,
  refusing otherwise to avoid an OOM kill.
- **fstrim** — `fstrim -av` to TRIM unused storage blocks on SSDs.
- **tune_swappiness** — runtime `vm.swappiness=10` (not persisted).
- **raise_fd_limits** — runtime `fs.file-max` raise (capped at 20,000,000)
  offered directly on the File-Descriptor-Pressure check.

The health scanner now attaches fixes for swap pressure (clear_swap), storage
(fstrim), and file-descriptor pressure (raise_fd_limits). The installer's
sudoers policy (`systemd/install-service.sh`) was extended with
`MONITORX_SYSCTL` and `MONITORX_MAINTENANCE` aliases (the latter only when the
corresponding tool is present). `pip-audit` still reports no known
vulnerabilities.

## Residual operational requirements

- Keep MonitorX private or behind a trusted HTTPS reverse proxy. Use a long random token.
- Set `MONITORX_COOKIE_SECURE=true` when TLS terminates at a trusted proxy.
- Do not grant broader sudo rules than `systemd/install-service.sh` documents.
- Mount libvirt only when VM monitoring is required.
- Re-run `pip-audit` during CI/builds because advisory data and resolved dependency versions change over time.
- Review any future change that adds an external script/font, arbitrary command, shell execution, filesystem path, network target, or unauthenticated control route.
