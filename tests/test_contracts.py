"""Small smoke/contract tests for the MonitorX quality pass.

The runtime smoke tests deliberately avoid requiring libvirt, NVIDIA, systemd, or
any container/Kubernetes tooling. They verify the dashboard still boots and the
removed integrations are not accidentally reintroduced.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "main.py"
FRONTEND = ROOT / "frontend"


class SourceContracts(unittest.TestCase):
    def test_container_and_kubernetes_cli_integrations_are_removed(self):
        backend = BACKEND.read_text()
        self.assertNotIn("get_docker_", backend)
        self.assertNotIn("get_kubernetes_", backend)
        self.assertNotIn("kubectl", backend)
        self.assertNotIn('"docker"', backend)
        self.assertNotIn("containers: Optional", backend)
        self.assertNotIn("pods: Optional", backend)

        html = (FRONTEND / "index.html").read_text().lower()
        self.assertNotIn("kubernetes", html)
        self.assertNotIn("docker containers", html)
        self.assertNotIn("pods-panel", html)

    def test_secure_local_default_and_shared_telemetry_bus(self):
        backend = BACKEND.read_text()
        app = (FRONTEND / "js" / "app.js").read_text()
        self.assertIn('MONITORX_HOST", "127.0.0.1"', backend)
        self.assertIn("MONITORX_AUTH_TOKEN", backend)
        self.assertIn("monitorx:stats", app)
        # Only app.js may own the shared /ws connection; no frontend module
        # opens a second raw telemetry socket.
        for script in (FRONTEND / "js").glob("*.js"):
            if script.name == "app.js":
                continue
            self.assertNotIn(
                "new WebSocket(proto + '://' + window.location.host + '/ws')",
                script.read_text(),
            )

    def test_flight_control_loop_board_is_removed(self):
        html = (FRONTEND / "index.html").read_text().lower()
        self.assertNotIn("mission-board", html)
        self.assertNotIn("mission-control.js", html)
        self.assertNotIn("mission-control.css", html)
        self.assertFalse((FRONTEND / "js" / "mission-control.js").exists())
        self.assertFalse((FRONTEND / "css" / "mission-control.css").exists())

    def test_all_static_buttons_have_type(self):
        import re

        html = (FRONTEND / "index.html").read_text()
        buttons = re.findall(r"<button\b[^>]*>", html, re.IGNORECASE)
        self.assertTrue(buttons)
        self.assertTrue(all(re.search(r"\btype\s*=", button, re.IGNORECASE) for button in buttons))

    def test_vm_insights_ssh_path_is_hardened(self):
        backend = BACKEND.read_text()
        # Hardened ssh: never interactive, never password-authenticated.
        self.assertIn("BatchMode=yes", backend)
        self.assertIn("PasswordAuthentication=no", backend)
        self.assertIn("KbdInteractiveAuthentication=no", backend)
        # The guest command allowlist exists and stays pinned to exactly
        # four read-only tools — any change needs a security review.
        self.assertIn('INSIGHTS_CMD_PROCESSES = ("ps"', backend)
        self.assertIn('INSIGHTS_CMD_SESSIONS = ("who",)', backend)
        self.assertIn('INSIGHTS_CMD_ACCOUNTS = ("getent", "passwd")', backend)
        self.assertIn('INSIGHTS_CMD_FILESYSTEMS = ("df", "-kP")', backend)
        # No shell interpreter is ever spawned anywhere in the backend.
        self.assertNotIn("shell=True", backend)
        self.assertNotIn("create_subprocess_shell", backend)
        # Profiles store a key *path* only; input validation is mandatory.
        self.assertIn("_validate_identity_file", backend)
        self.assertIn("_validate_insights_host", backend)
        self.assertIn("_validate_insights_user", backend)


class RuntimeSmoke(unittest.TestCase):
    """Run only when setup.sh dependencies are available."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("FastAPI dependencies are not installed")

        os.environ.setdefault("MONITORX_AUTH_TOKEN", "contract-test-secret")
        cls.db = tempfile.NamedTemporaryFile(prefix="monitorx-test-", suffix=".db", delete=False)
        cls.db.close()
        os.environ["MONITORX_OPERATIONS_DB"] = cls.db.name
        sys.path.insert(0, str(ROOT / "backend"))
        import main

        # Whole-suite runs may import the backend in another test module
        # before this class configures the token; re-sync the import-time
        # value so the authentication contract under test is preserved.
        if main.MONITORX_AUTH_TOKEN != os.environ["MONITORX_AUTH_TOKEN"]:
            main.MONITORX_AUTH_TOKEN = os.environ["MONITORX_AUTH_TOKEN"]

        cls.main = main
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        try:
            Path(cls.db.name).unlink(missing_ok=True)
        except Exception:
            pass

    def test_health_is_public_and_control_api_is_protected(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/stats/processes").status_code, 401)
        self.assertEqual(
            self.client.post("/api/auth/login", json={"token": "contract-test-secret"}).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/stats/processes").status_code, 200)

    def test_removed_routes_are_not_registered(self):
        self.assertEqual(self.client.get("/api/stats/containers").status_code, 404)
        self.assertEqual(self.client.get("/api/stats/pods").status_code, 404)

    def test_authenticated_websocket_schema(self):
        self.client.post("/api/auth/login", json={"token": "contract-test-secret"})
        with self.client.websocket_connect("/ws") as websocket:
            frame = websocket.receive_json()
        self.assertNotIn("containers", frame)
        self.assertNotIn("pods", frame)
        self.assertIn("processes", frame)


if __name__ == "__main__":
    unittest.main()
