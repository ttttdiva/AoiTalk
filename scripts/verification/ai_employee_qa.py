"""Isolated, authenticated AI employee QA runtime (never a production launcher).

Run with the repository venv Python. Commands:
  prepare [--node PATH] [--postgres-bin PATH]
  start --manifest PATH
  upgrade --manifest PATH
  refresh-frontend --manifest PATH
  check --manifest PATH
  browser --manifest PATH
  verify-journey --manifest PATH
  concurrency --manifest PATH  (owned app helpers must be stopped)
  migration-test --manifest PATH
  recovery-check --manifest PATH
  ui-smoke --manifest PATH  (copies only the two reviewed UI repair files)
  phone-smoke --manifest PATH  (enables explicit local phone fixture DI)
  cleanup --manifest PATH  (drops only the owned DB and removes private files)
  stop --manifest PATH [--drop-database]

prepare uses an existing PostgreSQL admin credential only when PGPASSWORD is
explicitly available. Otherwise it creates a task-owned PostgreSQL cluster.
No live database migration, auth bypass, provider call, or canonical CI runner
is used. Next sources are copied to the run directory to isolate generated
next-env/tsconfig/build files; installed node_modules are linked read-only by
convention. Re-run prepare for a fresh source snapshot and clean fixtures.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4


REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
PREFIX = "aoitalk_test_employee_"
IDENTIFIER = re.compile(r"aoitalk_test_employee_[0-9a-f]{32}\Z")
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))


def check_identity(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Refusing a database outside the exact QA UUID namespace")
    return value


def read_qa_credentials() -> tuple[str, str]:
    from dotenv import dotenv_values

    values = dotenv_values(REPO / ".env.qa-login", interpolate=False)
    username = values.get("AOITALK_QA_ADMIN_USERNAME")
    password = values.get("AOITALK_QA_ADMIN_PASSWORD")
    if not username or not password:
        raise RuntimeError(".env.qa-login must define the QA admin username/password")
    return username, password


def write_json(path: Path, value: dict) -> None:
    # Only task-created artifacts; never a repository configuration file.
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def migration_hashes() -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((REPO / "alembic/versions").glob("*.py"))}


def source_provenance() -> dict:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(REPO), *args], creationflags=HIDDEN, stderr=subprocess.PIPE)
    names = set(filter(None, (git("diff", "HEAD", "--name-only", "-z") +
        git("ls-files", "--others", "--exclude-standard", "-z")).decode("utf-8").split("\0")))
    return {"head": git("rev-parse", "HEAD").decode().strip(),
        "dirty_file_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            if (REPO / name).is_file() else None for name in sorted(names)},
        "captured_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}


def load_manifest(path: Path) -> dict:
    path = path.resolve(strict=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    name = check_identity(data["database"])
    if (data.get("harness") != "ai_employee_qa" or data.get("schema") != "public"
            or path.name != "manifest.json" or path.parent.name != name
            or Path(data["output_dir"]).resolve() != path.parent
            or Path(data["repo"]).resolve() != REPO):
        raise ValueError("QA manifest identity/path mismatch")
    if name != PREFIX + UUID(data["run_id"]).hex:
        raise ValueError("QA database name and run UUID disagree")
    if data["pg_host"] != "127.0.0.1":
        raise ValueError("QA PostgreSQL must be loopback")
    for key in ("pg_port", "backend_port", "frontend_port"):
        if type(data[key]) is not int or not 1024 <= data[key] <= 65535:
            raise ValueError("QA port invalid")
    if len({data[key] for key in ("pg_port", "backend_port", "frontend_port")}) != 3:
        raise ValueError("QA ports must be distinct")
    if data["backend_port"] in (3000, 3002) or data["frontend_port"] in (3000, 3002):
        raise ValueError("Existing application ports are forbidden")
    return data


def choose_port(excluded=()) -> int:
    for _ in range(30):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in {3000, 3002, 5432, *excluded}:
            return port
    raise RuntimeError("Could not allocate a dedicated local port")


def assert_port_free(port: int) -> None:
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))


def local_settings() -> dict:
    from dotenv import dotenv_values

    values = dict(dotenv_values(REPO / ".env", interpolate=False))
    return {
        "host": "127.0.0.1",
        "port": int(values.get("POSTGRES_PORT") or 5432),
        "user": values.get("POSTGRES_USER") or "aoitalk",
        "password": values.get("POSTGRES_PASSWORD") or "",
        "admin_password": os.environ.get("PGPASSWORD") or values.get("PGPASSWORD"),
    }


def child_environment(manifest: dict, private: dict) -> dict[str, str]:
    # Explicit OS/tool environment only. Never inherit provider credentials or
    # *_FILE settings and never reload the repository .env in a QA child.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
               "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "HOMEDRIVE", "HOMEPATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    root = Path(manifest["output_dir"])
    from urllib.parse import quote

    user, password = private["pg_user"], private["pg_password"]
    env.update({
        "PYTHONPATH": str(REPO), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_DOTENV_DISABLED": "1", "AOITALK_PROFILE": "personal", "AIVTUBER_ENV": "personal",
        "POSTGRES_HOST": manifest["pg_host"], "POSTGRES_PORT": str(manifest["pg_port"]),
        "POSTGRES_USER": user, "POSTGRES_PASSWORD": password, "POSTGRES_DB": check_identity(manifest["database"]),
        "DATABASE_URL": f"postgres://{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:{manifest['pg_port']}/{manifest['database']}",
        "PGOPTIONS": "-c search_path=public", "AOITALK_REQUIRE_DATABASE": "true",
        "AOITALK_ALLOW_UNAUTHENTICATED_DEV": "false",
        "FEATURE_VIRTUAL_COMPANY": "true", "FEATURE_AUTONOMOUS_AGENT_RUNTIME": "true",
        "FEATURE_MEDIA_OPERATIONS_AUTONOMY": "false", "FEATURE_CODE_AGENT": "false",
        "FEATURE_VOICE_INPUT": "false", "FEATURE_TTS_OUTPUT": "false",
        "FEATURE_DISCORD_BOT": "false", "FEATURE_CRAWLER_STATUS": "false",
        "FEATURE_ENTERTAINMENT": "false", "FEATURE_REMOTE_SERVER_VIEW": "false",
        "AOITALK_WORKSPACES_DIR": str(root / "workspaces"), "AOITALK_DATA_DIR": str(root / "data"),
        "AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY": "true", "AOITALK_FIELD_CRYPTO_KEY_B64": private["field_key"],
        "AOITALK_WEB_AUTH_SECRET": private["web_secret"], "NEXTAUTH_SECRET": private["next_secret"],
        "AUTH_SECRET": private["next_secret"], "AOITALK_JWT_SECRET": private["jwt_secret"],
        "INTERNAL_API_KEY": private["internal_key"],
        "PYTHON_API_URL": f"http://127.0.0.1:{manifest['backend_port']}",
        "NEXT_PUBLIC_AOITALK_WS_PORT": str(manifest["backend_port"]),
        "NEXTJS_URL": f"http://127.0.0.1:{manifest['frontend_port']}",
        "AOITALK_CORS_ORIGINS": f"http://127.0.0.1:{manifest['frontend_port']}",
        "NEXT_DIST_DIR": ".next-employee-qa", "NEXT_TELEMETRY_DISABLED": "1",
        "AOITALK_EMPLOYEE_QA_MANIFEST": str(root / "manifest.json"),
        "HF_HOME": str(root / "model-cache"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
    })
    if manifest.get("phone_fixture"):
        env.update(FEATURE_VOICE_INPUT="true", FEATURE_TTS_OUTPUT="true")
    return env


def private_state(manifest: dict) -> dict:
    state = json.loads((Path(manifest["output_dir"]) / ".runtime-private.json").read_text(encoding="utf-8"))
    if not manifest["dedicated_cluster"]:
        # Existing installation credentials remain solely at their original
        # source; do not copy them into a QA artifact.
        settings = local_settings()
        state.update(pg_password=settings["password"], admin_password=settings["admin_password"])
    return state


def pg_connect(manifest: dict, private: dict, *, admin=False, maintenance=False):
    import psycopg2

    return psycopg2.connect(host="127.0.0.1", port=manifest["pg_port"],
        user=private["admin_user" if admin else "pg_user"],
        password=private["admin_password" if admin else "pg_password"],
        dbname="postgres" if maintenance else check_identity(manifest["database"]), connect_timeout=5)


def run_logged(argv, *, cwd, env, log: Path, timeout=180) -> None:
    with log.open("ab") as stream:
        process = subprocess.Popen([str(x) for x in argv], cwd=cwd, env=env, stdout=stream,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=HIDDEN)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import psutil
            with contextlib.suppress(psutil.NoSuchProcess):
                parent = psutil.Process(process.pid)
                owned = parent.children(recursive=True)
                for child in reversed(owned):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.terminate()
                parent.terminate()
                psutil.wait_procs([*owned, parent], timeout=5)
            raise RuntimeError(f"QA command timed out; owned helpers stopped; see {log.name}") from None
    if code:
        raise RuntimeError(f"QA command failed ({code}); see {log.name}")


def prepare(args) -> Path:
    read_qa_credentials()  # Fail before allocating resources; never print values.
    node = args.node or shutil.which("node")
    if not node or not Path(node).is_file():
        raise RuntimeError("Provide --node with the installed node executable")
    pg_bin = Path(args.postgres_bin)
    run_id = uuid4()
    name = check_identity(PREFIX + run_id.hex)
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    # Next's Windows webpack loaders use relative paths to node_modules. A
    # cross-drive junction produces invalid './D:/...' module requests. Keep
    # this owned output beside the repo when TEMP lives on another drive.
    if os.name == "nt" and temporary_parent.drive.lower() != REPO.drive.lower():
        temporary_parent = REPO.parent
    root = temporary_parent / name
    root.mkdir(mode=0o700)
    settings = local_settings()
    dedicated_cluster = not settings["admin_password"]
    pg_port = choose_port() if dedicated_cluster else settings["port"]
    backend_port = choose_port([pg_port])
    frontend_port = choose_port([pg_port, backend_port])
    manifest = {"harness": "ai_employee_qa", "run_id": str(run_id), "database": name, "schema": "public",
        "repo": str(REPO), "output_dir": str(root), "pg_host": "127.0.0.1", "pg_port": pg_port,
        "backend_port": backend_port, "frontend_port": frontend_port,
        "backend_url": f"http://127.0.0.1:{backend_port}", "frontend_url": f"http://127.0.0.1:{frontend_port}",
        "node": str(Path(node).resolve()), "postgres_bin": str(pg_bin.resolve()),
        "dedicated_cluster": dedicated_cluster, "status": "allocated", "processes": {},
        "provider_mode": args.provider_mode,
        "provider_evidence": "deterministic_test_only; live suppliers and telephony UNVERIFIED"}
    private = {key: secrets.token_urlsafe(40) for key in
        ("web_secret", "next_secret", "jwt_secret", "internal_key", "fixture_api_key")}
    private["field_key"] = base64.b64encode(secrets.token_bytes(32)).decode()
    private.update({"pg_user": "aoitalk_qa" if dedicated_cluster else settings["user"],
        "pg_password": secrets.token_urlsafe(32) if dedicated_cluster else settings["password"],
        "admin_user": "qa_cluster_admin" if dedicated_cluster else "postgres",
        "admin_password": secrets.token_urlsafe(32) if dedicated_cluster else settings["admin_password"]})
    write_json(root / "manifest.json", manifest)
    write_json(root / ".runtime-private.json", private if dedicated_cluster else
        {key: value for key, value in private.items() if key not in ("pg_password", "admin_password")})
    env = child_environment(manifest, private)
    if dedicated_cluster:
        pwfile = root / ".initdb-password"
        pwfile.write_text(private["admin_password"], encoding="utf-8")
        pwfile.chmod(0o600)
        try:
            run_logged([pg_bin / "initdb.exe", "-D", root / "postgres", "-U", private["admin_user"],
                "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C", f"--pwfile={pwfile}"],
                cwd=root, env=env, log=root / "postgres-init.log")
        finally:
            pwfile.unlink(missing_ok=True)
        run_logged([pg_bin / "pg_ctl.exe", "-D", root / "postgres", "-l", root / "postgres.log",
            "-o", f"-h 127.0.0.1 -p {pg_port} -c max_connections=60", "-w", "start"],
            cwd=root, env=env, log=root / "postgres-init.log")
        manifest["postgres_pid"] = int((root / "postgres" / "postmaster.pid").read_text().splitlines()[0])
        write_json(root / "manifest.json", manifest)
    from psycopg2 import sql

    with contextlib.closing(pg_connect(manifest, private, admin=True, maintenance=True)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            if dedicated_cluster:
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Identifier(private["pg_user"])), (private["pg_password"],))
            cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name), sql.Identifier(private["pg_user"])))
            cursor.execute(sql.SQL("COMMENT ON DATABASE {} IS %s").format(sql.Identifier(name)), ("ai_employee_qa:" + str(run_id),))
    manifest["status"] = "database_created"
    manifest["migration_source_sha256"] = migration_hashes()
    manifest["source_before_prepare"] = source_provenance()
    write_json(root / "manifest.json", manifest)
    run_logged([sys.executable, SCRIPT, "_seed", "--manifest", root / "manifest.json"], cwd=root,
        env=env, log=root / "migration-seed.log", timeout=300)
    manifest = load_manifest(root / "manifest.json")
    if manifest["migration_source_sha256"] != migration_hashes():
        raise RuntimeError("Migration sources changed during preparation; rebuild after the owner finishes")
    snapshot_frontend(manifest)
    manifest["source_after_prepare"] = source_provenance()
    manifest["status"] = "prepared"
    write_json(root / "manifest.json", manifest)
    return root / "manifest.json"


def snapshot_frontend(manifest: dict) -> None:
    root = Path(manifest["output_dir"])
    source = REPO / "frontend"
    destination = root / "frontend"
    if destination.exists():
        raise RuntimeError("Refusing to overwrite an existing frontend snapshot")
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        "node_modules", ".next*", ".env*", "playwright-report*", "test-results*", "*.tsbuildinfo", ".git"))
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(source / "node_modules"), str(destination / "node_modules"))
    else:
        (destination / "node_modules").symlink_to(source / "node_modules", target_is_directory=True)


def refresh_frontend(manifest: dict) -> None:
    if manifest["processes"]:
        raise RuntimeError("Stop owned QA helpers before refreshing the frontend source snapshot")
    root = Path(manifest["output_dir"])
    shutil.copytree(REPO / "frontend", root / "frontend", dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("node_modules", ".next*", ".env*", "playwright-report*", "test-results*", "*.tsbuildinfo", ".git"))
    manifest["source_after_frontend_refresh"] = source_provenance()
    write_json(root / "manifest.json", manifest)


async def upgrade_database(manifest: dict) -> None:
    from sqlalchemy import text
    manager = validate_runtime_db(manifest)
    before = migration_hashes()
    if not await manager.initialize(max_retries=1):
        raise RuntimeError("Disposable database additive migration failed")
    if before != migration_hashes():
        raise RuntimeError("Migration changed during upgrade; wait for its owner")
    with manager.sync_engine.connect() as connection:
        heads = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    manifest.setdefault("migration_history", []).append({"previous_heads": manifest.get("migration_heads"),
        "heads": heads, "source_sha256": before})
    manifest["migration_heads"] = heads
    manifest["migration_source_sha256"] = before
    manifest["source_after_upgrade"] = source_provenance()
    write_json(Path(manifest["output_dir"]) / "manifest.json", manifest)
    await manager.engine.dispose()
    manager.sync_engine.dispose()


def upgrade(manifest: dict) -> None:
    if manifest["processes"]:
        raise RuntimeError("Stop owned QA helpers before upgrading the disposable database")
    root = Path(manifest["output_dir"])
    run_logged([sys.executable, SCRIPT, "_upgrade", "--manifest", root / "manifest.json"], cwd=root,
        env=child_environment(manifest, private_state(manifest)), log=root / "migration-upgrade.log", timeout=300)


def concurrency(manifest: dict) -> None:
    if manifest["processes"]:
        raise RuntimeError("Stop owned app helpers before running independent coordinator probes")
    root = Path(manifest["output_dir"])
    run_logged([sys.executable, SCRIPT, "_concurrency", "--manifest", root / "manifest.json"], cwd=root,
        env=child_environment(manifest, private_state(manifest)), log=root / "postgres-concurrency.log", timeout=300)
    manifest["concurrency_evidence"] = str(root / "postgres-concurrency-evidence.json")
    write_json(root / "manifest.json", manifest)


def migration_test(manifest: dict) -> None:
    root = Path(manifest["output_dir"])
    env = child_environment(manifest, private_state(manifest))
    env["AI_EMPLOYEE_TEST_POSTGRES_URL"] = env["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    run_logged([sys.executable, "-m", "pytest", str(REPO / "tests/test_ai_employee_models.py"), "-k", "bounded_sweep",
        "-q", "--basetemp", root / ("pytest-sweep-" + uuid4().hex), "--junitxml", root / "migration-sweep.xml"],
        cwd=root, env=env, log=root / "migration-sweep.log", timeout=180)


def validate_runtime_db(manifest: dict):
    from src.memory.config import MemoryConfig
    from src.memory.database import get_database_manager

    config = MemoryConfig()
    if (config.postgres_db != check_identity(manifest["database"])
            or config.postgres_host != "127.0.0.1" or config.postgres_port != manifest["pg_port"]
            or config.postgres_schema is not None):
        raise RuntimeError("MemoryConfig escaped the disposable database boundary")
    manager = get_database_manager(config=config)
    manager.sync_engine.hide_parameters = True
    manager.engine.sync_engine.hide_parameters = True
    from sqlalchemy import text

    with manager.sync_engine.connect() as connection:
        actual = connection.execute(text("SELECT current_database(), current_schema()")).one()
        ownership = connection.execute(text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=current_database()")).scalar_one()
    if actual != (manifest["database"], "public"):
        raise RuntimeError("Actual PostgreSQL connection escaped the QA database")
    if ownership != "ai_employee_qa:" + manifest["run_id"]:
        raise RuntimeError("Refusing database without the exact QA ownership marker")
    return manager


async def seed(manifest: dict) -> None:
    from src.memory.models import User, Space, Project, ProjectMember
    from src.memory.user_repository import UserRepository
    from src.services.agent_identity_service import OrganizationService

    manager = validate_runtime_db(manifest)
    if not await manager.initialize(max_retries=1):
        raise RuntimeError("Disposable database migration failed")
    username, password = read_qa_credentials()
    root = Path(manifest["output_dir"])
    user_id, space_id, project_id = uuid4(), uuid4(), uuid4()
    marker = {"schema_version": 1, "disposable": True, "run_id": manifest["run_id"],
        "source": "ai_employee_qa", "harness": "ai_employee_qa"}
    async with manager.SessionLocal() as session:
        session.add(User(id=user_id, username=username, password_hash=UserRepository.hash_password(password),
            role="admin", auth_source="local", is_active=True, is_password_reset_required=False,
            display_name="AI Employee QA administrator (test)", session_version=1,
            user_settings={"verification_provenance": marker}))
        await session.flush()
        session.add(Space(id=space_id, owner_id=user_id, name="AI Employee QA Space (test)", slug="employee-qa"))
        await session.flush()
        session.add(Project(id=project_id, owner_id=user_id, space_id=space_id,
            name="AI Employee QA Project (test)", slug="employee-qa", project_metadata={"verification_provenance": marker}))
        await session.flush()
        session.add(ProjectMember(project_id=project_id, user_id=user_id, role="owner"))
        await session.commit()
    organization = await OrganizationService(manager).bootstrap({"display_name": "AI Employee QA (test)",
        "autonomy_level": "bounded", "policy": {"allow_agent_runtime": True, "allow_external_actions": True,
        "require_human_approval": False, "max_concurrent_runs": 2}})
    from src.memory.conversation_repository import ConversationRepository
    conversation_repository = ConversationRepository()
    conversation = await conversation_repository.create_session(user_id=str(user_id),
        character_name="project_manager", title="AI Employee QA reports (test)", project_id=str(project_id))
    await conversation_repository.ensure_participant(str(conversation.id), "user", str(user_id),
        display_name="AI Employee QA administrator (test)", role="owner")
    from sqlalchemy import text
    with manager.sync_engine.connect() as connection:
        head = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    manifest["migration_heads"] = head
    manifest["fixtures"] = {"user_id": str(user_id), "space_id": str(space_id), "project_id": str(project_id),
        "organization_id": organization["id"], "conversation_id": str(conversation.id)}
    write_json(root / "manifest.json", manifest)
    await manager.engine.dispose()
    manager.sync_engine.dispose()


def restrict_backend_network(manifest: dict) -> None:
    ports = {manifest["pg_port"], manifest["frontend_port"], manifest["backend_port"]}

    def audit(event, args):
        if event != "socket.connect":
            return
        address = args[1]
        if not isinstance(address, tuple):
            raise PermissionError("QA outbound network denied")
        host, port = address[:2]
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        # Windows asyncio creates its private wakeup socket using the standard
        # library socketpair fallback (a self-connected ephemeral listener).
        # Permit only that exact stdlib frame, not general loopback egress.
        frame = sys._getframe(1)
        socketpair_code = getattr(socket.socketpair, "__code__", None)
        while local and frame is not None:
            if socketpair_code is not None and frame.f_code is socketpair_code:
                return
            frame = frame.f_back
        if not local or port not in ports:
            raise PermissionError("QA outbound network denied")
    sys.addaudithook(audit)


async def backend(manifest: dict) -> None:
    restrict_backend_network(manifest)
    manager = validate_runtime_db(manifest)
    if not await manager.initialize(max_retries=1):
        raise RuntimeError("QA backend migration failed")
    from src.config import Config
    from src.api.server import create_web_interface
    from scripts.verification.ai_employee_qa_support import fixture_dependencies, install_fixture_connection
    import src.services.ai_employee_platform as platform
    from functools import partial
    from unittest.mock import patch
    import uvicorn

    config = Config(config_path=str(Path(manifest["output_dir"]) / "absent-seed.yaml"))
    # Process-local runtime controls; production-safe defaults and saved config
    # are not modified. No ordinary chat LLM runtime is installed by this harness.
    config.config.setdefault("heartbeat", {})["enabled"] = False
    config.config.setdefault("agent_work", {})["poll_interval_seconds"] = 1
    # The employee journey uses a semantic fixture, not the unrelated Docs
    # embedding provider. Keep its index warmup local and inert in this process.
    from src.rag.config import get_rag_config
    get_rag_config().docs_enabled = False
    validate_runtime_db(manifest)
    registry, vault, invoker = fixture_dependencies(manifest)
    await install_fixture_connection(manager, manifest, vault)
    phone = None
    if manifest.get("phone_fixture"):
        from scripts.verification.ai_employee_qa_phone import install_phone_fixture
        from src.services.integration_action_registry import IntegrationActionRegistry
        from src.services.telephony_service import TelephonyTransferAdapter
        phone = await install_phone_fixture(manager, config, manifest, vault)
        registered = IntegrationActionRegistry(telephony_adapter_factory=lambda: TelephonyTransferAdapter(phone))
        registry._definitions["telephony.transfer_call"] = registered.require("telephony.transfer_call")
    with patch.object(platform, "build_ai_employee_services", partial(platform.build_ai_employee_services,
            registry=registry, credential_vault=vault, invoker=invoker, telephony=phone)):
        server = create_web_interface(config, character_name="project_manager")
    if not server.auth_enabled or not hasattr(server, "ai_employee_services"):
        raise RuntimeError("Real authenticated employee route composition is unavailable")
    if server.ai_employee_services.automation.invoker is not invoker:
        raise RuntimeError("Employee composition lost the injected condition provider")
    await uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=manifest["backend_port"],
        log_level="warning", access_log=False)).serve()


def spawn_owned(manifest: dict, name: str, argv, cwd: Path) -> None:
    import psutil

    root = Path(manifest["output_dir"])
    with (root / f"{name}.log").open("ab") as log:
        process = subprocess.Popen([str(x) for x in argv], cwd=cwd,
            env=child_environment(manifest, private_state(manifest)), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=HIDDEN)
    manifest["processes"][name] = {"pid": process.pid, "created_at": psutil.Process(process.pid).create_time()}
    write_json(root / "manifest.json", manifest)


def wait_http(url: str, *, timeout=100, process_info=None) -> None:
    import httpx
    import psutil
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_info and not psutil.pid_exists(process_info["pid"]):
            raise RuntimeError("QA helper exited before HTTP readiness; inspect task-owned log")
        try:
            response = httpx.get(url, timeout=3, trust_env=False, follow_redirects=False)
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("QA HTTP readiness timed out; inspect task-owned logs")


def start(manifest: dict) -> None:
    if (manifest.get("migration_source_sha256") is not None
            and manifest["migration_source_sha256"] != migration_hashes()):
        raise RuntimeError("Migration sources changed; prepare a fresh disposable database")
    if manifest["processes"]:
        raise RuntimeError("Stop the owned QA helpers before starting them again")
    assert_port_free(manifest["backend_port"])
    assert_port_free(manifest["frontend_port"])
    root = Path(manifest["output_dir"])
    manifest["source_at_start"] = source_provenance()
    manifest["status"] = "starting"
    spawn_owned(manifest, "backend", [sys.executable, SCRIPT, "_backend", "--manifest", root / "manifest.json"], root)
    wait_http(manifest["backend_url"] + "/api/health", process_info=manifest["processes"]["backend"])
    # Webpack resolves junctioned dependencies on Windows reliably; no shared
    # frontend artifact or tsconfig is touched by this isolated dev server.
    manifest = load_manifest(root / "manifest.json")
    spawn_owned(manifest, "frontend", [manifest["node"], root / "frontend/node_modules/next/dist/bin/next",
        "dev", "--webpack", "-H", "127.0.0.1", "-p", str(manifest["frontend_port"])], root / "frontend")
    wait_http(manifest["frontend_url"] + "/login", timeout=150, process_info=manifest["processes"]["frontend"])
    check(manifest)


def check(manifest: dict) -> None:
    import httpx
    root = Path(manifest["output_dir"])
    evidence = {"provider": "deterministic fixture only", "live_provider": "UNVERIFIED", "checks": []}
    username, password = read_qa_credentials()
    with httpx.Client(base_url=manifest["frontend_url"], timeout=60, trust_env=False) as client:
        response = client.get("/api/python-proxy/agents")
        if response.status_code not in (401, 403):
            raise RuntimeError(f"Unauthenticated employee API did not deny: {response.status_code}")
        evidence["checks"].append("anonymous_employee_api_denied")
        bad = client.post("/api/auth/login", headers={"Origin": manifest["frontend_url"]},
            json={"username": username, "password": "incorrect-" + secrets.token_urlsafe(20), "credential_source": "local"})
        if bad.status_code != 401 or client.cookies.get("aoitalk_session"):
            raise RuntimeError("Incorrect password did not fail closed")
        evidence["checks"].append("wrong_password_denied_without_session")
        response = client.post("/api/auth/login", headers={"Origin": manifest["frontend_url"]},
            json={"username": username, "password": password, "credential_source": "local"})
        if response.status_code != 200 or not client.cookies.get("aoitalk_session"):
            raise RuntimeError(f"Real Next -> FastAPI password login failed: {response.status_code}")
        if str(response.json().get("user", {}).get("id")) != manifest["fixtures"]["user_id"]:
            raise RuntimeError("Password login resolved a user outside the QA fixture")
        evidence["checks"].append("real_password_login_and_next_session_cookie")
        for route in ("/api/auth/status", "/api/python-proxy/agents", "/api/python-proxy/runtime/features"):
            response = client.get(route)
            if response.status_code != 200:
                raise RuntimeError(f"Authenticated QA route failed: {route} ({response.status_code})")
            if password in response.text:
                raise RuntimeError("QA password leaked in response")
            if route == "/api/python-proxy/runtime/features":
                flags = response.json()["application_features"]
                if flags.get("virtual_company") is not True or flags.get("autonomous_agent_runtime") is not True:
                    raise RuntimeError("QA company flags unavailable")
            evidence["checks"].append("authenticated:" + route)
        # Compile the actual authenticated Next entry points before the
        # browser begins timed interactions with a freshly copied dev tree.
        for route in ("/chat", "/operations?tab=agents"):
            response = client.get(route)
            if response.status_code != 200:
                raise RuntimeError(f"Authenticated page readiness failed: {route} ({response.status_code})")
            evidence["checks"].append("authenticated-page:" + route)
    manifest = load_manifest(root / "manifest.json")
    with contextlib.closing(pg_connect(manifest, private_state(manifest))) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_schema()")
            if cursor.fetchone() != (manifest["database"], "public"):
                raise RuntimeError("QA storage identity mismatch")
    evidence["checks"].append("disposable_database_identity_verified")
    evidence["source_after_auth"] = source_provenance()
    write_json(root / "auth-evidence.json", evidence)
    manifest["status"] = "authenticated_ready"
    write_json(root / "manifest.json", manifest)


def browser(manifest: dict) -> None:
    # Dedicated config omits repository globalSetup (unrelated fixture writes)
    # and webServer (must never launch against a shared app).
    root = Path(manifest["output_dir"])
    (root / "screenshots").mkdir(exist_ok=True)
    spec = REPO / "frontend/e2e/ai-employees.live.spec.ts"
    if not spec.is_file():
        raise RuntimeError("Frontend-owned live spec has not landed")
    shutil.copy2(spec, root / "frontend/e2e" / spec.name)
    manifest["browser_invocation"] = {"spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
        "source": source_provenance(), "expected_status": "uncertain" if manifest.get("provider_mode") == "timeout" else "succeeded"}
    write_json(root / "manifest.json", manifest)
    config = root / "frontend/ai-employee-qa.playwright.config.cjs"
    config.write_text("module.exports = {testDir:'./e2e',testMatch:'ai-employees.live.spec.ts',workers:1,retries:0,expect:{timeout:15000},"
        "reporter:[['line']],timeout:120000,use:{browserName:'chromium',trace:'off',screenshot:'off',video:'off'},"
        "outputDir:'./employee-qa-results'};\n", encoding="utf-8")
    env = child_environment(manifest, private_state(manifest))
    username, password = read_qa_credentials()
    env.update({"AI_EMPLOYEE_QA_BASE_URL": manifest["frontend_url"], "AI_EMPLOYEE_QA_EMAIL": username,
        "AI_EMPLOYEE_QA_BACKEND_URL": manifest["backend_url"],
        "AI_EMPLOYEE_QA_PASSWORD": password, "AI_EMPLOYEE_QA_PROJECT_ID": manifest["fixtures"]["project_id"],
        "AI_EMPLOYEE_QA_SPACE_ID": manifest["fixtures"]["space_id"],
        "AI_EMPLOYEE_QA_CONNECTION_ID": manifest["fixtures"].get("connection_id", ""),
        "AI_EMPLOYEE_QA_RESULT_PATH": str(root / "browser-journey.json"),
        "AI_EMPLOYEE_QA_CONVERSATION_ID": manifest["fixtures"]["conversation_id"],
        "AI_EMPLOYEE_QA_PROVIDER_EVIDENCE_PATH": str(root / "provider-evidence.json"),
        "AI_EMPLOYEE_QA_SCREENSHOT_DIR": str(root / "screenshots"),
        "AI_EMPLOYEE_QA_EXPECTED_ACTION_STATUS": "uncertain" if manifest.get("provider_mode") == "timeout" else "succeeded"})
    run_logged([manifest["node"], root / "frontend/node_modules/@playwright/test/cli.js", "test", "--config", config],
        cwd=root / "frontend", env=env, log=root / "browser.log", timeout=360)
    verify_journey(manifest)
    manifest["status"] = "journey_verified"
    manifest["journey_outcome"] = manifest["browser_invocation"]["expected_status"]
    write_json(root / "manifest.json", manifest)


def verify_journey(manifest: dict) -> None:
    """Read canonical rows independently of browser assertions/provider text."""
    from psycopg2.extras import RealDictCursor
    root = Path(manifest["output_dir"])
    result = json.loads((root / "browser-journey.json").read_text(encoding="utf-8"))
    provider = json.loads((root / "provider-evidence.json").read_text(encoding="utf-8"))
    agent_id, action_id = str(UUID(result["agent_id"])), str(UUID(result["action_id"]))
    expected = "uncertain" if manifest.get("provider_mode") == "timeout" else "succeeded"
    if provider["submission_count"] != 1 or provider.get("duplicate_attempts", 0) != 0 or provider["live_provider_verified"] is not False:
        raise RuntimeError("Independent test provider did not observe exactly one submission")
    with contextlib.closing(pg_connect(manifest, private_state(manifest))) as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT id::text,status,authorization_mode,origin_agent_id::text,origin_agent_run_id::text,origin_work_item_id::text FROM external_actions WHERE origin_agent_id=%s AND connection_id=%s",
                (agent_id, manifest["fixtures"]["connection_id"]))
            actions = cursor.fetchall()
            if len(actions) != 1 or actions[0]["id"] != action_id or actions[0]["status"] != expected or actions[0]["authorization_mode"] != "bounded_policy":
                raise RuntimeError("Canonical action count/status/authorization disagrees with the journey")
            action = actions[0]
            cursor.execute("SELECT id::text,status FROM external_action_attempts WHERE action_id=%s", (action_id,))
            attempts = cursor.fetchall()
            if len(attempts) != 1 or attempts[0]["status"] != expected:
                raise RuntimeError("Exactly one settled canonical provider Attempt was required")
            cursor.execute("SELECT id::text,attempt_id::text FROM external_action_receipts WHERE action_id=%s", (action_id,))
            receipts = cursor.fetchall()
            if len(receipts) != (1 if expected == "succeeded" else 0):
                raise RuntimeError("Canonical receipt evidence disagrees with the provider outcome")
            if receipts and receipts[0]["attempt_id"] != attempts[0]["id"]:
                raise RuntimeError("Receipt is not bound to the observed Attempt")
            cursor.execute("SELECT count(*) AS count FROM external_action_approvals WHERE action_id=%s", (action_id,))
            if cursor.fetchone()["count"] != 0:
                raise RuntimeError("Bounded policy execution fabricated a human approval")
            cursor.execute("SELECT id::text,source_id FROM agent_automation_events WHERE conversation_session_id=%s AND actor_kind='human' ORDER BY occurred_at,id",
                (manifest["fixtures"]["conversation_id"],))
            events = cursor.fetchall()
            if len(events) < 2 or len({row["source_id"] for row in events}) != len(events):
                raise RuntimeError("Distinct persisted human message events were not observed")
            cursor.execute("SELECT id::text,state,result_summary_json,active_agent_run_id::text FROM agent_work_items WHERE assigned_agent_id=%s AND source_type='automation_event' AND source_id=ANY(%s)",
                (agent_id, [row["id"] for row in events]))
            work = cursor.fetchall()
            if len(work) < 2 or any(row["state"] != "succeeded" for row in work):
                raise RuntimeError("Both canonical automation evaluations must complete")
            if not any(row["result_summary_json"].get("suppressed") is True for row in work):
                raise RuntimeError("Canonical second-report dedupe suppression was not recorded")
            cursor.execute("SELECT id::text,agent_id::text,work_item_id::text FROM agent_runs WHERE id=%s", (action["origin_agent_run_id"],))
            run = cursor.fetchone()
            if not run or run["agent_id"] != agent_id or run["work_item_id"] != action["origin_work_item_id"]:
                raise RuntimeError("Action does not preserve its typed AgentRun/WorkItem origin")
            cursor.execute("SELECT version_num FROM alembic_version")
            heads = [row["version_num"] for row in cursor.fetchall()]
    # No message plaintext, lease token, credentials, or provider payloads.
    evidence = {"outcome": expected, "database": manifest["database"], "migration_heads": heads,
        "action": dict(action), "attempts": [dict(row) for row in attempts], "receipts": [dict(row) for row in receipts],
        "trigger_event_ids": [row["id"] for row in events], "work_item_ids": [row["id"] for row in work],
        "provider_submission_count": 1, "human_approval_count": 0, "duplicate_suppressed": True,
        "chat_transport": "real authenticated canonical HTTP messages from browser test",
        "semantic_provider": "deterministic fixture; real model accuracy UNVERIFIED",
        "live_provider": "UNVERIFIED", "source_after_journey": source_provenance()}
    write_json(root / "journey-evidence.json", evidence)


def recovery_check(manifest: dict) -> None:
    root = Path(manifest["output_dir"])
    verify_journey(manifest)
    stop(manifest)
    start(load_manifest(root / "manifest.json"))
    # More than two normal poll intervals; then independently re-read both
    # canonical Attempt/Receipt rows and the restart-persistent provider count.
    time.sleep(4)
    verify_journey(load_manifest(root / "manifest.json"))
    write_json(root / "recovery-evidence.json", {"restart_verified": True, "provider_submission_count": 1,
        "duplicate_attempts": 0, "outcome": "uncertain" if manifest.get("provider_mode") == "timeout" else "succeeded",
        "source": source_provenance()})


def ui_smoke(manifest: dict, *, history=False) -> None:
    root = Path(manifest["output_dir"])
    for relative in ("src/components/operations/agents/employee-fields.tsx", "src/components/operations/operations-command-center.tsx"):
        source, target = REPO / "frontend" / relative, root / "frontend" / relative
        if source.read_bytes() != target.read_bytes():
            shutil.copy2(source, target)
    manifest["source_before_ui_smoke"] = source_provenance()
    write_json(root / "manifest.json", manifest)
    env = child_environment(manifest, private_state(manifest))
    username, password = read_qa_credentials()
    env.update(AI_EMPLOYEE_QA_EMAIL=username, AI_EMPLOYEE_QA_PASSWORD=password)
    if history:
        env["AI_EMPLOYEE_QA_SMOKE_MODE"] = "history"
    run_logged([manifest["node"], SCRIPT.with_name("ai_employee_qa_smoke.cjs"), root], cwd=root,
        env=env, log=root / "ui-smoke.log", timeout=180)
    verify_journey(manifest)


def phone_smoke(manifest: dict) -> None:
    root = Path(manifest["output_dir"])
    stop(manifest)
    manifest = load_manifest(root / "manifest.json")
    manifest["phone_fixture"] = True
    write_json(root / "manifest.json", manifest)
    start(manifest)
    manifest = load_manifest(root / "manifest.json")
    env = child_environment(manifest, private_state(manifest))
    username, password = read_qa_credentials()
    env.update(AI_EMPLOYEE_QA_EMAIL=username, AI_EMPLOYEE_QA_PASSWORD=password, AI_EMPLOYEE_QA_SMOKE_MODE="phone")
    run_logged([manifest["node"], SCRIPT.with_name("ai_employee_qa_smoke.cjs"), root], cwd=root,
        env=env, log=root / "phone-ui.log", timeout=180)
    verify_journey(manifest)


def stop(manifest: dict, *, drop_database=False) -> None:
    import psutil
    root = Path(manifest["output_dir"])
    manifest.setdefault("process_history", []).append({"event": "stop", "processes": dict(manifest["processes"]),
        "at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
    for info in reversed(list(manifest["processes"].values())):
        try:
            process = psutil.Process(info["pid"])
            if abs(process.create_time() - info["created_at"]) > 0.01:
                raise RuntimeError("Refusing to stop a reused process ID")
            cmdline = process.cmdline()
            if str(root) not in " ".join(cmdline):
                raise RuntimeError("Refusing to stop a process outside the QA launch")
            descendants = process.children(recursive=True)
            for child in reversed(descendants):
                child.terminate()
            process.terminate()
            _, alive = psutil.wait_procs([*descendants, process], timeout=10)
            if alive:
                raise RuntimeError("Owned QA helper did not terminate")
        except psutil.NoSuchProcess:
            pass
    manifest["processes"] = {}
    manifest["status"] = "stopped"
    write_json(root / "manifest.json", manifest)
    if drop_database:
        from psycopg2 import sql
        private = private_state(manifest)
        with contextlib.closing(pg_connect(manifest, private, admin=True, maintenance=True)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=%s", (manifest["database"],))
                row = cursor.fetchone()
                if row is None or row[0] != "ai_employee_qa:" + manifest["run_id"]:
                    raise RuntimeError("Refusing to drop database without exact QA ownership marker")
                cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(check_identity(manifest["database"]))))
        if manifest["dedicated_cluster"]:
            run_logged([Path(manifest["postgres_bin"]) / "pg_ctl.exe", "-D", root / "postgres", "-m", "fast", "-w", "stop"],
                cwd=root, env=child_environment(manifest, private), log=root / "postgres-init.log")
        manifest["status"] = "database_dropped"
        write_json(root / "manifest.json", manifest)


def cleanup(manifest: dict) -> None:
    root = Path(manifest["output_dir"]).resolve()
    already_dropped = manifest["status"] == "database_dropped"
    if not already_dropped:
        stop(manifest, drop_database=True)
    manifest = load_manifest(root / "manifest.json")
    if manifest["processes"] or (manifest["dedicated_cluster"] and (root / "postgres/postmaster.pid").exists()):
        raise RuntimeError("Private files cannot be removed while owned helpers/PostgreSQL remain active")
    private_files = [root / ".runtime-private.json", root / ".initdb-password",
        *root.glob("frontend.env*"), *(root / "frontend").glob(".env*")]
    removed = []
    for path in private_files:
        if not path.exists():
            continue
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeError("Refusing private-file cleanup outside the owned artifact directory")
        path.unlink()
        removed.append(str(path.relative_to(root)))
    manifest["cleanup"] = {"database": manifest["database"], "database_removed": True,
        "already_dropped": already_dropped, "private_files_removed": removed,
        "safe_artifacts_retained": True,
        "completed_at_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    write_json(root / "manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "upgrade", "refresh-frontend", "check", "browser", "verify-journey",
        "concurrency", "migration-test", "recovery-check", "ui-smoke", "history-smoke", "phone-smoke", "cleanup", "stop", "_seed", "_backend", "_upgrade", "_concurrency"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--postgres-bin", default=r"C:\Program Files\PostgreSQL\16\bin")
    parser.add_argument("--drop-database", action="store_true")
    parser.add_argument("--provider-mode", choices=("succeeded", "timeout", "weak", "failed", "transient"), default="succeeded")
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args))
        return 0
    if not args.manifest:
        parser.error("--manifest required")
    manifest = load_manifest(args.manifest)
    if args.command.startswith("_"):
        if os.getenv("PYTHON_DOTENV_DISABLED") != "1" or os.getenv("POSTGRES_DB") != manifest["database"]:
            raise RuntimeError("Internal QA child must be launched by this harness")
        if args.command == "_concurrency":
            from scripts.verification.ai_employee_qa_concurrency import run
            asyncio.run(run(manifest))
        else:
            asyncio.run({"_seed": seed, "_backend": backend, "_upgrade": upgrade_database}[args.command](manifest))
    elif args.command == "stop":
        stop(manifest, drop_database=args.drop_database)
    else:
        {"start": start, "upgrade": upgrade, "refresh-frontend": refresh_frontend, "check": check,
         "browser": browser, "verify-journey": verify_journey, "concurrency": concurrency,
         "migration-test": migration_test, "recovery-check": recovery_check, "ui-smoke": ui_smoke,
         "history-smoke": lambda value: ui_smoke(value, history=True), "phone-smoke": phone_smoke, "cleanup": cleanup}[args.command](manifest)
    print(json.dumps({"manifest": str(args.manifest), "command": args.command, "result": "complete"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
