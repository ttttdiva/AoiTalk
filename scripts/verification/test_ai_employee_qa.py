"""Focused launcher guard tests, independent of repository-wide conftest."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verification.ai_employee_qa import (
    check_identity, child_environment, load_manifest, write_json,
)


class LauncherBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.run_id = uuid4()
        self.name = "aoitalk_test_employee_" + self.run_id.hex

    def test_rejects_live_or_ambiguous_database_names(self):
        self.assertEqual(check_identity(self.name), self.name)
        for value in ("aoitalk_memory", "public", "aoitalk_test_employee_", self.name + ";DROP", self.name.upper(), "../" + self.name):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_identity(value)

    def test_child_environment_cannot_inherit_live_db_or_provider_secrets(self):
        manifest = {"output_dir": "task-output", "database": self.name, "pg_host": "127.0.0.1", "pg_port": 55432,
                    "backend_port": 3100, "frontend_port": 3102}
        private = {key: "qa-only" for key in ("pg_user", "pg_password", "field_key", "web_secret", "next_secret", "jwt_secret", "internal_key")}
        with patch.dict(os.environ, {"POSTGRES_SCHEMA": "live", "POSTGRES_DB": "aoitalk_memory", "OPENAI_API_KEY": "live-provider",
                "POSTGRES_PASSWORD_FILE": "live-secret-file", "PYTHON_DOTENV_DISABLED": "0"}):
            env = child_environment(manifest, private)
        self.assertEqual(env["POSTGRES_DB"], self.name)
        self.assertEqual(env["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(env["AOITALK_ALLOW_UNAUTHENTICATED_DEV"], "false")
        for key in ("POSTGRES_SCHEMA", "OPENAI_API_KEY", "POSTGRES_PASSWORD_FILE"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PYTHON_API_URL"], "http://127.0.0.1:3100")
        self.assertEqual(env["NEXT_PUBLIC_AOITALK_WS_PORT"], "3100")
        self.assertNotIn("live-provider", json.dumps(env))

    def test_manifest_rejects_unowned_paths_and_existing_app_ports(self):
        from scripts.verification.ai_employee_qa import REPO
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / self.name
            root.mkdir()
            manifest = {"harness": "ai_employee_qa", "database": self.name, "run_id": str(self.run_id), "schema": "public",
                "repo": str(REPO), "output_dir": str(root), "pg_host": "127.0.0.1", "pg_port": 55432, "backend_port": 3100, "frontend_port": 3102}
            path = root / "manifest.json"
            write_json(path, manifest)
            self.assertEqual(load_manifest(path)["database"], self.name)
            for key, value in (("database", "aoitalk_memory"), ("backend_port", 3000), ("frontend_port", 3002),
                    ("schema", "other"), ("output_dir", str(root.parent)), ("pg_host", "remote.example"), ("run_id", str(uuid4()))):
                with self.subTest(key=key):
                    write_json(path, {**manifest, key: value})
                    with self.assertRaises(ValueError):
                        load_manifest(path)

    def test_network_guard_rejects_provider_and_existing_app_before_connect(self):
        code = """
import socket
from scripts.verification.ai_employee_qa import restrict_backend_network
restrict_backend_network({'pg_port':55432,'backend_port':3100,'frontend_port':3102})
pair = socket.socketpair()
pair[0].send(b'x')
assert pair[1].recv(1) == b'x'
for sock in pair: sock.close()
for address in [('203.0.113.1',443),('127.0.0.1',3000),('127.0.0.1',3002),('127.0.0.1',5432)]:
    try:
        with socket.socket() as sock:
            sock.connect(address)
    except PermissionError as exc:
        assert str(exc) == 'QA outbound network denied'
    else:
        raise AssertionError('outbound connection allowed')
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_artifact_replace_leaves_valid_json_without_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            write_json(path, {"stage": "prepared"})
            write_json(path, {"stage": "ready"})
            self.assertEqual(json.loads(path.read_text()), {"stage": "ready"})
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_phone_features_are_explicitly_local_fixture_only(self):
        manifest = {"output_dir": "task-output", "database": self.name, "pg_host": "127.0.0.1", "pg_port": 55432,
                    "backend_port": 3100, "frontend_port": 3102}
        private = {key: "qa-only" for key in ("pg_user", "pg_password", "field_key", "web_secret", "next_secret", "jwt_secret", "internal_key")}
        base = child_environment(manifest, private)
        phone = child_environment({**manifest, "phone_fixture": True}, private)
        self.assertEqual(base["FEATURE_VOICE_INPUT"], "false")
        self.assertEqual(base["FEATURE_TTS_OUTPUT"], "false")
        self.assertEqual(phone["FEATURE_VOICE_INPUT"], "true")
        self.assertEqual(phone["FEATURE_TTS_OUTPUT"], "true")
        self.assertNotIn("OPENAI_API_KEY", phone)

    def test_private_cleanup_requires_stopped_cluster_and_keeps_evidence(self):
        from scripts.verification.ai_employee_qa import REPO, cleanup
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / self.name
            (root / "postgres").mkdir(parents=True)
            manifest = {"harness": "ai_employee_qa", "database": self.name, "run_id": str(self.run_id), "schema": "public",
                "repo": str(REPO), "output_dir": str(root), "pg_host": "127.0.0.1", "pg_port": 55432,
                "backend_port": 3100, "frontend_port": 3102, "processes": {}, "dedicated_cluster": True, "status": "database_dropped"}
            write_json(root / "manifest.json", manifest)
            write_json(root / ".runtime-private.json", {"dummy": "private-fixture"})
            write_json(root / "provider-evidence.json", {"submission_count": 1})
            write_json(Path(temp) / ".env.qa-login", {"dummy": "canonical-source-sentinel"})
            (root / "postgres/postmaster.pid").write_text("123")
            with self.assertRaises(RuntimeError):
                cleanup(manifest)
            self.assertTrue((root / ".runtime-private.json").exists())
            (root / "postgres/postmaster.pid").unlink()
            cleanup(manifest)
            self.assertFalse((root / ".runtime-private.json").exists())
            self.assertTrue((root / "provider-evidence.json").exists())
            self.assertTrue((Path(temp) / ".env.qa-login").exists())
            self.assertEqual(load_manifest(root / "manifest.json")["cleanup"]["private_files_removed"], [".runtime-private.json"])


if __name__ == "__main__":
    unittest.main()
