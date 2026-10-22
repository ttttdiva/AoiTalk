"""Optional host production integration. GET detects only; mutations are explicit.

Browser input never supplies commands, git refs, Python paths, environment,
project roots or agent prompts. Host production settings are admin-owned.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from pathlib import Path

from pydantic import Field, StrictBool
from sqlalchemy import text

from ..app_config_store import load_app_config_snapshot_sync, update_app_config_keys_sync
from ..features import Features
from ..memory.database import get_db_session
from ..services.storage_roots import assert_no_links
from ..services.storage_io import StorageError
from .spec import Record

REPO_ROOT = Path(__file__).resolve().parents[2]
TRUSTED_REPOSITORY = "https://github.com/ttttdiva/87_see-though_rig.git"
PROTOCOL_VERSION = 1
COMMANDS = frozenset({"doctor", "prepare", "project-create", "project-status", "project-run",
                      "project-resume", "project-cancel", "project-export"})
COMMANDS = frozenset(COMMANDS) | {"project-editor-info", "project-editor-save"}
TERMINAL = frozenset({"ready", "failed", "cancelled", "needs_input"})
MAX_OUTPUT = 4 * 1024 * 1024


class ProductionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ProductionSettingsView(Record):
    enabled: bool = False
    path: str | None = None
    effective_path: str | None = None
    status: str = "disabled"
    problems: list[str] = Field(default_factory=list)
    managed: bool = False
    can_manage: bool = False
    installed: bool = False
    ready: bool = False
    protocol_version: int | None = None


class ProductionSettingsRequest(Record):
    enabled: StrictBool
    path: str | None = Field(default=None, max_length=1024)


class ProductionOperationRequest(Record):
    path: str | None = Field(default=None, max_length=1024)


async def config_snapshot():
    try:
        return await asyncio.to_thread(load_app_config_snapshot_sync)
    except Exception as exc:
        raise ProductionError("製作設定を取得できません", 503) from exc


async def settings():
    config = await config_snapshot()
    value = config.get("rig_production", {})
    return value if isinstance(value, dict) else {}


async def persist(changes, expected=None):
    values = {"rig_production." + k: v for k, v in changes.items()}
    expectations = {"rig_production." + k: v for k, v in expected.items()} if expected is not None else None
    if not await asyncio.to_thread(update_app_config_keys_sync, values, expected_values=expectations):
        raise ProductionError("製作設定を保存できません（競合またはDB障害）", 409)


def effective_path(saved, requested=None):
    value = requested if requested is not None else saved.get("path")
    if value is None:
        path = REPO_ROOT.parent / "87_see-though_rig"
    else:
        if (not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value)
                or value.startswith(("//", "\\\\")) or "://" in value):
            raise ProductionError("製作環境はローカルの絶対パスを指定してください")
        path = Path(value)
        if not path.is_absolute():
            raise ProductionError("製作環境は絶対パスを指定してください")
    try:
        assert_no_links(path, allow_missing=True)
        path = path.resolve()
    except Exception as exc:
        raise ProductionError("製作環境にlink/reparseまたは不正なパスがあります") from exc
    if path == REPO_ROOT or path in REPO_ROOT.parents or path.is_relative_to(REPO_ROOT):
        raise ProductionError("AoiTalk本体を製作環境として実行できません")
    return path


def python_path(path):
    candidates = (path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
                  path / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    for python in candidates:
        try:
            assert_no_links(python, allow_missing=True)
        except Exception:
            continue
        if python.is_file():
            return python
    raise ProductionError("既存の製作環境Pythonがありません。管理者による準備が必要です", 503)


def detect(path, saved):
    installed = False
    identity_file = path / "seethrough-production.json"
    try:
        assert_no_links(identity_file, allow_missing=False)
        if identity_file.stat().st_size <= 4096:
            identity = json.loads(identity_file.read_text(encoding="utf-8"))
            installed = (isinstance(identity, dict) and identity.get("identity") == "seethrough-rig"
                         and type(identity.get("protocol_version")) is int and identity["protocol_version"] == 1
                         and identity.get("version") == "0.1.0"
                         and identity.get("module") == "seethrough_rig.integration"
                         and (path / "seethrough_rig" / "integration.py").is_file())
    except (OSError, ValueError, TypeError, StorageError):
        pass
    try:
        assert_no_links(path, allow_missing=True)
        if installed:
            assert_no_links(path / "seethrough_rig" / "integration.py", allow_missing=False)
        python_path(path)
        python_ok = True
    except Exception:
        python_ok = False
    marker = path / ".aoitalk-managed-production.json"
    managed = False
    if marker.is_file():
        try:
            assert_no_links(marker, allow_missing=False)
            if marker.stat().st_size <= 4096:
                data = json.loads(marker.read_text(encoding="utf-8"))
                managed = data.get("repository") == TRUSTED_REPOSITORY and bool(re.fullmatch("[0-9a-f]{40}", data.get("revision", "")))
        except Exception:
            pass
    return installed, python_ok, managed


async def view(can_manage=False, requested=None):
    saved = await settings()
    problems = []
    try:
        path = effective_path(saved, requested)
        installed, python_ok, managed = detect(path, saved)
    except ProductionError as exc:
        path, installed, python_ok, managed = None, False, False, False
        problems.append(str(exc))
    previous_doctor = saved.get("doctor") or {}
    if path and previous_doctor.get("path") == str(path) and isinstance(previous_doctor.get("problems"), list):
        problems.extend(str(x)[:300] for x in previous_doctor["problems"][:64])
    enabled = saved.get("enabled") is True and not Features.is_enterprise()
    if Features.is_enterprise():
        problems.append("Enterpriseでは製作実行を利用できません")
    if not installed:
        problems.append("製作CLIが見つかりません")
    elif not python_ok:
        problems.append("製作環境Pythonが見つかりません")
    doctor = saved.get("doctor", {})
    ready = bool(installed and python_ok and doctor.get("ready") is True and doctor.get("path") == str(path))
    state = "disabled" if not enabled else ("ready" if ready else "needs_preparation")
    operation = saved.get("operation") or {}
    if operation.get("status") in {"running", "interrupted", "failed"}:
        operation_status = operation["status"]
        if operation_status == "running":
            async with await get_db_session() as probe_db, probe_db.begin():
                if probe_db.bind.dialect.name == "postgresql":
                    free = (await probe_db.execute(text("SELECT pg_try_advisory_xact_lock(4102026, 932)"))).scalar()
                    if free: operation_status = "interrupted"
                elif not _local_operation_lock.locked():
                    operation_status = "interrupted"
        state = operation_status + ":" + str(operation.get("action", ""))
    return ProductionSettingsView(enabled=enabled, path=saved.get("path") if can_manage else None,
                                  effective_path=str(path) if can_manage and path else None,
                                  status=state, problems=problems if can_manage else (["製作環境が利用できません"] if problems else []),
                                  managed=managed, can_manage=can_manage and not Features.is_enterprise(),
                                  installed=installed, ready=ready, protocol_version=1 if installed else None)


def require_personal():
    if Features.is_enterprise():
        raise ProductionError("Enterpriseでは製作実行を利用できません", 403)


def child_env():
    keys = {"SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "CUDA_PATH"}
    allowed = {key.upper() for key in keys}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    for key in ("SEETHROUGH_ROOT", "SEETHROUGH_PYTHON", "SEETHROUGH_MODELS"):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return env


async def process(argv, cwd, timeout=300, cancel=None):
    """Fixed trusted commands only. Bounded separate streams and tree cleanup."""
    from ..agent_harness.runner import _terminate_local_process
    proc = await asyncio.create_subprocess_exec(*map(str, argv), cwd=str(cwd), env=child_env(),
                                               stdin=asyncio.subprocess.DEVNULL,
                                               stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    async def drain(stream):
        chunks, count = [], 0
        while chunk := await stream.read(65536):
            count += len(chunk)
            if count > MAX_OUTPUT:
                raise ProductionError("製作CLI出力が上限を超えました", 502)
            chunks.append(chunk)
        return b"".join(chunks)

    async def wait():
        out, _err = await asyncio.gather(drain(proc.stdout), drain(proc.stderr))
        code = await proc.wait()
        return code, out

    task = asyncio.create_task(wait())
    watcher = None
    if cancel is not None:
        async def watch():
            await cancel.wait()
            if not task.done():
                _terminate_local_process(proc)
        watcher = asyncio.create_task(watch())
    try:
        return await asyncio.wait_for(task, timeout)
    except BaseException:
        _terminate_local_process(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 10)
        raise
    finally:
        if watcher:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


def envelope(raw, code):
    from .package_import import json_data
    try:
        data = json_data(raw)
    except ValueError as exc:
        # Doctor results contain legitimate host paths; parse strictly separately.
        from .package_import import _pairs
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except Exception:
            raise ProductionError("製作CLIのJSON応答が不正です", 502) from exc
    if (not isinstance(data, dict) or data.get("identity") != "seethrough-rig"
            or type(data.get("protocol_version")) is not int or data["protocol_version"] != 1
            or type(data.get("ok")) is not bool or not isinstance(data.get("result"), dict)):
        raise ProductionError("未対応の製作CLI protocolです", 502)
    if code != 0 or not data["ok"]:
        error = data.get("error") or {}
        failure = ProductionError("製作CLIが失敗しました: " + str(error.get("code", "unknown"))[:120], 502)
        failure.code = str(error.get("code", "unknown"))
        raise failure
    return data["result"]


async def cli(path, command, args=(), timeout=300, cancel=None):
    require_personal()
    if command not in COMMANDS:
        raise ProductionError("未対応の製作コマンドです")
    if not detect(path, {})[0]:
        raise ProductionError("対応する製作identity/protocolが見つかりません", 503)
    assert_no_links(path / "seethrough_rig" / "integration.py", allow_missing=False)
    argv = [python_path(path), "-m", "seethrough_rig.integration", command, *args, "--json"]
    try:
        code, out = await process(argv, path, timeout, cancel)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ProductionError("製作CLIを実行できません（環境・timeoutを確認してください）", 503) from exc
    return envelope(out, code)


def release():
    path = REPO_ROOT / "resources" / "seethrough-rig" / "release.json"
    try:
        assert_no_links(path, allow_missing=False)
        if path.stat().st_size > 4096:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("repository") != TRUSTED_REPOSITORY
                or not re.fullmatch("[0-9a-f]{40}", value.get("revision", ""))
                or value.get("protocol_version") != 1
                or value.get("runtime", {}).get("abi") != 1
                or value.get("runtime", {}).get("version") not in {"0.1.0", "0.2.0", "0.3.0", "0.4.0"}):
            raise ValueError()
        return value
    except Exception as exc:
        raise ProductionError("固定された信頼済みrelease契約がありません", 503) from exc


@contextlib.asynccontextmanager
async def operation_lock():
    """Cross-server OS-independent lock, crash automatically releases."""
    async with await get_db_session() as db, db.begin():
        if db.bind.dialect.name == "postgresql":
            acquired = (await db.execute(text("SELECT pg_try_advisory_xact_lock(4102026, 932)"))).scalar()
            worker_free = (await db.execute(text("SELECT pg_try_advisory_xact_lock(4102026, 931)"))).scalar()
            if not acquired or not worker_free:
                raise ProductionError("別の製作処理・環境操作が実行中です", 409)
        async with _local_operation_lock:
            yield


_local_operation_lock = asyncio.Lock()


async def save_settings(body):
    require_personal()
    async with operation_lock():
        saved = await settings()
        effective_path(saved, body.path)
        await persist({"enabled": body.enabled, "path": body.path, "doctor": {}})
    return await view(True)


async def doctor(requested=None):
    require_personal()
    saved = await settings()
    path = effective_path(saved, requested)
    result = await cli(path, "doctor")
    # Required stages are independent: dependency readiness is not artistic QA.
    ready = result.get("ready") is True
    await persist({"doctor": {"path": str(path), "ready": ready, "protocol_version": 1, "problems": result.get("problems", [])[:64]}})
    response = await view(True, requested)
    response.problems = [str(x)[:300] for x in result.get("problems", [])] if isinstance(result.get("problems", []), list) else response.problems
    return response


async def git(path, *args):
    code, out = await process(["git", "-c", "core.hooksPath=", "-c", "protocol.file.allow=never", *args], path, 600)
    if code:
        raise ProductionError("管理対象repository操作に失敗しました", 502)
    return out.decode("utf-8", errors="strict").strip()


async def operate(action, requested=None):
    require_personal()
    if action not in {"install", "prepare", "update"}:
        raise ProductionError("未対応の製作操作です")
    async with operation_lock():
        saved = await settings()
        path = effective_path(saved, requested)
        token = uuid.uuid4().hex
        state = {"id": token, "action": action, "status": "running", "path": str(path), "stage": "checking"}
        await persist({"operation": state})
        try:
            if action in {"install", "update"}:
                trusted = release()
                marker = path / ".aoitalk-managed-production.json"
                if action == "install" and not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    state = {**state, "stage": "cloning"}
                    await persist({"operation": state})
                    # Clone exactly the fixed trusted source, no checkout or hooks.
                    await git(path.parent, "clone", "--filter=blob:none", "--no-checkout", "--", trusted["repository"], str(path))
                    marker.write_text(json.dumps(trusted), encoding="utf-8")
                prior = saved.get("operation") or {}
                if (not marker.exists() and action == "install" and prior.get("action") == "install"
                        and prior.get("path") == str(path) and prior.get("stage") == "cloning"
                        and path.is_dir() and all(p.name == ".git" for p in path.iterdir())
                        and await git(path, "remote", "get-url", "origin") == TRUSTED_REPOSITORY):
                    marker.write_text(json.dumps(trusted), encoding="utf-8")
                if not detect(path, saved)[2]:
                    raise ProductionError("管理対象として作成した製作環境だけ更新できます", 409)
                pristine_install = action == "install" and all(p.name in {".git", ".aoitalk-managed-production.json"} for p in path.iterdir())
                if not pristine_install:
                    changes = await git(path, "status", "--porcelain", "-z", "--untracked-files=all")
                    allowed_markers = {"?? .aoitalk-managed-production.json", "?? .aoitalk-python-bootstrap.json"}
                    if any(entry and entry not in allowed_markers for entry in changes.split("\0")):
                        raise ProductionError("変更された製作repositoryを更新できません", 409)
                remote = await git(path, "remote", "get-url", "origin")
                if remote != TRUSTED_REPOSITORY:
                    raise ProductionError("管理対象remoteが信頼済みURLと一致しません", 409)
                state = {**state, "stage": "fetching"}
                await persist({"operation": state})
                await git(path, "fetch", "--no-tags", "--", trusted["repository"], trusted["revision"])
                await git(path, "checkout", "--detach", trusted["revision"])
                if await git(path, "rev-parse", "HEAD") != trusted["revision"]:
                    raise ProductionError("release revisionを確認できません", 502)
                marker.write_text(json.dumps(trusted), encoding="utf-8")
            state = {**state, "stage": "preparing"}
            await persist({"operation": state, "path": str(path), "doctor": {}})
            if not detect(path, saved)[0]:
                raise ProductionError("製作identity/protocolが一致しません", 409)
            from .production_bootstrap import BootstrapError, ensure_tool_python
            try:
                await ensure_tool_python(path, process)
            except BootstrapError as exc:
                raise ProductionError(str(exc), 503) from exc
            if action in {"install", "prepare"}:
                await cli(path, "prepare", timeout=1800)
            result = await cli(path, "doctor")
            await persist({"doctor": {"path": str(path), "ready": result.get("ready") is True, "protocol_version": 1, "problems": result.get("problems", [])[:64]},
                           "operation": {**state, "status": "completed", "stage": "done"}})
        except asyncio.CancelledError:
            await persist({"operation": {**state, "status": "interrupted"}})
            raise
        except Exception as exc:
            await persist({"operation": {**state, "status": "failed", "error": str(exc)[:300]}})
            raise
    return await view(True)
