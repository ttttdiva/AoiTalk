"""Explicit, process-owned Qdrant Docker lifecycle; never install or take over.

The Docker daemon must already run locally. Storage and a verified official
image are prepared separately. A new process never adopts an orphan container;
it can remove an exact managed container only after proving its owner is dead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

import psutil


logger = logging.getLogger(__name__)
_LABEL = "io.aoitalk.qdrant."
_SOCKET = "unix:///var/run/docker.sock"
_DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
_OWNED_EMBEDDING_COLLECTION = re.compile(
    r"^aoitalk_(?:docs|knowledge|media|conversation_memory)__eg2_[0-9a-f]{16}$"
)
_CONTAINER_NAME_CONFLICT = re.compile(
    r"(?:already\s+in\s+use|name\s+.*(?:conflict|already)|conflict.*name)",
    re.IGNORECASE,
)
_LIFETIME_RECOVERY_ATTEMPTS = 2
_LIFETIME_RECOVERY_DELAY_SECONDS = 0.2
_LIFETIME_RECOVERY_IO_TIMEOUT_SECONDS = 2.0
_ORPHAN_REMOVAL_TIMEOUT_SECONDS = 5.0
_ENVIRONMENT = (
    "QDRANT__SERVICE__HOST=0.0.0.0",  # Container only; published host is 127.0.0.1.
    "QDRANT__SERVICE__HTTP_PORT=6333",
    "QDRANT__SERVICE__ENABLE_TLS=false",
    "QDRANT__SERVICE__ENABLE_CORS=false",
    "QDRANT__CLUSTER__ENABLED=false",
    "QDRANT__TELEMETRY_DISABLED=true",
    "QDRANT__STORAGE__SNAPSHOTS_PATH=/qdrant/storage/snapshots",
    # The managed Windows/WSL storage is a DrvFS bind mount.  Keep snapshot
    # archive temp files on Linux ext4 so metadata operations do not fail with
    # Qdrant's `failed to query metadata of file` error during backup/restore.
    "QDRANT__STORAGE__TEMP_PATH=/tmp/aoitalk-qdrant-snapshots-temp",
)


class QdrantLaunchError(RuntimeError):
    """No fallback endpoint or storage is safe for this launch."""


class QdrantOwnershipError(QdrantLaunchError):
    """The inspected container no longer matches this manager's ownership."""


@dataclass
class _Lifetime:
    process: subprocess.Popen
    generation: int = 0
    stopping: threading.Event = field(default_factory=threading.Event)
    status_lock: threading.Lock = field(default_factory=threading.Lock)
    failure: str | None = None


@dataclass(frozen=True)
class LaunchPlan:
    image: str
    storage_path: str
    port: int
    wsl_distribution: str | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def docker(self) -> list[str]:
        command = ["docker", "--host", _SOCKET]
        if self.wsl_distribution:
            prefix = ["wsl.exe", "--distribution", self.wsl_distribution, "--exec", "env"]
            for name in _DOCKER_ENV:
                prefix.extend(["-u", name])
            return prefix + command
        return command


def _environment_for_plan(plan: LaunchPlan) -> tuple[str, ...]:
    """Return the exact Qdrant environment owned by one launch plan."""
    return _ENVIRONMENT


def _uses_wsl_drvfs_storage(plan: LaunchPlan) -> bool:
    """Return whether a Windows WSL plan binds a DrvFS drive mount."""
    return bool(
        plan.wsl_distribution
        and re.match(r"^/mnt/[A-Za-z](?:/|$)", plan.storage_path)
    )


def _apply_drvfs_optimizer_override(plan: LaunchPlan) -> None:
    """Persistently disable Qdrant optimizers for known DrvFS collections.

    Qdrant 1.16.3 accepts the typed zero in its collection PATCH API and
    persists it.  The node-level ``optimizers_overwrite`` setting does not
    update an existing collection's UpdateHandler after restart, so this
    narrow collection-config operation is required for a DrvFS bind mount.
    Only the already-owned local-managed endpoint is reached by callers.
    """
    if not _uses_wsl_drvfs_storage(plan):
        return
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(plan.endpoint + "/collections", timeout=5) as response:
            if response.status != 200:
                raise QdrantLaunchError("Qdrant collection一覧を確認できません")
            listing = json.loads(response.read(64 * 1024))
        entries = (listing.get("result") or {}).get("collections")
        if not isinstance(entries, list):
            raise QdrantLaunchError("Qdrant collection一覧の形式が不正です")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
                raise QdrantLaunchError("Qdrant collection名の形式が不正です")
            if not _OWNED_EMBEDDING_COLLECTION.fullmatch(entry["name"]):
                continue
            request = Request(
                plan.endpoint + "/collections/" + quote(entry["name"], safe=""),
                data=json.dumps({"optimizers_config": {"max_optimization_threads": 0}}).encode(),
                method="PATCH",
                headers={"Content-Type": "application/json"},
            )
            with opener.open(request, timeout=5) as response:
                payload = json.loads(response.read(64 * 1024))
            if response.status != 200 or payload.get("status") != "ok" or payload.get("result") is not True:
                raise QdrantLaunchError("Qdrant collection optimizer設定の更新に失敗しました")
    except QdrantLaunchError:
        raise
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise QdrantLaunchError("Qdrant collection optimizer設定を確認できません") from exc


def _effective_config(config: object | None) -> dict:
    if config is None:
        return {}
    if isinstance(config, dict):
        return config
    # Normal main.py calls this after DB readiness and Config initialization.
    # A fresh strict read prevents Config's Personal fallback from hiding the
    # stored managed mode when PostgreSQL was unavailable.
    from src.app_config_store import load_app_config_snapshot_sync

    try:
        snapshot = load_app_config_snapshot_sync(bootstrap_config=config)
    except Exception as exc:
        raise QdrantLaunchError(
            "Qdrant起動方式をDBから確認できません。PostgreSQL/設定DBのreadinessを確認してください"
        ) from exc
    initial = getattr(config, "config", None)
    if not isinstance(initial, dict) or initial.get("rag", {}).get("qdrant", {}) != snapshot.get("rag", {}).get("qdrant", {}):
        raise QdrantLaunchError("Qdrant設定がConfig読込後に変わりました。設定を再読込して起動してください")
    return snapshot


def launch_plan(config: object | None, *, is_windows: bool) -> LaunchPlan | None:
    rag = _effective_config(config).get("rag", {})
    if not isinstance(rag, dict):
        raise QdrantLaunchError("rag は設定オブジェクトで指定してください")
    raw = rag.get("qdrant", {})
    if not isinstance(raw, dict):
        raise QdrantLaunchError("rag.qdrant は設定オブジェクトで指定してください")
    mode = raw.get("mode", "external")
    if mode == "external":
        return None  # Includes legacy local_path embedded storage.
    if mode != "local-managed":
        raise QdrantLaunchError("rag.qdrant.mode は external / local-managed のいずれかです")
    if raw.get("local_path") is not None:
        raise QdrantLaunchError("local-managed と embedded local_path は併用できません")
    missing = [name for name in ("host", "port", "image", "storage_path") if raw.get(name) in (None, "")]
    if missing:
        raise QdrantLaunchError("local-managedの明示設定が不足: " + ", ".join(missing))
    if raw["host"] != "127.0.0.1":
        raise QdrantLaunchError("local-managedのhostは127.0.0.1のみ対応します（HTTP）")
    port = raw["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise QdrantLaunchError("local-managedのportは1024〜65535の整数で指定してください")
    image = raw["image"]
    if not isinstance(image, str) or not re.fullmatch(r"qdrant/qdrant:v1\.16\.3@sha256:[0-9a-f]{64}", image):
        raise QdrantLaunchError("imageは公式 qdrant/qdrant:v1.16.3@sha256:<確認済み64桁digest> が必要です")
    if raw.get("api_key"):
        raise QdrantLaunchError("local-managedは認証情報を生成/受渡ししません。api_key設定はexternalで管理してください")
    storage = raw["storage_path"]
    if not isinstance(storage, str) or not storage.startswith("/") or any(c in storage for c in (",", "\n", "\r", "\0", "\\")) or str(PurePosixPath(storage)) != storage or ".." in PurePosixPath(storage).parts or storage == "/":
        raise QdrantLaunchError("storage_pathはDockerホスト内の正規化済み絶対ディレクトリが必要です")
    distro = raw.get("wsl_distribution")
    if is_windows:
        if not isinstance(distro, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", distro):
            raise QdrantLaunchError("Windowsでは既存のwsl_distributionを明示してください")
    elif distro:
        raise QdrantLaunchError("Linuxではwsl_distributionを指定しないでください")
    if not is_windows and (not Path(storage).is_dir() or str(Path(storage).resolve()) != storage):
        raise QdrantLaunchError("storage_pathは既存ディレクトリの実体パスで指定してください")
    return LaunchPlan(image, storage, port, distro if is_windows else None)


def _owner_host_labels() -> dict[str, str]:
    """Scope PID observations to the same host and Linux PID namespace."""
    labels = {"owner-platform": sys.platform, "owner-host": socket.gethostname()}
    if sys.platform == "linux":
        try:
            # A procfs inherited from an outer PID namespace must not make
            # psutil's absent-PID result look like proof about our namespace.
            if os.readlink("/proc/self") != str(os.getpid()):
                return labels
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            namespace = os.readlink("/proc/self/ns/pid")
        except OSError:
            return labels  # Launch may continue, but orphan recovery cannot.
        if re.fullmatch(r"[0-9a-f-]{36}", boot_id) and re.fullmatch(r"pid:\[[0-9]+\]", namespace):
            labels.update({"owner-boot-id": boot_id, "owner-pid-namespace": namespace})
    return labels


def _owner_process_labels() -> dict[str, str]:
    """Record a scoped local process identity, not a credential/remote PID.

    Creation time is diagnostic only: psutil's Linux wall-clock calculation
    can move after clock adjustments, so a changed time never proves death.
    """
    try:
        created = psutil.Process(os.getpid()).create_time()
        labels = _owner_host_labels()
    except (psutil.Error, OSError) as exc:
        raise QdrantLaunchError("Qdrant所有元processの識別情報を取得できません") from exc
    return {**labels, "owner-pid": str(os.getpid()), "owner-created": repr(created)}


def _owner_process_is_dead(labels: dict) -> bool:
    """Return True only when the scoped PID is conclusively absent.

    PID reuse conservatively blocks recovery, even if the current process is
    unrelated. A Linux PID is meaningful only in the same boot/namespace.
    """
    if sys.platform not in {"win32", "linux"}:
        return False
    try:
        scope = _owner_host_labels()
        if sys.platform == "linux" and not {"owner-boot-id", "owner-pid-namespace"} <= scope.keys():
            return False
        if any(labels.get(_LABEL + key) != value for key, value in scope.items()):
            return False
        raw_pid = labels.get(_LABEL + "owner-pid", "")
        raw_created = labels.get(_LABEL + "owner-created", "")
        if (
            not isinstance(raw_pid, str) or not re.fullmatch(r"[1-9][0-9]*", raw_pid)
            or not isinstance(raw_created, str)
        ):
            return False
        pid = int(raw_pid)
        created = float(raw_created)
        if pid > 2**31 - 1 or not math.isfinite(created) or created <= 0:
            return False
        psutil.Process(pid).create_time()
        return False  # Existing PID is never evidence of a dead owner.
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError, TypeError, OverflowError):
        return False  # Access denied or malformed labels are not proof of death.


def create_command(plan: LaunchPlan, owner: str, name: str) -> list[str]:
    command = plan.docker + [
        "create", "--pull=never", "--rm", "--name", name, "--restart=no",
        "--publish", f"127.0.0.1:{plan.port}:6333/tcp",
        "--mount", f"type=bind,src={plan.storage_path},dst=/qdrant/storage",
    ]
    for key, value in {"owner": owner, "endpoint": plan.endpoint, "storage": plan.storage_path, **_owner_process_labels()}.items():
        command.extend(["--label", _LABEL + key + "=" + value])
    for value in _environment_for_plan(plan):
        command.extend(["--env", value])
    return command + [plan.image]


def container_name(plan: LaunchPlan) -> str:
    """Return the deterministic Docker name used as the cross-process guard."""
    return "aoitalk-qdrant-" + hashlib.sha256(plan.storage_path.encode()).hexdigest()[:24]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ready(plan: LaunchPlan) -> bool:
    # Never let ambient proxies or redirects send this local probe elsewhere.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(plan.endpoint + "/readyz", timeout=1) as response:
            if response.status != 200:
                return False
        with opener.open(plan.endpoint + "/", timeout=1) as response:
            identity = json.loads(response.read(8192))
        return identity.get("title") == "qdrant - vector search engine" and identity.get("version") == "1.16.3"
    except (OSError, ValueError, AttributeError):
        return False


class ManagedQdrant:
    def __init__(self) -> None:
        self.owner = uuid.uuid4().hex  # Not a credential; lifecycle identity only.
        self.plan: LaunchPlan | None = None
        self.container_id: str | None = None
        self.image_id: str | None = None
        self._lock = threading.RLock()
        # Lifecycle I/O is serialized separately from the short state lock so
        # Docker/Qdrant timeouts cannot block health status projection.
        self._lifecycle_lock = threading.RLock()
        self._lifetime: _Lifetime | None = None
        self._failure: str | None = None
        self._configured_mode: str | None = None
        self._container_running: bool | None = None
        self._generation = 0
        self._supervisor_recovery_used = 0
        self._ownership_unknown = False

    @property
    def lifetime_error(self) -> str | None:
        return self._failure

    @property
    def healthy(self) -> bool:
        lifetime = self._lifetime
        supervisor_attached = bool(
            lifetime
            and not lifetime.stopping.is_set()
            and lifetime.process.poll() is None
        )
        return bool(
            self.container_id
            and self.plan
            and not self.lifetime_error
            and (not self.plan.wsl_distribution or supervisor_attached)
        )

    def status(self) -> dict[str, object]:
        """Return a no-I/O lifecycle projection for diagnostics and health routes.

        The projection intentionally reports the last observed state.  It does
        not probe Docker or Qdrant from a request handler, and it never turns a
        degraded managed dependency into a process-wide liveness failure.
        """
        with self._lock:
            plan = self.plan
            lifetime = self._lifetime
            supervisor_attached = bool(
                lifetime
                and not lifetime.stopping.is_set()
                and lifetime.process.poll() is None
            )
            supervisor_state = (
                "attached"
                if supervisor_attached
                else "detached"
                if plan and plan.wsl_distribution
                else "not-required"
                if plan
                else "none"
            )
            if self._configured_mode != "local-managed":
                state = "external" if self._configured_mode == "external" else "unknown"
            elif self._failure:
                state = "degraded"
            elif not self.container_id or not plan:
                state = "stopped"
            elif plan.wsl_distribution and lifetime and lifetime.process.poll() is not None:
                state = "degraded"
            elif self.healthy:
                state = "ready"
            else:
                state = "starting"
            reason = self._failure
            if (
                reason is None
                and state == "degraded"
                and plan
                and plan.wsl_distribution
                and lifetime
                and lifetime.process.poll() is not None
            ):
                reason = "WSL Qdrant foreground supervisorが終了し、container状態を再確認中です"
            ownership = (
                "unknown"
                if self._ownership_unknown
                else "owned"
                if self.container_id and plan
                else "none"
            )
            return {
                "mode": self._configured_mode or "unknown",
                "state": state,
                "endpoint": plan.endpoint if plan else None,
                "container_owned": bool(self.container_id and plan),
                "container_running": self._container_running,
                "ownership": ownership,
                "supervisor": supervisor_state,
                "error": reason,
            }

    def _remember_container_state(self, info: dict) -> None:
        state = info.get("State")
        with self._lock:
            self._container_running = state.get("Running") is True if isinstance(state, dict) else None

    def _state_snapshot(self, lifetime: _Lifetime | None = None) -> tuple[LaunchPlan, str, int] | None:
        """Read lifecycle identity under the short state lock only."""
        with self._lock:
            current = self._lifetime
            if lifetime is not None and (
                current is not lifetime
                or lifetime.stopping.is_set()
                or lifetime.generation != self._generation
            ):
                return None
            if self.plan is None or self.container_id is None:
                return None
            return self.plan, self.container_id, self._generation

    def _discard_local_state(self, *, ownership_unknown: bool, error: str | None = None) -> None:
        """Drop local handles without touching an unverified container."""
        with self._lock:
            self.container_id = None
            self.plan = None
            self.image_id = None
            self._container_running = None
            if ownership_unknown:
                self._ownership_unknown = True
            if error:
                self._failure = error

    def _mark_lifetime_stopping(self, lifetime: _Lifetime | None = None) -> None:
        with self._lock:
            target = lifetime or self._lifetime
            if target is not None:
                target.stopping.set()
            self._generation += 1

    def _record_lifetime_failure(self, lifetime: _Lifetime, detail: str) -> None:
        with lifetime.status_lock:
            if lifetime.stopping.is_set() or lifetime.failure:
                return
            lifetime.failure = f"WSL Qdrant foreground supervisorが予期せず終了しました ({detail})。AoiTalkを正常終了して再起動してください"
            with self._lock:
                self._failure = lifetime.failure
            logger.error("%s", lifetime.failure)

    def _watch_lifetime(self, lifetime: _Lifetime) -> None:
        try:
            code = lifetime.process.wait()
            detail = f"exit={code}"
        except Exception as exc:
            detail = type(exc).__name__
        if lifetime.stopping.is_set():
            return
        # Serialize lifecycle transitions without holding the short state lock
        # across Docker/Qdrant I/O.  Health projection can therefore remain
        # responsive while recovery waits on a 15-second Docker inspect.
        with self._lifecycle_lock:
            snapshot = self._state_snapshot(lifetime)
            if snapshot is None:
                return
            plan, cid, generation = snapshot
            recovery_reason = "container状態を確認できません"
            ownership_verified = False
            reattached = False
            if plan.wsl_distribution:
                for attempt in range(_LIFETIME_RECOVERY_ATTEMPTS):
                    if attempt:
                        time.sleep(_LIFETIME_RECOVERY_DELAY_SECONDS)
                    if self._state_snapshot(lifetime) is None:
                        return
                    try:
                        info = self._inspect(
                            plan, cid, timeout=_LIFETIME_RECOVERY_IO_TIMEOUT_SECONDS
                        )
                        self._assert_owned(info, plan)
                        ownership_verified = True
                        self._remember_container_state(info)
                        if self._state_snapshot(lifetime) is None:
                            return
                    except QdrantOwnershipError:
                        ownership_verified = False
                        recovery_reason = "container所有権検証に失敗しました"
                        break
                    except QdrantLaunchError:
                        ownership_verified = False
                        recovery_reason = "container状態を確認できません"
                        continue
                    if not info.get("State", {}).get("Running"):
                        recovery_reason = "containerが停止しています"
                        break
                    if not _ready(plan):
                        recovery_reason = "containerは稼働中ですがreadyではありません"
                        break
                    if self._state_snapshot(lifetime) is None:
                        return
                    # TOCTOU guard: readiness alone is insufficient. Reinspect
                    # the same CID and ownership after the HTTP probe.
                    try:
                        ready_info = self._inspect(
                            plan, cid, timeout=_LIFETIME_RECOVERY_IO_TIMEOUT_SECONDS
                        )
                        self._assert_owned(ready_info, plan)
                        ownership_verified = True
                        self._remember_container_state(ready_info)
                        if self._state_snapshot(lifetime) is None:
                            return
                        if not ready_info.get("State", {}).get("Running"):
                            recovery_reason = "ready確認後にcontainerが停止しました"
                            break
                    except QdrantOwnershipError:
                        ownership_verified = False
                        recovery_reason = "container所有権検証に失敗しました"
                        break
                    except QdrantLaunchError:
                        ownership_verified = False
                        recovery_reason = "ready確認後のcontainer検証に失敗しました"
                        break
                    with self._lock:
                        if self._generation != generation or self._lifetime is not lifetime:
                            return
                        if self._supervisor_recovery_used >= 1:
                            recovery_reason = "foreground supervisor再接続上限に達しました"
                            break
                        self._supervisor_recovery_used += 1
                    try:
                        self._start_lifetime(plan, cid)
                    except QdrantLaunchError:
                        recovery_reason = "foreground supervisor再接続に失敗しました"
                        break
                    with self._lock:
                        new_lifetime = self._lifetime
                        stale_reattach = (
                            self._generation != generation
                            or new_lifetime is None
                            or new_lifetime.generation != generation
                            or new_lifetime.stopping.is_set()
                        )
                        if stale_reattach and new_lifetime is not None:
                            new_lifetime.stopping.set()
                    if stale_reattach:
                        return
                    lifetime.stopping.set()
                    reattached = True
                    logger.warning(
                        "WSL Qdrant foreground supervisorが終了しました (%s) が、"
                        "owned containerはreadyのため再接続しました",
                        detail,
                    )
                    break
            if reattached:
                return
            if self._state_snapshot(lifetime) is None:
                return
            self._record_lifetime_failure(lifetime, f"{detail}; {recovery_reason}")
            if ownership_verified:
                self.stop()  # Verify ownership and normally stop Docker.
                return
            # An ownership mismatch or an unverified Docker state must never
            # trigger Docker operations against a potentially foreign target.
            safe_error = (
                "Qdrant containerの所有権を検証できないため、container操作を行わず"
                "ローカル参照を破棄しました"
            )
            self._mark_lifetime_stopping(lifetime)
            self._release_lifetime()
            self._discard_local_state(ownership_unknown=True, error=safe_error)

    def _start_lifetime(self, plan: LaunchPlan, cid: str) -> None:
        if not plan.wsl_distribution:
            return
        environment = {key: value for key, value in os.environ.items() if key not in _DOCKER_ENV}
        try:
            process = subprocess.Popen(
                plan.docker + ["wait", cid], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise QdrantLaunchError(f"WSL Qdrant foreground supervisorを起動できません: {type(exc).__name__}") from exc
        with self._lock:
            lifetime = _Lifetime(process, generation=self._generation)
            self._lifetime = lifetime
        threading.Thread(target=self._watch_lifetime, args=(lifetime,), name="aoitalk-qdrant-lifetime", daemon=True).start()
        self._require_lifetime(plan)
        logger.info("WSL Qdrant foreground supervisorを開始しました")

    def _require_lifetime(self, plan: LaunchPlan) -> None:
        if self.lifetime_error:
            raise QdrantLaunchError(self._failure)
        if plan.wsl_distribution and self._lifetime is None:
            raise QdrantLaunchError("WSL Qdrant foreground supervisorがありません")
        if self._lifetime and self._lifetime.stopping.is_set():
            raise QdrantLaunchError("所有Qdrant foreground supervisorのcleanupが未完了です")
        if (
            plan.wsl_distribution
            and self._lifetime
            and self._lifetime.process.poll() is not None
        ):
            raise QdrantLaunchError(
                "WSL Qdrant foreground supervisorが予期せず終了しました。container状態を再確認中です"
            )

    def _release_lifetime(self) -> None:
        lifetime = self._lifetime
        if lifetime is None:
            return
        lifetime.stopping.set()
        process = lifetime.process
        try:
            try:
                process.wait(timeout=5)  # Docker stop comes first; natural exit.
            except subprocess.TimeoutExpired:
                logger.warning("所有Qdrant wait clientの終了待機がtimeoutしました")
                process.terminate()  # This owned client only; never stop WSL.
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            with self._lock:
                self._failure = f"所有Qdrant wait clientのcleanupに失敗しました: {type(exc).__name__}"
            logger.error("%s", self._failure)
            raise QdrantLaunchError(self._failure) from exc
        finally:
            if process.poll() is not None:
                with self._lock:
                    if self._lifetime is lifetime:
                        self._lifetime = None

    def _execute(self, command: list[str], *, timeout: float = 15) -> str:
        environment = {key: value for key, value in os.environ.items() if key not in _DOCKER_ENV}
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise QdrantLaunchError(f"ローカルQdrant準備コマンドを実行できません: {type(exc).__name__}") from exc
        if result.returncode:
            # No credentials are included in these commands.
            raise QdrantLaunchError(f"ローカルQdrant準備コマンド失敗: {result.stderr.strip()[:600]}")
        return result.stdout.strip()

    def _run(self, plan: LaunchPlan, *args: str, timeout: float = 15) -> str:
        return self._execute(plan.docker + list(args), timeout=timeout)

    def _validate_wsl_storage(self, plan: LaunchPlan) -> None:
        if not plan.wsl_distribution:
            return
        prefix = ["wsl.exe", "--distribution", plan.wsl_distribution, "--exec"]
        # This first WSL command may also cold-start the distribution. Keep
        # ordinary Docker commands bounded at 15s, but allow its boot here.
        canonical = self._execute(prefix + ["realpath", "-e", "--", plan.storage_path], timeout=45)
        if canonical != plan.storage_path:
            raise QdrantLaunchError("storage_pathは指定WSL内の実体パスで指定してください")
        self._execute(prefix + ["test", "-d", plan.storage_path])

    def _inspect(self, plan: LaunchPlan, container_id: str, *, timeout: float = 15) -> dict:
        try:
            result = json.loads(
                self._run(
                    plan,
                    "inspect",
                    "--type",
                    "container",
                    container_id,
                    timeout=timeout,
                )
            )
            if len(result) != 1 or result[0].get("Id") != container_id:
                raise ValueError("container ID mismatch")
            return result[0]
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise QdrantLaunchError("Dockerのcontainer検証結果が不正です") from exc

    def _assert_owned(self, info: dict, plan: LaunchPlan) -> None:
        self._assert_container_contract(
            info, plan, owner=self.owner, container_id=self.container_id,
            image_id=self.image_id,
        )

    @staticmethod
    def _assert_container_contract(
        info: dict, plan: LaunchPlan, *, owner: str,
        container_id: str | None, image_id: str | None,
    ) -> None:
        config = info.get("Config", {})
        host = info.get("HostConfig", {})
        labels = config.get("Labels") or {}
        mounts = info.get("Mounts") or []
        expected_ports = {"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(plan.port)}]}
        network = info.get("NetworkSettings") or {}
        running = info.get("State", {}).get("Running") is True
        valid = (
            info.get("Id") == container_id
            and info.get("Image") == image_id
            and config.get("Image") == plan.image
            and all(labels.get(_LABEL + key) == value for key, value in {"owner": owner, "endpoint": plan.endpoint, "storage": plan.storage_path}.items())
            and host.get("PortBindings") == expected_ports
            and host.get("PublishAllPorts") is False
            and host.get("Privileged") is False
            and host.get("NetworkMode") in ("default", "bridge")
            and host.get("RestartPolicy", {}).get("Name") in ("no", "")
            and host.get("AutoRemove") is True
            and (not running or {key: value for key, value in (network.get("Ports") or {}).items() if value} == expected_ports)
            and (not running or set(network.get("Networks") or {}) == {"bridge"})
            and not host.get("Binds")
            and len(mounts) == 1
            and mounts[0].get("Type") == "bind"
            and mounts[0].get("Source") == plan.storage_path
            and mounts[0].get("Destination") == "/qdrant/storage"
            and mounts[0].get("RW") is True
            and {value for value in config.get("Env", []) if value.startswith("QDRANT_")} == set(_environment_for_plan(plan))
        )
        if not valid:
            raise QdrantOwnershipError(
                "Qdrant containerの所有権/endpoint/storage/image/公開設定が一致しません。操作を拒否します"
            )

    def _recover_dead_owner(self, plan: LaunchPlan, info: dict, image_id: str) -> None:
        """Stop only one exact managed CID whose original process is dead.

        No adoption, volume removal, force removal, name-based stop, or port
        fallback is allowed. Legacy containers lack process identity and still
        need a manual ownership check. Recheck the immutable CID immediately
        before mutation, and never publish the old container as locally owned.
        """
        cid = info.get("Id")
        labels = (info.get("Config") or {}).get("Labels") or {}
        owner = labels.get(_LABEL + "owner")
        if (
            not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{64}", cid)
            or info.get("Name") != "/" + container_name(plan)
            or not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner)
            or owner == self.owner
        ):
            raise QdrantLaunchError("指定storageを使う別containerがあります。停止/採用は行いません")
        self._assert_container_contract(
            info, plan, owner=owner, container_id=cid, image_id=image_id,
        )
        if not _owner_process_is_dead(labels):
            raise QdrantLaunchError(
                "指定storageを使う別containerの所有元が終了したことを確認できません。停止/採用は行いません"
            )
        current = self._inspect(plan, cid, timeout=2)
        self._assert_container_contract(
            current, plan, owner=owner, container_id=cid, image_id=image_id,
        )
        if (
            current.get("Name") != "/" + container_name(plan)
            or (current.get("Config") or {}).get("Labels") != labels
            or not _owner_process_is_dead(labels)
        ):
            raise QdrantOwnershipError("Qdrant orphanの所有元を再検証できません。操作を拒否します")
        if current.get("State", {}).get("Running"):
            self._run(plan, "stop", "--time", "15", cid, timeout=20)
        else:
            self._run(plan, "rm", cid, timeout=5)
        # --rm removal may finish shortly after stop returns. Never race a new
        # create against removal; the verification has one short total budget.
        deadline = time.monotonic() + _ORPHAN_REMOVAL_TIMEOUT_SECONDS
        while True:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise QdrantLaunchError("所有元が終了したQdrant containerの削除を確認できません")
            remaining = self._run(
                plan, "ps", "--all", "--quiet", "--no-trunc", timeout=min(2, budget),
            ).split()
            if cid not in remaining:
                break
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        logger.warning("所有元が終了したQdrant containerを停止・削除しました（bind storageは保持）: %s", cid)

    def _name_conflict_detail(self, plan: LaunchPlan, name: str) -> str | None:
        """Describe a fixed-name race without adopting or stopping its owner."""
        try:
            candidates = self._run(
                plan,
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"name=^/{name}$",
            ).split()
            if len(candidates) != 1 or not re.fullmatch(r"[0-9a-f]{64}", candidates[0]):
                return None
            info = self._inspect(plan, candidates[0])
            labels = (info.get("Config") or {}).get("Labels") or {}
            owner = labels.get(_LABEL + "owner")
            state = "稼働中" if (info.get("State") or {}).get("Running") else "停止済み"
            if owner == self.owner:
                return None
            return (
                f"Qdrant container名 {name} は別AoiTalk実行が所有する{state}containerと競合しました。"
                f"endpoint={plan.endpoint}。既存containerは採用・停止していません。"
            )
        except (QdrantLaunchError, TypeError, AttributeError, ValueError):
            return None

    def start(self, config: object | None, *, is_windows: bool, timeout: float = 30, reserved_ports: set[int] | None = None) -> bool:
        with self._lifecycle_lock:
            if self.lifetime_error:
                raise QdrantLaunchError(self._failure)
            plan = launch_plan(config, is_windows=is_windows)
            if plan is None:
                if self.container_id:
                    raise QdrantLaunchError("稼働中のmanaged設定を切り替えず、AoiTalkを正常終了してから再起動してください")
                with self._lock:
                    self._configured_mode = "external"
                return False
            with self._lock:
                self._configured_mode = "local-managed"
            if plan.port in (reserved_ports or set()):
                raise QdrantLaunchError("Qdrant portがAoiTalkのFastAPI/frontend/Caddy用portと重複しています")
            if self.container_id:
                if plan != self.plan:
                    raise QdrantLaunchError("同一AoiTalk実行中のQdrant設定変更は再起動が必要です")
                info = self._inspect(plan, self.container_id)
                self._assert_owned(info, plan)
                self._remember_container_state(info)
                self._require_lifetime(plan)
                if not info.get("State", {}).get("Running") or not _ready(plan):
                    raise QdrantLaunchError("所有Qdrant serverがreadyではありません")
                _apply_drvfs_optimizer_override(plan)
                self._require_lifetime(plan)
                return False
            self._validate_wsl_storage(plan)
            version = self._run(plan, "version", "--format", "{{.Server.Version}}")
            if not re.match(r"\d+\.", version) or int(version.split(".", 1)[0]) < 28:
                raise QdrantLaunchError("loopback公開の安全性のため既存Docker Engine 28以上が必要です")
            try:
                image_info = json.loads(self._run(plan, "image", "inspect", plan.image))
                valid_image = isinstance(image_info, list) and len(image_info) == 1 and re.fullmatch(r"sha256:[0-9a-f]{64}", image_info[0].get("Id", ""))
            except (TypeError, ValueError, AttributeError):
                image_info = []
                valid_image = False
            expected_digest = "qdrant/qdrant@" + plan.image.split("@", 1)[1]
            if not valid_image or expected_digest not in (image_info[0].get("RepoDigests") or []):
                raise QdrantLaunchError("指定digestの公式imageがlocalにありません。初回準備を別途行ってください")
            # Inspect every storage user before any cleanup. Multiple users or
            # foreign/live/unverifiable owners always fail closed.
            ids = self._run(plan, "ps", "--all", "--quiet", "--no-trunc").split()
            storage_users = []
            for container_id in ids:
                info = self._inspect(plan, container_id)
                if any(mount.get("Source") == plan.storage_path for mount in info.get("Mounts", [])):
                    storage_users.append(info)
            if len(storage_users) > 1:
                raise QdrantLaunchError("指定storageを使う別containerが複数あります。停止/採用は行いません")
            if storage_users:
                self._recover_dead_owner(plan, storage_users[0], image_info[0]["Id"])
            # Host-side check never kills or takes over the listening process.
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", plan.port)) == 0:
                    raise QdrantLaunchError(f"Qdrant port {plan.port} は既に使用中です。既存processは操作しません")
            with self._lock:
                self.image_id = image_info[0]["Id"]
                self.plan = plan
                self._generation += 1
                self._supervisor_recovery_used = 0
                self._ownership_unknown = False
            # Docker name uniqueness is the atomic guard against two app
            # launches racing to mount the same storage on different ports.
            name = container_name(plan)
            command = create_command(plan, self.owner, name)
            try:
                try:
                    cid = self._run(plan, *command[len(plan.docker):])
                except QdrantLaunchError as exc:
                    if _CONTAINER_NAME_CONFLICT.search(str(exc)):
                        detail = self._name_conflict_detail(plan, name)
                        if detail:
                            logger.warning("%s", detail)
                            raise QdrantLaunchError(detail) from exc
                    raise
                if not re.fullmatch(r"[0-9a-f]{64}", cid):
                    raise QdrantLaunchError("Docker createが正しいcontainer IDを返しませんでした")
                with self._lock:
                    self.container_id = cid
                info = self._inspect(plan, cid)
                self._assert_owned(info, plan)
                self._remember_container_state(info)
                self._run(plan, "start", cid)
                with self._lock:
                    self._container_running = True
                self._start_lifetime(plan, cid)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    self._require_lifetime(plan)
                    info = self._inspect(plan, cid)
                    self._assert_owned(info, plan)
                    self._remember_container_state(info)
                    if not info.get("State", {}).get("Running"):
                        raise QdrantLaunchError("所有Qdrant serverがreadiness前に終了しました")
                    if _ready(plan):
                        # TOCTOU guard: verify the same CID and ownership after
                        # the HTTP readiness probe before publishing ready.
                        ready_info = self._inspect(plan, cid)
                        self._assert_owned(ready_info, plan)
                        self._remember_container_state(ready_info)
                        if not ready_info.get("State", {}).get("Running"):
                            raise QdrantLaunchError("ready確認後にcontainerが停止しました")
                        self._require_lifetime(plan)
                        _apply_drvfs_optimizer_override(plan)
                        return True
                    time.sleep(0.2)
                raise QdrantLaunchError(f"Qdrant readiness timeout: {plan.endpoint}。storageを確認してください")
            except BaseException as exc:
                try:
                    if self.container_id is None:
                        # create may succeed in the daemon but lose its reply.
                        # Recover only this name AND this process's owner label.
                        candidates = self._run(plan, "ps", "--all", "--quiet", "--no-trunc", "--filter", f"name=^/{name}$", "--filter", f"label={_LABEL}owner={self.owner}").split()
                        if len(candidates) == 1 and re.fullmatch(r"[0-9a-f]{64}", candidates[0]):
                            self.container_id = candidates[0]
                    self.stop()
                except QdrantLaunchError as cleanup:
                    raise QdrantLaunchError(f"{exc}; 所有containerのcleanupも失敗: {cleanup}") from exc
                raise

    def stop(self) -> None:
        # Publish shutdown intent before waiting for a recovery transition.
        # A watcher blocked in bounded Docker I/O will observe the generation
        # fence and skip reattach/cleanup when it returns.
        with self._lock:
            lifetime = self._lifetime
            if lifetime:
                lifetime.stopping.set()
            self._generation += 1
        with self._lifecycle_lock:
            with self._lock:
                lifetime = self._lifetime
            if not self.container_id or not self.plan:
                self._release_lifetime()
                with self._lock:
                    self.container_id = None
                    self.plan = None
                    self.image_id = None
                    self._container_running = None
                return
            plan, cid = self.plan, self.container_id
            try:
                # A missing container is normal for --rm after a server-side exit.
                ids = self._run(plan, "ps", "--all", "--quiet", "--no-trunc").split()
                if cid in ids:
                    info = self._inspect(plan, cid)
                    self._assert_owned(info, plan)
                    if info.get("State", {}).get("Running"):
                        self._run(plan, "stop", "--time", "15", cid, timeout=20)
                    else:
                        # Storage is a bind mount and is never removed.
                        self._run(plan, "rm", cid)
                    remaining = self._run(plan, "ps", "--all", "--quiet", "--no-trunc").split()
                    if cid in remaining:
                        info = self._inspect(plan, cid)
                        self._assert_owned(info, plan)
                        if info.get("State", {}).get("Running"):
                            raise QdrantLaunchError("所有Qdrant containerの停止を確認できませんでした")
            except QdrantOwnershipError:
                safe_error = (
                    "Qdrant containerの所有権を検証できないため、container操作を行わず"
                    "ローカル参照を破棄しました"
                )
                self._discard_local_state(ownership_unknown=True, error=safe_error)
                raise
            finally:
                self._release_lifetime()
            with self._lock:
                self.container_id = None
                self.plan = None
                self.image_id = None
                self._container_running = None
            if self._failure:
                logger.info("所有Qdrantのcleanupを完了しました。異常状態は保持され、新しいAoiTalkプロセスでの再起動が必要です")


_managed_qdrant = ManagedQdrant()


def start_managed_qdrant(config: object | None, *, is_windows: bool, reserved_ports: set[int] | None = None) -> bool:
    return _managed_qdrant.start(config, is_windows=is_windows, reserved_ports=reserved_ports)


def stop_managed_qdrant() -> None:
    _managed_qdrant.stop()


def managed_qdrant_status() -> dict[str, object]:
    """Return the process-local Qdrant lifecycle projection without probing."""
    return _managed_qdrant.status()
