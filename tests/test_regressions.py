"""Regression tests for the bugs found in the 2026-08-13 codebase review.

Each test here pins a specific defect so it cannot silently return. They are
written to run without libvirt, NVIDIA, systemd, or any container tooling.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "main.py"
FRONTEND = ROOT / "frontend"


def _load_main():
    """Import backend.main with an isolated on-disk state directory."""
    try:
        import fastapi  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("FastAPI dependencies are not installed")
    tmp = tempfile.mkdtemp(prefix="monitorx-regress-")
    os.environ["MONITORX_STATE_DIR"] = tmp
    os.environ["MONITORX_OPERATIONS_DB"] = str(Path(tmp) / "ops.db")
    sys.path.insert(0, str(ROOT / "backend"))
    import main
    return main


class OpsConnectionLifecycle(unittest.TestCase):
    """P0-1: `with sqlite3.connect(...)` commits but does NOT close."""

    def test_sqlite_context_manager_does_not_close(self):
        # Documents the stdlib behaviour the original code misread.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE t(x)")
            conn.execute("SELECT 1")  # still open -> no exception
            conn.close()

    def test_ops_conn_closes_connection(self):
        main = _load_main()
        with main._ops_conn() as conn:
            conn.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_ops_conn_closes_on_exception(self):
        main = _load_main()
        leaked = None
        with self.assertRaises(RuntimeError):
            with main._ops_conn() as conn:
                leaked = conn
                raise RuntimeError("boom")
        with self.assertRaises(sqlite3.ProgrammingError):
            leaked.execute("SELECT 1")

    def test_repeated_use_does_not_leak_descriptors(self):
        main = _load_main()
        main.init_operations_store()  # schema is created by lifespan, not import
        fd_dir = "/proc/self/fd"
        if not os.path.isdir(fd_dir):
            self.skipTest("no /proc")
        for _ in range(25):  # warm up
            with main._ops_conn() as conn:
                conn.execute("SELECT 1")
        before = len(os.listdir(fd_dir))
        for _ in range(300):
            with main._ops_conn() as conn:
                conn.execute("SELECT count(*) FROM alert_rules")
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after - before, 2, f"fd growth {before}->{after}")


class StateFileSecurity(unittest.TestCase):
    """P0-3: state must not live at predictable world-readable /tmp paths."""

    def test_no_hardcoded_tmp_state_paths(self):
        src = BACKEND.read_text()
        self.assertNotIn('Path("/tmp/', src)
        self.assertNotIn('"/tmp/monitorx-audit.log"', src)
        self.assertNotIn('"/tmp/monitorx_metrics.db"', src)

    def test_audit_log_is_owner_only(self):
        main = _load_main()
        main._append_audit_line("regression test entry")
        self.assertTrue(main.AUDIT_LOG.exists())
        self.assertEqual(main.AUDIT_LOG.stat().st_mode & 0o777, 0o600)

    def test_audit_log_refuses_symlink_target(self):
        main = _load_main()
        target = main.AUDIT_LOG.parent / "hijacked.txt"
        if main.AUDIT_LOG.exists():
            main.AUDIT_LOG.unlink()
        os.symlink(target, main.AUDIT_LOG)
        try:
            main._append_audit_line("should not follow symlink")
            self.assertFalse(target.exists(), "audit write followed a symlink")
        finally:
            if main.AUDIT_LOG.is_symlink():
                main.AUDIT_LOG.unlink()

    def test_operations_db_is_owner_only(self):
        main = _load_main()
        with main._ops_conn() as conn:
            conn.execute("SELECT 1")
        self.assertEqual(Path(main.OPERATIONS_DB).stat().st_mode & 0o777, 0o600)


class RuntimeConfiguration(unittest.TestCase):
    """Malformed environment values must not crash or weaken startup."""

    def test_numeric_environment_values_are_bounded(self):
        main = _load_main()
        os.environ["MONITORX_TEST_NUMBER"] = "NaN"
        try:
            self.assertEqual(
                main._env_number("MONITORX_TEST_NUMBER", 7.0, minimum=1.0),
                7.0,
            )
            os.environ["MONITORX_TEST_NUMBER"] = "999"
            self.assertEqual(
                main._env_number("MONITORX_TEST_NUMBER", 7, maximum=50),
                50,
            )
        finally:
            os.environ.pop("MONITORX_TEST_NUMBER", None)

    def test_only_loopback_binds_may_run_without_auth(self):
        main = _load_main()
        for host in ("127.0.0.1", "::1", "[::1]", "localhost"):
            self.assertTrue(main._bind_is_loopback(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.20", "monitor.example"):
            self.assertFalse(main._bind_is_loopback(host), host)

    def test_lifespan_refuses_unauthenticated_network_bind(self):
        import asyncio

        main = _load_main()
        old_host, old_token = main.MONITORX_HOST, main.MONITORX_AUTH_TOKEN
        main.MONITORX_HOST, main.MONITORX_AUTH_TOKEN = "0.0.0.0", ""

        async def start():
            async with main.lifespan(main.app):
                self.fail("unsafe application startup unexpectedly succeeded")

        try:
            with self.assertRaisesRegex(RuntimeError, "MONITORX_AUTH_TOKEN"):
                asyncio.run(start())
        finally:
            main.MONITORX_HOST, main.MONITORX_AUTH_TOKEN = old_host, old_token


class SignalShadowing(unittest.TestCase):
    """P1-4: the `signal` parameter shadowed the `signal` module."""

    def test_kill_process_does_not_shadow_signal_module(self):
        main = _load_main()
        import inspect
        params = inspect.signature(main.kill_process).parameters
        self.assertNotIn("signal", params)
        self.assertIn("sig", params)

    def test_signal_query_alias_is_preserved(self):
        main = _load_main()
        import inspect
        default = inspect.signature(main.kill_process).parameters["sig"].default
        self.assertEqual(getattr(default, "alias", None), "signal")


class DuplicateMetricStore(unittest.TestCase):
    """P1-5: the second /tmp time-series store is gone."""

    def test_single_metric_store(self):
        src = BACKEND.read_text()
        self.assertNotIn("DB_PATH", src)
        self.assertNotIn("monitorx_metrics.db", src)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS metrics ", src)


class BlockingCollectors(unittest.TestCase):
    """P1-6: psutil scans must not block the event loop."""

    def test_process_iter_calls_are_offloaded(self):
        src = BACKEND.read_text().splitlines()
        offenders = []
        for i, line in enumerate(src, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "psutil.process_iter(" in line or "psutil.net_connections(" in line:
                window = "\n".join(src[max(0, i - 12):i])
                if "def _" in window and "_sync" in window:
                    continue  # already inside a dedicated sync helper
                offenders.append(f"{i}: {stripped[:70]}")
        self.assertEqual(offenders, [], "blocking psutil scans on the event loop")


class SecurityHeaders(unittest.TestCase):
    """P1-7: CSP and framing protection."""

    def test_csp_and_frame_headers_present(self):
        main = _load_main()
        from fastapi.testclient import TestClient
        with TestClient(main.app) as client:
            res = client.get("/api/health")
        csp = res.headers.get("content-security-policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertEqual(res.headers.get("x-frame-options"), "DENY")


class SessionTokens(unittest.TestCase):
    """P1-8: the cookie must not be the shared secret itself."""

    def test_cookie_is_not_the_raw_token(self):
        token = "regression-shared-secret-value"
        os.environ["MONITORX_AUTH_TOKEN"] = token
        for mod in [m for m in list(sys.modules) if m == "main"]:
            del sys.modules[mod]
        main = _load_main()
        from fastapi.testclient import TestClient
        try:
            with TestClient(main.app) as client:
                res = client.post("/api/auth/login", json={"token": token})
                self.assertEqual(res.status_code, 200)
                cookie = client.cookies.get(main.AUTH_COOKIE_NAME)
                self.assertIsNotNone(cookie)
                self.assertNotEqual(cookie, token)
                # session id must still authenticate
                self.assertEqual(client.get("/api/stats/processes").status_code, 200)
                # and logout must revoke it server-side
                client.post("/api/auth/logout")
                client.cookies.set(main.AUTH_COOKIE_NAME, cookie)
                self.assertEqual(client.get("/api/stats/processes").status_code, 401)
        finally:
            os.environ.pop("MONITORX_AUTH_TOKEN", None)
            sys.modules.pop("main", None)


class DeadCode(unittest.TestCase):
    """P2: unused files and imports."""

    def test_orphan_theme_css_removed(self):
        self.assertFalse((FRONTEND / "css" / "theme.css").exists())

    def test_unused_jinja2_dependency_dropped(self):
        src = BACKEND.read_text()
        self.assertNotIn("Jinja2Templates", src)
        reqs = [
            line.split("#", 1)[0].strip().lower()
            for line in (ROOT / "requirements.txt").read_text().splitlines()
        ]
        self.assertNotIn("jinja2", [r.split("=")[0].split(">")[0].split("<")[0] for r in reqs if r])


class SecurityHardeningContracts(unittest.TestCase):
    """Regression coverage for the repository-wide 2026-08-16 hardening."""

    def test_no_third_party_runtime_assets(self):
        html = (FRONTEND / "index.html").read_text()
        app_js = (FRONTEND / "js" / "app.js").read_text()
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("cdn.jsdelivr.net", app_js)
        self.assertTrue((FRONTEND / "vendor" / "simple-terminal.js").is_file())

    def test_compose_requires_authentication_token(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("MONITORX_AUTH_TOKEN:?", compose)
        self.assertNotIn("MONITORX_AUTH_TOKEN:-", compose)

    def test_sensitive_process_environment_is_not_returned(self):
        main = _load_main()
        import asyncio
        detail = asyncio.run(main.get_process_detail(os.getpid()))
        self.assertNotIn("environ", detail)

    def test_cross_site_websocket_origin_is_rejected(self):
        main = _load_main()

        class FakeSocket:
            headers = {"host": "monitor.example", "origin": "https://evil.example"}

        self.assertFalse(main._websocket_origin_allowed(FakeSocket()))
        FakeSocket.headers["origin"] = "https://monitor.example"
        self.assertTrue(main._websocket_origin_allowed(FakeSocket()))

    def test_private_probe_targets_are_rejected(self):
        main = _load_main()
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"):
            self.assertIsNotNone(main._reject_ssrf_target(host, 80), host)

    def test_log_search_is_literal_not_attacker_regex(self):
        src = BACKEND.read_text()
        self.assertIn("re.compile(re.escape(search), re.IGNORECASE)", src)
        self.assertNotIn("re.compile(search, re.IGNORECASE)", src)

    def test_dhclient_release_flag_is_preserved(self):
        src = BACKEND.read_text()
        self.assertIn("await _sudo_cmd([path, *args], timeout=25.0)", src)
        self.assertNotIn("await _sudo_cmd([path, *args[1:]], timeout=25.0)", src)


class VirshArgvContract(unittest.TestCase):
    """P2-13: sudoers policy and runtime argv must stay in sync."""

    def test_virsh_argv_matches_sudoers_policy(self):
        main = _load_main()
        cmd = main._virsh_command("start", "demo-guest")
        self.assertIn("--no-pkttyagent", cmd)
        self.assertIn("--connect", cmd)
        self.assertIn(main.LIBVIRT_URI, cmd)
        self.assertEqual(cmd[-2:], ["--", "demo-guest"])
        installer = (ROOT / "systemd" / "install-service.sh").read_text()
        self.assertIn("--no-pkttyagent", installer)

    def test_poweroff_maps_to_destroy(self):
        main = _load_main()
        self.assertEqual(main.VM_ACTION_TO_VIRSH["poweroff"], "destroy")
        self.assertNotIn("poweroff", main._virsh_command("poweroff", "g"))


if __name__ == "__main__":
    unittest.main()
