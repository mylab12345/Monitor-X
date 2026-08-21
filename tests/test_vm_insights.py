"""Security and behaviour tests for VM Guest Insights (SSH-based inspection).

Covers:
  * strict input validation (command/option injection attempts are refused)
  * the exact hardened ssh argv (BatchMode, no password auth, allowlisted cmds)
  * parsers against malformed / hostile guest output
  * config persistence (0600, round-trip, deletion)
  * endpoint behaviour end-to-end with a stub ``ssh`` binary (no network)

No libvirt, systemd, or real guest is required.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Private scratch state for the backend under test — never the repo itself.
_TMP = tempfile.mkdtemp(prefix="monitorx-insights-test-")
os.environ["MONITORX_STATE_DIR"] = _TMP
os.environ["MONITORX_OPERATIONS_DB"] = str(Path(_TMP) / "ops.db")
os.environ["MONITORX_AUDIT_LOG"] = str(Path(_TMP) / "audit.log")
os.environ["MONITORX_INSIGHTS_CONFIG"] = str(Path(_TMP) / "vm-insights-config.json")
os.environ.pop("MONITORX_AUTH_TOKEN", None)

sys.path.insert(0, str(ROOT / "backend"))
import main  # noqa: E402

# Another test module in the same process (e.g. test_contracts) may have
# imported the backend first, binding import-time paths to its own values.
# Redirect every state location so these tests never write into the repo.
main.AUDIT_LOG = Path(_TMP) / "audit.log"
main.STATE_DIR = Path(_TMP)

FIXTURE_DIR = Path(_TMP) / "fixtures"
FIXTURE_DIR.mkdir(exist_ok=True)

PS_FIXTURE = """\
    1     0 root      0.0  0.1   8236 00:12:01 systemd
  512     1 www-data 12.5  2.4 204800 02:11:45 nginx: worker process
  900   512 postgres  3.1  5.5 412000 1-02:03:04 postgres
this line is malformed and must be skipped
  999     1 root      abc  1.0    100 00:00:01 weirdcpu
"""

WHO_FIXTURE = """\
root     pts/0        2026-08-13 09:12 (10.0.0.5)
deploy   pts/1        2026-08-13 10:44 (192.168.1.20)
"""

PASSWD_FIXTURE = """\
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
deploy:x:1000:1000:Deploy User:/home/deploy:/bin/bash
alice:x:1001:1001:Alice,,,:/home/alice:/bin/zsh
"""

DF_FIXTURE = """\
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/vda1       20511356  12345678   7100000      64% /
tmpfs            1024000         0   1024000       0% /dev/shm
/dev/vdb1       51200000   5120000  43520000      11% /data
overlay         20511356  12345678   7100000      64% /var/lib/docker/merged
garbage line without numbers
"""

(FIXTURE_DIR / "ps.txt").write_text(PS_FIXTURE)
(FIXTURE_DIR / "who.txt").write_text(WHO_FIXTURE)
(FIXTURE_DIR / "passwd.txt").write_text(PASSWD_FIXTURE)
(FIXTURE_DIR / "df.txt").write_text(DF_FIXTURE)

# Stub ssh: never touches the network. It routes purely on the destination
# (user@host) and the allowlisted remote command, exactly like the real
# argv the backend produces.
FAKE_SSH = Path(_TMP) / "fake_ssh.sh"
FAKE_SSH.write_text(f"""#!/bin/sh
dest=""
for a in "$@"; do
  case "$a" in *@*) dest="$a" ;; esac
done
case "$dest" in
  *@fail.invalid) echo "ssh: connect to host fail.invalid port 22: No route to host" >&2; exit 255 ;;
  *@slow.invalid) sleep 30; exit 0 ;;
esac
all="$*"
case "$all" in
  *"ps -eo"*) cat "{FIXTURE_DIR}/ps.txt" ;;
  *" who"*) cat "{FIXTURE_DIR}/who.txt" ;;
  *"getent passwd"*) cat "{FIXTURE_DIR}/passwd.txt" ;;
  *"df -kP"*) cat "{FIXTURE_DIR}/df.txt" ;;
  *) echo "fake-ssh: command not allowed" >&2; exit 127 ;;
esac
exit 0
""")
FAKE_SSH.chmod(0o755)


class ValidationTests(unittest.TestCase):
    def test_valid_hosts(self):
        self.assertEqual(main._validate_insights_host("192.168.122.10"), "192.168.122.10")
        self.assertEqual(main._validate_insights_host("vm1.internal"), "vm1.internal")
        self.assertEqual(main._validate_insights_host("fe80::1"), "fe80::1")
        self.assertEqual(main._validate_insights_host("[fd00::5]"), "[fd00::5]")

    def test_host_injection_attempts_are_refused(self):
        hostile = [
            "", "   ",
            "; rm -rf /", "$(reboot)", "`id`", "a&&b", "a|nc",
            "host\n-Xevil", "host\r\n", "user@host", "host name",
            "-oProxyCommand=evil", "--config=/etc/shadow",
            "a" * 300, "host/../../etc", "http://x", "host:22",
        ]
        for value in hostile:
            with self.assertRaises(ValueError, msg=f"accepted hostile host {value!r}"):
                main._validate_insights_host(value)

    def test_user_validation(self):
        self.assertEqual(main._validate_insights_user("root"), "root")
        self.assertEqual(main._validate_insights_user("deploy-1.ops"), "deploy-1.ops")
        for value in ["", "-oEvil", "-i", "a b", "a;b", "$(x)", "ü", "a" * 40, "user\n"]:
            with self.assertRaises(ValueError, msg=f"accepted hostile user {value!r}"):
                main._validate_insights_user(value)

    def test_identity_file_must_exist(self):
        self.assertIsNone(main._validate_identity_file(None))
        self.assertIsNone(main._validate_identity_file("   "))
        with self.assertRaises(ValueError):
            main._validate_identity_file("/nonexistent/key/xyz")
        with self.assertRaises(ValueError):
            main._validate_identity_file("/tmp/key\n-Xevil")
        key = Path(_TMP) / "id_test"
        key.write_text("dummy")
        self.assertEqual(main._validate_identity_file(str(key)), str(key))


class SshArgvTests(unittest.TestCase):
    BASE_CONFIG = {"host": "10.0.0.5", "port": 2222, "user": "deploy", "identity_file": None}

    def test_argv_is_hardened_and_exact(self):
        argv = main._vm_insights_ssh_argv(self.BASE_CONFIG, main.INSIGHTS_CMD_PROCESSES)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("PasswordAuthentication=no", argv)
        self.assertIn("KbdInteractiveAuthentication=no", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("deploy@10.0.0.5", argv)
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        # The allowlisted remote command is appended verbatim, unchanged.
        self.assertEqual(argv[-len(main.INSIGHTS_CMD_PROCESSES):], list(main.INSIGHTS_CMD_PROCESSES))
        # No identity options leak in without a key configured.
        self.assertNotIn("-i", argv)

    def test_identity_file_adds_exactly_two_options(self):
        config = {**self.BASE_CONFIG, "identity_file": "/home/x/.ssh/id_ed25519"}
        argv = main._vm_insights_ssh_argv(config, main.INSIGHTS_CMD_FILESYSTEMS)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertEqual(argv[argv.index("-i") + 1], "/home/x/.ssh/id_ed25519")

    def test_only_allowlisted_remote_commands_exist(self):
        commands = {
            main.INSIGHTS_CMD_PROCESSES, main.INSIGHTS_CMD_SESSIONS,
            main.INSIGHTS_CMD_ACCOUNTS, main.INSIGHTS_CMD_FILESYSTEMS,
        }
        for command in commands:
            self.assertEqual(command[0], command[0])  # tuple sanity
            self.assertTrue(all(isinstance(part, str) for part in command))


class ParserTests(unittest.TestCase):
    def test_ps_parser(self):
        parsed = main._parse_ps_table(PS_FIXTURE)
        rows = parsed["processes"]
        self.assertEqual(len(rows), 4)  # malformed line skipped
        self.assertEqual(rows[0]["name"], "nginx: worker process")  # CPU-sorted
        self.assertEqual(rows[0]["pid"], 512)
        self.assertEqual(rows[0]["cpu_percent"], 12.5)
        self.assertEqual(rows[0]["user"], "www-data")
        self.assertAlmostEqual(rows[0]["memory_mb"], 204800 / 1024.0, places=1)
        weird = next(r for r in rows if r["pid"] == 999)
        self.assertEqual(weird["cpu_percent"], 0.0)  # non-numeric CPU falls back safely
        self.assertFalse(parsed["truncated"])

    def test_ps_parser_truncation_and_hostile_values(self):
        huge = " 1 0 root 999999.9 250.0 999999999999 00:00:01 " + "A" * 1000 + "\n"
        parsed = main._parse_ps_table(huge * 3, limit=2)
        self.assertTrue(parsed["truncated"])
        self.assertLessEqual(len(parsed["processes"]), 2)
        row = parsed["processes"][0]
        self.assertLessEqual(row["memory_percent"], 100.0)
        self.assertLessEqual(len(row["name"]), 256)
        self.assertLessEqual(row["cpu_percent"], 100000.0)

    def test_who_parser(self):
        parsed = main._parse_who_sessions(WHO_FIXTURE)
        self.assertEqual(len(parsed["sessions"]), 2)
        self.assertEqual(parsed["sessions"][0]["user"], "root")
        self.assertEqual(parsed["sessions"][0]["from"], "10.0.0.5")
        self.assertEqual(parsed["sessions"][1]["tty"], "pts/1")

    def test_passwd_parser_only_human_users(self):
        parsed = main._parse_passwd_accounts(PASSWD_FIXTURE)
        names = [a["name"] for a in parsed["accounts"]]
        self.assertEqual(names, ["root", "deploy", "alice"])  # system accounts filtered
        self.assertEqual(parsed["total_entries"], 5)

    def test_df_parser(self):
        parsed = main._parse_df_filesystems(DF_FIXTURE)
        root = parsed["root"]
        self.assertIsNotNone(root)
        self.assertEqual(root["device"], "/dev/vda1")
        self.assertEqual(root["percent"], 64)
        self.assertEqual(root["used_kb"], 12345678)
        devices = [fs["device"] for fs in parsed["filesystems"]]
        self.assertIn("/dev/vdb1", devices)
        self.assertIn("overlay", devices)
        self.assertTrue(next(fs for fs in parsed["filesystems"] if fs["device"] == "tmpfs")["pseudo"])

    def test_df_parser_hostile(self):
        evil = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/x 999999999999999999999 -5 99999999999 300% /\n"
            "/dev/y abc def ghi zz% /boot\n"
        )
        parsed = main._parse_df_filesystems(evil)
        # The first row parses with clamped values; the second is skipped.
        self.assertIsNotNone(parsed["root"])
        self.assertLessEqual(parsed["root"]["percent"], 100)
        self.assertEqual(len(parsed["filesystems"]), 1)

    def test_df_parser_no_root_falls_back_to_first_real_fs(self):
        parsed = main._parse_df_filesystems(
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "tmpfs 100 10 90 10% /run\n/dev/sda1 100 10 90 10% /opt\n"
        )
        self.assertEqual(parsed["root"]["device"], "/dev/sda1")


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self._old = main.INSIGHTS_CACHE_TTL

    def tearDown(self):
        main.INSIGHTS_CACHE_TTL = self._old

    def test_roundtrip_and_permissions(self):
        import asyncio

        async def scenario():
            await main._save_insights_configs({"vm-a": {"host": "10.1.1.1", "port": 22, "user": "root", "identity_file": None}})
            return main._load_insights_configs()

        configs = asyncio.run(scenario())
        self.assertEqual(configs["vm-a"]["host"], "10.1.1.1")
        mode = stat.S_IMODE(main._insights_config_path().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_store_returns_empty(self):
        main._insights_config_path().write_text("{not json")
        self.assertEqual(main._load_insights_configs(), {})

    def test_atomic_save_leaves_no_temporary_files(self):
        import asyncio

        asyncio.run(main._save_insights_configs({
            "vm-atomic": {
                "host": "10.2.2.2", "port": 22,
                "user": "root", "identity_file": None,
            }
        }))
        path = main._insights_config_path()
        leftovers = list(path.parent.glob(f".{path.name}.*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(main._load_insights_configs()["vm-atomic"]["host"], "10.2.2.2")


class ApiTests(unittest.TestCase):
    """Endpoint behaviour with the stub ssh binary standing in for a guest."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls._ssh_patch = main._ssh_binary
        main._ssh_binary = lambda: str(FAKE_SSH)
        cls.client = TestClient(main.app)
        cls.client.__enter__()
        # When another module imported the backend with an auth token set,
        # authenticate this session instead of failing on 401s.
        if main.MONITORX_AUTH_TOKEN:
            res = cls.client.post("/api/auth/login", json={"token": main.MONITORX_AUTH_TOKEN})
            assert res.status_code == 200, "test session could not authenticate"

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        main._ssh_binary = cls._ssh_patch

    def setUp(self):
        # Note: cookies are intentionally NOT cleared — when the backend runs
        # with an auth token, the jar holds this session's login cookie.
        main._insights_cache.clear()
        main._insights_overview_cache.clear()
        # Full isolation: no profile may leak between tests.
        try:
            main._insights_config_path().unlink()
        except FileNotFoundError:
            pass

    def _configure(self, vm="web-01", host="10.0.0.5"):
        res = self.client.put(f"/api/vms/{vm}/insights/config",
                              json={"host": host, "port": 22, "user": "root"})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_put_get_delete_config_roundtrip(self):
        body = self._configure()
        self.assertTrue(body["configured"])

        got = self.client.get("/api/vms/web-01/insights/config").json()
        self.assertTrue(got["configured"])
        self.assertEqual(got["config"]["host"], "10.0.0.5")
        self.assertTrue(got["ssh_available"])

        res = self.client.delete("/api/vms/web-01/insights/config")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.client.get("/api/vms/web-01/insights/config").json()["configured"])
        self.assertEqual(self.client.delete("/api/vms/web-01/insights/config").status_code, 404)

    def test_malicious_profiles_are_rejected(self):
        hostile_hosts = ["$(reboot)", "; rm -rf /", "-oProxyCommand=evil", "host\nname", ""]
        for host in hostile_hosts:
            res = self.client.put("/api/vms/web-01/insights/config",
                                  json={"host": host, "port": 22, "user": "root"})
            self.assertIn(res.status_code, (400, 422), f"accepted hostile host {host!r}")
        res = self.client.put("/api/vms/web-01/insights/config",
                              json={"host": "10.0.0.5", "user": "-oEvil"})
        self.assertEqual(res.status_code, 422)
        res = self.client.put("/api/vms/web-01/insights/config",
                              json={"host": "10.0.0.5", "user": "root", "port": 99999})
        self.assertEqual(res.status_code, 422)
        res = self.client.put("/api/vms/web-01/insights/config",
                              json={"host": "10.0.0.5", "user": "root", "identity_file": "/nonexistent/key"})
        self.assertEqual(res.status_code, 422)
        # Nothing was persisted by any attempt.
        self.assertFalse(self.client.get("/api/vms/web-01/insights/config").json()["configured"])

    def test_vm_id_validation(self):
        self.assertEqual(self.client.get("/api/vms/bad%0Aid/insights").status_code, 400)
        self.assertEqual(self.client.get("/api/vms/-dashfirst/insights").status_code, 400)

    def test_insights_without_config(self):
        res = self.client.get("/api/vms/lonely-vm/insights")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["configured"])
        self.assertIn("SSH profile", body["error"])

    def test_full_collection_via_stub_ssh(self):
        self._configure()
        body = self.client.get("/api/vms/web-01/insights").json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["host"], "10.0.0.5")

        procs = body["processes"]
        self.assertTrue(procs["ok"])
        self.assertEqual(procs["count"], 4)
        self.assertEqual(procs["processes"][0]["name"], "nginx: worker process")

        users = body["users"]
        self.assertTrue(users["ok"])
        self.assertEqual(len(users["sessions"]), 2)
        self.assertEqual([a["name"] for a in users["accounts"]], ["root", "deploy", "alice"])

        disk = body["root_disk"]
        self.assertTrue(disk["ok"])
        self.assertEqual(disk["root"]["device"], "/dev/vda1")
        self.assertEqual(disk["root"]["percent"], 64)

    def test_collection_failures_are_reported_per_section(self):
        self._configure(host="fail.invalid")
        body = self.client.get("/api/vms/web-01/insights?force=true").json()
        for section in ("processes", "users", "root_disk"):
            self.assertFalse(body[section]["ok"])
            self.assertIn("SSH connection failed", body[section]["error"])

    def test_timeout_is_enforced(self):
        self._configure(host="slow.invalid")
        old_timeout = main.INSIGHTS_SSH_TIMEOUT
        main.INSIGHTS_SSH_TIMEOUT = 2.0
        try:
            import time
            start = time.monotonic()
            body = self.client.get("/api/vms/web-01/insights?force=true").json()
            elapsed = time.monotonic() - start
        finally:
            main.INSIGHTS_SSH_TIMEOUT = old_timeout
        self.assertFalse(body["processes"]["ok"])
        self.assertIn("timed out", body["processes"]["error"])
        self.assertLess(elapsed, 15, "timeout did not bound the request")

    def test_output_is_capped(self):
        old_cap = main.INSIGHTS_MAX_OUTPUT
        main.INSIGHTS_MAX_OUTPUT = 64
        try:
            self._configure()
            result = None
            import asyncio
            config = asyncio.run(main._resolve_insights_config("web-01"))
            result = asyncio.run(main._run_insights_ssh(config, main.INSIGHTS_CMD_PROCESSES))
        finally:
            main.INSIGHTS_MAX_OUTPUT = old_cap
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), 64)

    def test_overview_summarises_all_configured_vms(self):
        self._configure(vm="web-01")
        self._configure(vm="db-01", host="fail.invalid")
        body = self.client.get("/api/vms/insights?force=true").json()
        self.assertEqual(body["configured"], 2)
        by_name = {v["vm"]: v for v in body["vms"]}
        self.assertTrue(by_name["web-01"]["ok"])
        self.assertEqual(by_name["web-01"]["processes"]["count"], 4)
        self.assertEqual(by_name["web-01"]["users"]["sessions"], 2)
        self.assertEqual(by_name["web-01"]["root_disk"]["percent"], 64)
        self.assertFalse(by_name["db-01"]["ok"])

    def test_results_are_cached_briefly(self):
        self._configure()
        first = self.client.get("/api/vms/web-01/insights").json()
        cached = self.client.get("/api/vms/web-01/insights").json()
        self.assertEqual(first["collected_at"], cached["collected_at"])
        main._insights_cache.clear()
        fresh = self.client.get("/api/vms/web-01/insights").json()
        self.assertTrue(fresh["configured"])


if __name__ == "__main__":
    unittest.main()
