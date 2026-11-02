"""AoiTalk を最短の停止時間で再起動する（統括・運用者用の正規手順）。

    venv\\Scripts\\python.exe scripts\\restart_aoitalk.py [--timeout 60] [--skip-build] [--force-after N]

1. build  : 85 の Build-AoiTalk-Production.ps1 -Staged で、稼働中の .next を
            そのままにして frontend/.next-staged を作る（稼働は止めない）。
2. reserve: POST /api/admin/restart-drain で再起動を予約する。41 は新しい
            agent work の開始を止め、中断できない区間（外部送信など）が
            終わったら自分で GPU を解放して正常終了する。中断してよい work は
            停止時に再キューされ、再起動後に続きから再開する。
3. stop   : 3000/3002 が閉じるまで待つ。予約が上限時間で取り消されたら、
            41 は元の状態に戻っているので、何も変えずに終了する。
            --force-after N のときは、N 秒たっても drain が終わらなければ、
            中断できない区間が開いていない限り、41 の main.py を process tree
            ごと強制終了して先へ進む（止まった生成などで待ち続けないため）。
4. swap   : .next-staged を .next に切り替える（数秒）。使えないときだけ、
            停止中に従来どおり build する。
5. start  : 前の supervisor の子（Start-AoiTalk-Supervised.ps1）の終了を確認し、
            85 supervisor（127.0.0.1:6044）から起動する。--restart-supervisor の
            ときは、その前に supervisor をタスク 85_CaddyGateway - Supervisor で
            入れ替える（41 の launcher は supervisor の job object に入っていて
            supervisor と一緒に終わるので、41 の停止中にだけ行う）。
6. verify : health（新しい boot_id）、ログイン画面、管理者ログインを確認する。

拒否・失敗のときは終了コード 2（41 は稼働のまま、設定変更なし）。
停止後の確認に失敗したときは 3（41 が何も応答していなければ、その前に
start と verify を 1 回だけやり直す）。停止したまま supervisor から起動できない
ときは 4 で、出力の recovery に復旧手順を出す（復旧後は --start-only で
start と verify だけを行う）。秘密値は表示しない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

AOI = Path(__file__).resolve().parents[1]
GW = Path(os.environ.get("AOITALK_GATEWAY_ROOT") or AOI.parent / "85_CaddyGateway")
HEALTH_URL = "http://127.0.0.1:3000/api/health"
LOGIN_URL = "http://127.0.0.1:3002/at/login"
SUPERVISOR_URL = "http://127.0.0.1:6044/at/.gateway/mobile/runtime"
SUPERVISOR_HEADERS = {"Host": "127.0.0.1:6044"}
SUPERVISOR_HEALTH_URL = "http://127.0.0.1:6199/__supervisor/healthz"
POLL_SECONDS = 5.0
# The server cancels its own reservation at the deadline; this only bounds the wait.
LOCAL_SLACK_SECONDS = 120.0
LAUNCHER_SCRIPT = "Start-AoiTalk-Supervised.ps1"
LAUNCHER_EXIT_WAIT_SECONDS = 120.0
START_ATTEMPTS = 3
# After the supervisor reports ready, the new boot must answer /api/health.
NEW_BOOT_WAIT_SECONDS = 30.0
# After its ports closed, the previous main.py gets this long to end by itself.
MAIN_EXIT_WAIT_SECONDS = 60.0
# A forced stop never interrupts these: an external send or partial write may be open.
UNFORCEABLE_REASONS = {"agent_work_non_preemptible"}
# 41 is stopped and the supervisor did not start it: the operator must act now.
EXIT_START_BLOCKED = 4
START_ONLY_COMMAND = r"venv\Scripts\python.exe scripts\restart_aoitalk.py --start-only"
SUPERVISOR_TASK = "85_CaddyGateway - Supervisor"

sys.path.insert(0, str(AOI / "plugins" / "AoiTalk_Plugin"))
sys.path.insert(0, str(AOI))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class Abort(Exception):
    def __init__(self, stage: str, reason: str, code: int = 2, **detail):
        super().__init__(reason)
        self.stage, self.reason, self.code, self.detail = stage, reason, code, detail


def emit(stage: str, **fields) -> None:
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def http_json(url: str, *, method: str = "GET", headers=None, timeout: float = 10):
    """Return ``(status, body)``; ``(None, None)`` when nothing answered."""
    request = urllib.request.Request(
        url, method=method, headers=headers or {}, data=b"" if method == "POST" else None
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except Exception:  # noqa: BLE001 - refused / timeout
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return status, None


def listening(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def health() -> dict | None:
    status, body = http_json(HEALTH_URL, timeout=5)
    return body if status == 200 and isinstance(body, dict) and body.get("status") == "ok" else None


def run_build(*, staged: bool) -> str:
    """Run 85's branded build; staged leaves the served .next untouched."""
    stage = "build" if staged else "rebuild"
    pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
    script = GW / "scripts" / "Build-AoiTalk-Production.ps1"
    if not pwsh or not script.is_file():
        raise Abort(stage, "pwsh または 85 の Build-AoiTalk-Production.ps1 が見つかりません",
                    code=2 if staged else 3)
    # Output goes to a file: a pipe inherited by a long-lived child hung earlier scripts.
    log_dir = AOI / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fd, log_name = tempfile.mkstemp(prefix="aoitalk-staged-build-", suffix=".log", dir=log_dir)
    started = time.monotonic()
    try:
        with os.fdopen(fd, "wb") as log:
            process = subprocess.Popen(
                [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
                + (["-Staged"] if staged else []),
                cwd=str(GW), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
            try:
                code = process.wait(3600)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = "timeout"
    except BaseException:
        Path(log_name).unlink(missing_ok=True)
        raise
    text = Path(log_name).read_text(encoding="utf-8", errors="replace")
    match = re.search(r"AoiTalk staged build: (current|staged)", text) if staged else None
    if code != 0 or (staged and not match):
        reason = "staged build に失敗しました（41 は稼働のまま）" if staged else "停止中の build に失敗しました"
        raise Abort(stage, reason, code=2 if staged else 3, log=log_name, tail=text[-1200:])
    Path(log_name).unlink(missing_ok=True)
    result = match.group(1) if match else "built"
    emit(stage, ok=True, result=result, seconds=round(time.monotonic() - started))
    return result


async def reserve_and_wait(boot_id: str, timeout: float, *, force_after: float | None = None) -> bool:
    """Reserve the restart and follow it until 41 has started its shutdown.

    Returns True when ``force_after`` passed with only forceable work left:
    the caller then stops 41 by force.
    """
    from aoitalk_plugin import aoitalk_manage

    operation_id = f"restart-{uuid.uuid4()}"

    async def call(client):
        response = await client.http.post(
            "/api/admin/restart-drain",
            # The reservation must outlive the force deadline it serves.
            json={"expected_boot_id": boot_id, "operation_id": operation_id,
                  "timeout_seconds": timeout if force_after is None else max(timeout, force_after + 60)},
        )
        body = response.json() if response.content else {}
        if response.status_code in (404, 405):
            # A 41 started before the drain API existed (a catch-all route
            # answers 405): one idle-only graceful shutdown attempt, never a
            # retry loop.
            legacy = await client.http.post(
                "/api/admin/shutdown",
                json={"expected_boot_id": boot_id, "operation_id": operation_id},
            )
            legacy_body = legacy.json() if legacy.content else {}
            if legacy.status_code == 200 and legacy_body.get("success"):
                emit("reserve", ok=True, mode="legacy_shutdown")
                return {"stopping": True}
            return {"refused": legacy_body.get("error") or f"http_{legacy.status_code}"}
        if response.status_code != 202:
            return {"refused": body.get("error") or f"http_{response.status_code}"}
        emit("reserve", ok=True, operation_id=operation_id,
             deadline_at=body.get("deadline_at"), waiting_count=body.get("waiting_count"))
        reserved = time.monotonic()
        local_deadline = reserved + timeout + LOCAL_SLACK_SECONDS
        if force_after is not None:
            local_deadline = max(local_deadline, reserved + force_after + LOCAL_SLACK_SECONDS)
        last = None
        try:
            while time.monotonic() < local_deadline:
                await asyncio.sleep(POLL_SECONDS)
                try:
                    response = await client.http.get("/api/admin/restart-drain")
                except Exception:  # noqa: BLE001 - the server may be going away
                    if listening(3000):
                        continue
                    return {"stopping": True}
                if response.status_code != 200:
                    continue
                status = response.json()
                if status.get("boot_id") != boot_id:
                    return {"stopping": True}
                state = status.get("state")
                if state == "shutting_down":
                    return {"stopping": True}
                if state == "idle":
                    return {"cancelled": status.get("last_result") or {}}
                view = (status.get("waiting_count"), tuple(status.get("waiting_reasons") or ()))
                if view != last:
                    last = view
                    emit("drain", waiting_count=view[0], waiting_reasons=list(view[1]),
                         waiting_items=status.get("waiting_items"),
                         remaining_seconds=status.get("remaining_seconds"))
                if (force_after is not None and time.monotonic() - reserved >= force_after
                        and not UNFORCEABLE_REASONS & set(view[1])):
                    # The reservation stays: new work remains refused until the stop.
                    return {"force": list(view[1])}
            reason = "script_timeout"
        except (asyncio.CancelledError, KeyboardInterrupt):
            reason = "script_interrupted"
        # Leave nothing half-done: hand the gates back to normal operation.
        try:
            await client.http.delete("/api/admin/restart-drain")
        except Exception:  # noqa: BLE001
            pass
        return {"cancelled": {"reason": reason}}

    try:
        result = await aoitalk_manage._session(call)
    except aoitalk_manage.ManageError as exc:
        raise Abort("reserve", f"管理者ログインに失敗しました: {exc.code}") from None
    if "force" in result:
        emit("drain", ok=False, force=True, waiting_reasons=result["force"])
        return True
    if "refused" in result:
        raise Abort("reserve", f"再起動予約が拒否されました: {result['refused']}")
    if "cancelled" in result:
        last = result["cancelled"]
        raise Abort("drain", f"再起動予約は取り消されました: {last.get('reason')}",
                    waiting_reasons=last.get("waiting_reasons"),
                    waiting_items=last.get("waiting_items"))
    return False


def main_processes() -> list:
    """This checkout's running main.py processes (the venv launcher at the top)."""
    from src.utils.aoitalk_processes import main_processes as find

    return find(AOI / "main.py")


def kill_main(processes: list, reason: str) -> None:
    from src.utils.aoitalk_processes import kill_tree

    for process in processes:
        kill_tree(process)
    emit("stop", forced=True, reason=reason, pids=[process.pid for process in processes])


def force_stop() -> None:
    """Kill this checkout's main.py and its children (Next.js, launchers).

    Preemptible AgentWork leases expire and are retried after the restart; the
    caller never forces while a non-preemptible section is open.
    """
    processes = main_processes()
    if processes:
        kill_main(processes, "force_after")
    elif listening(3000):
        raise Abort("stop", "3000 を持つ 41 の main.py が見つからないため、強制停止しません", code=2)


def wait_stopped(processes: list, *, graceful_only: bool = False) -> None:
    """Stopped means the previous boot's main.py ended, not only that its ports closed.

    2026-10-30 13:30: PID 57800 closed 3000/3002 and kept running its background
    workers; one that does not end after its ports closed is killed with its tree.
    """
    deadline = time.monotonic() + 300
    while listening(3000) or listening(3002):
        if time.monotonic() >= deadline:
            raise Abort("stop", "graceful shutdown 後 5 分たっても 3000/3002 が閉じません", code=3)
        time.sleep(2)
    exit_deadline = time.monotonic() + MAIN_EXIT_WAIT_SECONDS
    alive = [process for process in processes if process.is_running()]
    while alive and time.monotonic() < exit_deadline:
        time.sleep(2)
        alive = [process for process in alive if process.is_running()]
    if alive:
        if graceful_only:
            raise Abort("stop", "正常停止後も旧 main が終了していません", code=3,
                        pids=[process.pid for process in alive])
        kill_main(alive, "outlived_shutdown")
    emit("stop", ok=True)


def swap_frontend() -> None:
    from src.service_manager._frontend_build import (
        _frontend_build_rebuild_reason,
        promote_staged_frontend_build,
    )

    try:
        emit("swap", ok=True, result=promote_staged_frontend_build(AOI))
    except Exception as exc:  # noqa: BLE001 - the previous .next is restored
        emit("swap", ok=False, reason=type(exc).__name__)
    # Startup refuses a build that does not match the source.  Only when the
    # staged build could not be used, fall back to building while stopped.
    reason, _ = _frontend_build_rebuild_reason(AOI / "frontend")
    if reason:
        emit("swap", ok=False, reason=reason, note="停止中に build します")
        run_build(staged=False)


def powershell(command: str, *, timeout: float = 60) -> subprocess.CompletedProcess | None:
    """Run Windows PowerShell; None when it could not run or timed out."""
    try:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", command],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def launcher_pids() -> list[int] | None:
    """PIDs of the supervisor's AoiTalk launcher; None when they cannot be listed."""
    result = powershell(
        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
        f"$_.CommandLine -like '*{LAUNCHER_SCRIPT}*' }} | ForEach-Object {{ $_.ProcessId }}"
    )
    if result is None or result.returncode != 0:
        return None
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def _gateway_common() -> str:
    # Without Stop, a failed dot-source or command still exits 0.
    return (f"$ErrorActionPreference = 'Stop'; . '{GW / 'scripts' / 'Gateway.Common.ps1'}'; "
            "Initialize-GatewayRuntime; ")


def check_supervisor_replaceable() -> None:
    """Refuse --restart-supervisor while 41 is still running and nothing changed."""
    result = powershell(
        _gateway_common()
        + "$o = Get-GatewaySupervisorPortOwner; "
        "if ($null -eq $o -or -not (Test-GatewaySupervisorProcess -ProcessId ([int]$o.OwningProcess))) { exit 3 }; "
        f"Get-ScheduledTask -TaskName '{SUPERVISOR_TASK}' -ErrorAction Stop | Out-Null; "
        "if (-not $env:GATEWAY_SUPERVISOR_ACTIVATION_SECRET) { exit 4 }"
    )
    if result is None or result.returncode != 0:
        reason = {3: "6199 の持ち主が 85 supervisor であることを確認できません",
                  4: "supervisor の activation 値を復元できません"}.get(
            getattr(result, "returncode", None), f"タスク {SUPERVISOR_TASK} を確認できません")
        raise Abort("pre", f"--restart-supervisor を使えません: {reason}")


def service_listeners() -> dict[str, dict[int, int]]:
    """Listening PID per upstream port of every 85 service (impact record)."""
    from urllib.parse import urlsplit

    registry = json.loads((GW / "supervisor" / "services.json").read_text(encoding="utf-8"))
    result = powershell(
        "Get-NetTCPConnection -State Listen | Select-Object LocalPort, OwningProcess | ConvertTo-Json -Compress"
    )
    owners: dict[int, int] = {}
    if result is not None and result.returncode == 0 and result.stdout.strip():
        rows = json.loads(result.stdout)
        for row in rows if isinstance(rows, list) else [rows]:
            owners.setdefault(int(row["LocalPort"]), int(row["OwningProcess"]))
    snapshot = {}
    for service in registry.get("services", []):
        origins = [service.get("upstreamOrigin")] + [
            upstream.get("origin") for upstream in (service.get("upstreams") or {}).values()
        ]
        ports = sorted({urlsplit(origin).port for origin in origins if origin} - {None})
        snapshot[service["id"]] = {port: owners[port] for port in ports if port in owners}
    return snapshot


def supervisor_healthy() -> bool:
    status, body = http_json(SUPERVISOR_HEALTH_URL, timeout=5)
    return status == 200 and isinstance(body, dict) and body.get("ok") is True


def replace_supervisor() -> None:
    """Swap the 85 supervisor while 41 is stopped.

    Its non-detached children (the service launchers) share the supervisor's
    job object and end with it; the services they started and detached
    services keep running.  41 must therefore be stopped first.
    """
    recovery = (f"タスク {SUPERVISOR_TASK} を開始し、/__supervisor/healthz を確認してから "
                f"{START_ONLY_COMMAND} を実行してください")
    before = service_listeners()
    result = powershell(
        _gateway_common()
        + "$o = Get-GatewaySupervisorPortOwner; "
        "$old = if ($null -ne $o) { [int]$o.OwningProcess } else { 0 }; "
        "Stop-GatewaySupervisor | Out-Null; "
        # The graceful stop closes the listener; end the verified old node too.
        "$forced = $false; "
        "if ($old -gt 0) { $p = Get-Process -Id $old -ErrorAction SilentlyContinue; "
        "if ($null -ne $p -and -not $p.WaitForExit(15000)) { Stop-Process -Id $old -Force; $forced = $true } }; "
        f"Start-ScheduledTask -TaskName '{SUPERVISOR_TASK}'; "
        "[pscustomobject]@{ old_pid = $old; forced = $forced } | ConvertTo-Json -Compress",
        timeout=120,
    )
    if result is None or result.returncode != 0:
        tail = (result.stderr or result.stdout)[-600:] if result is not None else "timeout"
        raise Abort("supervisor", "supervisor を入れ替えられませんでした", code=EXIT_START_BLOCKED,
                    tail=tail, recovery=recovery)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    stopped = json.loads(lines[-1]) if lines else {}
    deadline = time.monotonic() + 60
    while not supervisor_healthy():
        if time.monotonic() >= deadline:
            raise Abort("supervisor", "新しい supervisor の healthz が ok になりません",
                        code=EXIT_START_BLOCKED, recovery=recovery)
        time.sleep(1)
    after = service_listeners()
    lost = sorted(sid for sid, ports in before.items() if ports and after.get(sid) != ports)
    emit("supervisor", ok=True, old_pid=stopped.get("old_pid"), forced=stopped.get("forced"),
         services={sid: {"before": before[sid], "after": after.get(sid, {})} for sid in before},
         lost=lost)


def new_boot_of(launchers: list[int], old_boot_id: str | None) -> str | None:
    """The boot_id when a remaining launcher is the parent of a new boot's main.py.

    The supervisor may start 41 by itself while it is stopped (a wake from a
    client); that launcher stays until its main.py ends and is not a leftover.
    """
    current = health()
    boot_id = current.get("boot_id") if current else None
    if not boot_id or boot_id == old_boot_id:
        return None
    parents = set()
    for process in main_processes():
        try:
            parents.add(process.ppid())
        except Exception:  # noqa: BLE001 - it ended meanwhile
            continue
    return str(boot_id) if parents & set(launchers) else None


def wait_launcher_exited(old_boot_id: str | None = None) -> str | None:
    """The supervisor starts a new launcher only after the previous one exited.

    Returns the new boot_id when the remaining launcher already runs a new boot
    (2026-10-30 15:03/15:17: such a launcher was reported as a leftover).
    """
    deadline = time.monotonic() + LAUNCHER_EXIT_WAIT_SECONDS
    pids = launcher_pids()
    while pids and time.monotonic() < deadline:
        boot_id = new_boot_of(pids, old_boot_id)
        if boot_id:
            emit("start", launcher_of_new_boot=pids, boot_id=boot_id)
            return boot_id
        time.sleep(2)
        pids = launcher_pids()
    if pids:
        boot_id = new_boot_of(pids, old_boot_id)
        if boot_id:
            emit("start", launcher_of_new_boot=pids, boot_id=boot_id)
            return boot_id
        raise Abort("start", f"前の launcher ({LAUNCHER_SCRIPT}) が終了しません",
                    code=EXIT_START_BLOCKED, pids=pids,
                    recovery=f"PID {pids} の終了を確認してから {START_ONLY_COMMAND} を実行してください")
    emit("start", launcher_exited=pids is not None)
    return None


def wait_supervisor_saw_stop() -> None:
    """Let the supervisor see the stop before activating.

    A status GET probes the service and replaces the previous boot's cached
    ready with stopped; a supervisor without that fix in POST would otherwise
    answer ready and start nothing.
    """
    deadline = time.monotonic() + 60
    state = None
    while time.monotonic() < deadline:
        _status, body = http_json(SUPERVISOR_URL, headers=SUPERVISOR_HEADERS)
        state = body.get("state") if isinstance(body, dict) else None
        if state in ("stopped", "failed"):
            return
        time.sleep(2)
    emit("start", supervisor_state=state, note="supervisor が停止をまだ見ていません")


def wait_new_boot(old_boot_id: str | None) -> str | None:
    """The boot_id of a 41 that is not ``old_boot_id``; None when none appears."""
    deadline = time.monotonic() + NEW_BOOT_WAIT_SECONDS
    while True:
        current = health()
        if current and current.get("boot_id") != old_boot_id:
            return str(current.get("boot_id"))
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def start_via_supervisor(old_boot_id: str | None = None, *, restart_supervisor: bool = False,
                         graceful_only: bool = False) -> None:
    """Start 41 through the supervisor; success is a new boot, not the 200."""
    if graceful_only and restart_supervisor:
        raise Abort("start", "graceful-only では supervisor を置換できません")
    boot_id = wait_launcher_exited(old_boot_id)
    if boot_id:
        # Already started; replacing the supervisor now would end its launcher.
        emit("start", ok=True, attempt=0, boot_id=boot_id, supervisor_replaced=False)
        return
    if restart_supervisor:
        replace_supervisor()
    code = body = None
    for attempt in range(1, (1 if graceful_only else START_ATTEMPTS) + 1):
        wait_supervisor_saw_stop()
        code, body = http_json(SUPERVISOR_URL, method="POST", headers=SUPERVISOR_HEADERS, timeout=180)
        deadline = time.monotonic() + 240
        while code == 202 and time.monotonic() < deadline:
            time.sleep(2)
            code, body = http_json(SUPERVISOR_URL, headers=SUPERVISOR_HEADERS)
        if code == 200:
            boot_id = wait_new_boot(old_boot_id)
            if boot_id:
                emit("start", ok=True, attempt=attempt, boot_id=boot_id)
                return
            # A supervisor still holding the previous boot's ready answers 200
            # without starting anything; the next attempt's status GET clears it.
            if graceful_only:
                break
            emit("start", ok=False, attempt=attempt, retrying=True,
                 reason="supervisor は ready を返したが、新しい boot が出ません")
            continue
        error = body.get("error_code") if isinstance(body, dict) else None
        # Only temporary failures carry retry_exhausted; anything else stays
        # failed in the supervisor until an operator clears it.
        if not isinstance(body, dict) or body.get("retry_exhausted") is not False:
            break
        if graceful_only:
            break
        emit("start", ok=False, attempt=attempt, error_code=error, retrying=True)
        time.sleep(max(2.0, (body.get("retry_after_ms") or 0) / 1000 + 1))
    error = body.get("error_code") if isinstance(body, dict) else None
    if code == 200:
        raise Abort("start", "supervisor は ready を返しましたが、新しい boot が出ません",
                    code=EXIT_START_BLOCKED,
                    recovery=f"{START_ONLY_COMMAND} --restart-supervisor を実行してください")
    raise Abort(
        "start", f"supervisor から起動できませんでした (HTTP {code}, {error or 'no_response'})",
        code=EXIT_START_BLOCKED, error_code=error,
        recovery=("85 supervisor がこの失敗を保持しています。"
                  f"{START_ONLY_COMMAND} --restart-supervisor を実行してください"
                  if error and code == 503 else
                  f"85 supervisor (6199/6044) の状態を確認してから {START_ONLY_COMMAND} を実行してください"),
        log=str(GW / "runtime" / "logs" / "supervisor-at.err.log"),
    )


async def verify(old_boot_id: str | None) -> None:
    from aoitalk_plugin import aoitalk_manage

    current = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = health()
        if current and current.get("boot_id") != old_boot_id:
            break
        time.sleep(2)
    else:
        raise Abort("verify", "新しい boot の health を確認できません", code=3)
    login_status, _ = http_json(LOGIN_URL, timeout=15)

    async def call(client):
        response = await client.http.get("/api/admin/restart-drain")
        return {"status": response.status_code}

    # reserve logged in moments ago, so the login limiter may still be closed.
    # The new boot is already serving; waiting here delays only this report.
    login_deadline = time.monotonic() + 360
    while True:
        try:
            admin = (await aoitalk_manage._session(call)).get("status")
        except aoitalk_manage.ManageError as exc:
            admin = exc.code
            wait = min(max(exc.retry_after or 30, 5), 120)
            if admin == "rate_limited" and time.monotonic() + wait < login_deadline:
                time.sleep(wait)
                continue
        break
    ok = login_status == 200 and admin == 200
    emit("verify", ok=ok, boot_id=current.get("boot_id"), login_page=login_status, admin_login=admin)
    if not ok:
        raise Abort("verify", "ログイン確認に失敗しました", code=3)


def start_and_verify(old_boot_id: str | None, *, restart_supervisor: bool = False,
                     graceful_only: bool = False) -> None:
    """Start and verify; when nothing came up, retry the start once."""
    if graceful_only:
        try:
            start_via_supervisor(old_boot_id, restart_supervisor=restart_supervisor, graceful_only=True)
            asyncio.run(verify(old_boot_id))
        except Abort as exc:
            try:
                exc.detail["owned_main_pids"] = [process.pid for process in main_processes()]
            except Exception:  # Keep the original start/verify failure if inspection fails.
                exc.detail["owned_main_pids"] = None
            raise
        return
    try:
        start_via_supervisor(old_boot_id, restart_supervisor=restart_supervisor)
    except Abort as exc:
        # A failed startup (a slow dependency, say) stays failed in the
        # supervisor until it is replaced; replace it and start once more.
        if exc.detail.get("error_code") != "startup_failed" or listening(3000) or listening(3002):
            raise
        emit("start", ok=False, reason=exc.reason, retry="restart_supervisor")
        start_via_supervisor(old_boot_id, restart_supervisor=True)
    try:
        asyncio.run(verify(old_boot_id))
    except Abort as exc:
        # Only a fully stopped 41 is restarted; one that answers (or is still
        # binding its ports) is left alone with the original failure.
        if exc.stage != "verify" or health() is not None or listening(3000) or listening(3002):
            raise
        emit("verify", ok=False, reason=exc.reason, retry="start_only")
        start_via_supervisor(old_boot_id)
        asyncio.run(verify(old_boot_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="中断できない work（外部送信など）を待つ上限秒数（超えたら予約を取り消す）")
    parser.add_argument("--skip-build", action="store_true", help="frontend の build を行わない")
    parser.add_argument("--start-only", action="store_true",
                        help="停止中の 41 を supervisor から起動して確認だけ行う（終了コード 4 の復旧用）")
    parser.add_argument("--force-after", type=float, default=None, metavar="N",
                        help="drain が N 秒で終わらなければ、中断できない区間が無い限り 41 を強制停止して進む")
    parser.add_argument("--restart-supervisor", action="store_true",
                        help=f"41 の停止中に 85 supervisor をタスク {SUPERVISOR_TASK} で入れ替えてから起動する")
    parser.add_argument("--graceful-only", action="store_true",
                        help="強制停止せず、supervisor の起動を１回だけ試みる")
    args = parser.parse_args()
    if args.graceful_only and (args.force_after is not None or args.restart_supervisor):
        parser.error("--graceful-only は --force-after / --restart-supervisor と併用できません")
    stage = "pre"
    try:
        if args.start_only:
            if listening(3000) or listening(3002):
                raise Abort("pre", "3000/3002 が使用中です。--start-only は停止中の 41 にだけ使います")
            if args.restart_supervisor:
                check_supervisor_replaceable()
            leftover = main_processes()
            if leftover:
                if args.graceful_only:
                    raise Abort("pre", "旧 main が残っているため起動しません", code=2,
                                pids=[process.pid for process in leftover])
                # A main.py without its ports is a previous boot that never ended.
                kill_main(leftover, "leftover")
            stage = "start"
            if args.graceful_only:
                start_and_verify(None, graceful_only=True)
            else:
                start_and_verify(None, restart_supervisor=args.restart_supervisor)
            emit("done", ok=True)
            return 0
        before = health()
        supervisor, _ = http_json(SUPERVISOR_HEALTH_URL, timeout=5)
        if not before or supervisor is None:
            raise Abort("pre", "41 が health ok でない、または 85 supervisor (6199) が応答しません")
        if args.restart_supervisor:
            check_supervisor_replaceable()
        boot_id = str(before.get("boot_id"))
        previous = main_processes()
        emit("pre", ok=True, boot_id=boot_id)
        if not args.skip_build:
            stage = "build"
            run_build(staged=True)
        stage = "reserve"
        forced = asyncio.run(reserve_and_wait(boot_id, args.timeout, force_after=args.force_after))
        stage = "stop"
        if forced:
            if args.graceful_only:
                raise Abort("stop", "graceful-only では強制停止できません", code=3)
            force_stop()
        if args.graceful_only:
            wait_stopped(previous, graceful_only=True)
        else:
            wait_stopped(previous)
        swap_frontend()
        stage = "start"
        if args.graceful_only:
            start_and_verify(boot_id, graceful_only=True)
        else:
            start_and_verify(boot_id, restart_supervisor=args.restart_supervisor)
    except Abort as exc:
        emit(exc.stage, ok=False, reason=exc.reason, **exc.detail)
        return exc.code
    except KeyboardInterrupt:
        emit(stage, ok=False, reason="interrupted")
        return 2 if stage in ("pre", "build", "reserve") else 3
    emit("done", ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
