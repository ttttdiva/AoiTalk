"""Focused launcher guard tests, independent of repository-wide conftest."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verification.ai_employee_qa import (
    QA_SESSION_COOKIE_NAME, REPO, check_identity, child_environment, load_manifest,
    qa_artifact_path, qa_run_path, write_json,
)


def _temporary_directory():
    parent = REPO / ".local" / "qa-script-tests"
    parent.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=parent)


class LauncherBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.run_id = uuid4()
        self.name = "aoitalk_test_employee_" + self.run_id.hex

    def test_rejects_live_or_ambiguous_database_names(self):
        self.assertEqual(check_identity(self.name), self.name)
        for value in ("aoitalk_memory", "public", "aoitalk_test_employee_", self.name + ";DROP", self.name.upper(), "../" + self.name):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_identity(value)

    def test_run_output_path_is_confined_to_repository_scratch(self):
        with _temporary_directory() as temp:
            repository = Path(temp) / "checkout"
            repository.mkdir()
            path = qa_run_path(self.name, repository=repository)

            self.assertEqual(
                path,
                repository.resolve() / ".local" / "qa-runs" / self.name,
            )
            with self.assertRaises(ValueError):
                qa_run_path("../" + self.name, repository=repository)

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
        self.assertEqual(env["AOITALK_SESSION_COOKIE_NAME"], QA_SESSION_COOKIE_NAME)
        self.assertNotIn("live-provider", json.dumps(env))

    def test_manifest_rejects_unowned_paths_and_existing_app_ports(self):
        from scripts.verification.ai_employee_qa import REPO
        with _temporary_directory() as temp:
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
        with _temporary_directory() as temp:
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
        with _temporary_directory() as temp:
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

    def test_cleanup_archives_evidence_and_removes_new_qa_scratch(self):
        from scripts.verification.ai_employee_qa import cleanup

        root = qa_run_path(self.name)
        artifact_root = qa_artifact_path(self.name)
        root.mkdir(parents=True)
        (root / "screenshots").mkdir()
        (root / "screenshots" / "review.png").write_bytes(b"fixture")
        manifest = {
            "harness": "ai_employee_qa",
            "database": self.name,
            "run_id": str(self.run_id),
            "schema": "public",
            "repo": str(REPO),
            "output_dir": str(root),
            "pg_host": "127.0.0.1",
            "pg_port": 55432,
            "backend_port": 3100,
            "frontend_port": 3102,
            "processes": {},
            "dedicated_cluster": False,
            "status": "database_dropped",
        }
        write_json(root / "manifest.json", manifest)
        write_json(root / "provider-evidence.json", {"submission_count": 1})
        write_json(root / ".runtime-private.json", {"fixture": "private"})
        (root / "browser.log").write_text("safe test log\n", encoding="utf-8")

        try:
            archived_manifest = cleanup(manifest)

            self.assertEqual(archived_manifest, artifact_root / "manifest.json")
            self.assertFalse(root.exists())
            archived = load_manifest(archived_manifest)
            self.assertEqual(archived["output_dir"], str(artifact_root))
            self.assertTrue((artifact_root / "provider-evidence.json").is_file())
            self.assertTrue((artifact_root / "browser.log").is_file())
            self.assertTrue((artifact_root / "screenshots" / "review.png").is_file())
            self.assertFalse((artifact_root / ".runtime-private.json").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(artifact_root, ignore_errors=True)

    def test_cleanup_resumes_from_partial_owned_archive(self):
        from scripts.verification.ai_employee_qa import cleanup

        root = qa_run_path(self.name)
        artifact_root = qa_artifact_path(self.name)
        root.mkdir(parents=True)
        write_json(root / "manifest.json", {
            "harness": "ai_employee_qa",
            "database": self.name,
            "run_id": str(self.run_id),
            "schema": "public",
            "repo": str(REPO),
            "output_dir": str(root),
            "pg_host": "127.0.0.1",
            "pg_port": 55432,
            "backend_port": 3100,
            "frontend_port": 3102,
            "processes": {},
            "dedicated_cluster": False,
            "status": "database_dropped",
        })
        (root / "provider-evidence.json").write_text(
            '{"submission_count":1}\n', encoding="utf-8"
        )
        artifact_root.mkdir(parents=True)
        write_json(artifact_root / ".archive-owner.json", {
            "run_id": str(self.run_id),
            "database": self.name,
        })
        (artifact_root / "partial-copy.bin").write_bytes(b"partial")
        manifest = load_manifest(root / "manifest.json")

        try:
            archived_manifest = cleanup(manifest)

            self.assertEqual(archived_manifest, artifact_root / "manifest.json")
            self.assertFalse(root.exists())
            self.assertTrue((artifact_root / "provider-evidence.json").is_file())
            self.assertFalse((artifact_root / "partial-copy.bin").exists())
            self.assertFalse((artifact_root / ".archive-owner.json").exists())
            self.assertEqual(load_manifest(archived_manifest)["run_id"], str(self.run_id))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(artifact_root, ignore_errors=True)

    def test_cleanup_does_not_overwrite_another_runs_archive(self):
        from scripts.verification.ai_employee_qa import cleanup

        root = qa_run_path(self.name)
        artifact_root = qa_artifact_path(self.name)
        root.mkdir(parents=True)
        artifact_root.mkdir(parents=True)
        (artifact_root / "foreign.txt").write_text("keep\n", encoding="utf-8")
        manifest = {
            "harness": "ai_employee_qa",
            "database": self.name,
            "run_id": str(self.run_id),
            "schema": "public",
            "repo": str(REPO),
            "output_dir": str(root),
            "pg_host": "127.0.0.1",
            "pg_port": 55432,
            "backend_port": 3100,
            "frontend_port": 3102,
            "processes": {},
            "dedicated_cluster": False,
            "status": "database_dropped",
        }
        write_json(root / "manifest.json", manifest)

        try:
            with self.assertRaises(RuntimeError):
                cleanup(manifest)
            self.assertTrue(root.exists())
            self.assertEqual(
                (artifact_root / "foreign.txt").read_text(encoding="utf-8"),
                "keep\n",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(artifact_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
