import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

import connection
from runtime import Pebrel, RuntimeErrorInfo

SHELLS = [path for name in ("pwsh.exe", "powershell.exe") if (path := shutil.which(name))]


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.original_environment = dict(os.environ)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record_path = Path(self.temp.name) / "connection.json"
        self.cli = str(Path(self.temp.name) / "pebrel.exe")
        for patcher in (patch.object(connection, "CONNECTION_FILE", self.record_path),
                        patch.dict(os.environ, {}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def register(self, cipher="encrypted-fixture", cli=None):
        self.record_path.write_text(json.dumps({"version": 1, "cli": cli or self.cli,
                                               "protected_endpoint": cipher}), encoding="utf-8")

    def test_desktop_start_uses_encrypted_registered_connection(self):
        self.register()
        with patch.object(connection, "unprotect", return_value="synthetic-endpoint") as decrypt:
            client = Pebrel(self.cli)
        self.assertEqual(client.environment["PEBREL_RUNTIME_ENDPOINT"], "synthetic-endpoint")
        decrypt.assert_called_once_with("encrypted-fixture")
        self.assertNotIn("PEBREL_RUNTIME_ENDPOINT", os.environ)

    def test_explicit_pebrel_environment_takes_precedence(self):
        self.register()
        with patch.dict(os.environ, {"PEBREL_RUNTIME_ENDPOINT": "inherited"}), patch.object(connection, "unprotect") as decrypt:
            client = Pebrel(self.cli)
        decrypt.assert_not_called()
        self.assertEqual(client.environment["PEBREL_RUNTIME_ENDPOINT"], "inherited")

    def test_other_installation_cache_is_not_used(self):
        self.register(cli="different-pebrel.exe")
        with patch.object(connection, "unprotect") as decrypt:
            client = Pebrel(self.cli)
        decrypt.assert_not_called()
        self.assertNotIn("PEBREL_RUNTIME_ENDPOINT", client.environment)

    def test_corrupt_or_undecryptable_registration_does_not_crash(self):
        self.record_path.write_text("broken json", encoding="utf-8")
        self.assertNotIn("PEBREL_RUNTIME_ENDPOINT", Pebrel(self.cli).environment)
        self.register()
        with patch.object(connection, "unprotect", side_effect=OSError("decrypt failed")):
            self.assertNotIn("PEBREL_RUNTIME_ENDPOINT", Pebrel(self.cli).environment)

    def test_new_clients_pick_up_restarted_host_without_retargeting_bound_client(self):
        self.register()
        with patch.object(connection, "unprotect", side_effect=["first-instance", "second-instance"]):
            bound = Pebrel(self.cli)
            refresh = Pebrel(self.cli)
        self.assertEqual(bound.environment["PEBREL_RUNTIME_ENDPOINT"], "first-instance")
        self.assertEqual(refresh.environment["PEBREL_RUNTIME_ENDPOINT"], "second-instance")

    def test_cli_receives_registered_environment_and_error_is_actionable(self):
        self.register()
        with patch.object(connection, "unprotect", return_value="synthetic-endpoint"):
            client = Pebrel(self.cli)
        response = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": {
            "code": "runtime_unavailable", "message": "no resident Pebrel runtime found"}}), "")
        with patch("runtime.os.path.isfile", return_value=True), patch("runtime.subprocess.run", return_value=response) as run:
            with self.assertRaises(RuntimeErrorInfo) as error:
                client.agents()
        self.assertEqual(run.call_args.kwargs["env"]["PEBREL_RUNTIME_ENDPOINT"], "synthetic-endpoint")
        self.assertEqual(error.exception.code, "runtime_unavailable")
        self.assertIn("自动重试", str(error.exception))

    @unittest.skipUnless(os.name == "nt" and SHELLS, "Windows DPAPI / PowerShell integration")
    def test_registration_hook_encrypts_and_replaces_connection_on_new_session(self):
        script = str(Path(__file__).with_name("register-runtime.ps1"))
        cache = Path(self.temp.name) / "CodexResumeHelper" / "connection.json"
        for shell in SHELLS:
            for endpoint in ("1000 synthetic-first", "1001 synthetic-restarted"):
                with self.subTest(shell=shell, endpoint=endpoint):
                    env = dict(self.original_environment, LOCALAPPDATA=self.temp.name,
                               TERM_PROGRAM="pebrel", PEBREL_CLI=self.cli, PEBREL_RUNTIME_ENDPOINT=endpoint)
                    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", script],
                                            env=env, capture_output=True, timeout=15,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.assertEqual(result.returncode, 0)
                    text = cache.read_text(encoding="utf-8-sig")
                    self.assertNotIn(endpoint, text)
                    record = json.loads(text)
                    self.assertEqual(connection.unprotect(record["protected_endpoint"]), endpoint)


class PortableCliTests(unittest.TestCase):
    def test_bundled_default_uses_current_windows_account(self):
        from app import cli_default
        with patch.dict(os.environ, {"LOCALAPPDATA": r"D:\Users\Example\AppData\Local"}, clear=True):
            self.assertEqual(cli_default({}), r"D:\Users\Example\AppData\Local\Programs\Pebrel\pebrel.exe")

    def test_explicit_connection_setting_has_priority(self):
        from app import cli_default
        with patch.dict(os.environ, {"PEBREL_CLI": r"D:\Pebrel\pebrel.exe"}, clear=True):
            self.assertEqual(cli_default({"cli": r"E:\Custom\pebrel.exe"}), r"E:\Custom\pebrel.exe")

    def test_terminal_environment_has_priority_over_bundled_default(self):
        from app import cli_default
        for variable in ("PEBREL_CLI", "NEBULA_CLI"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: r"D:\Pebrel\pebrel.exe"}, clear=True):
                self.assertEqual(cli_default({}), r"D:\Pebrel\pebrel.exe")


if __name__ == "__main__":
    unittest.main()
