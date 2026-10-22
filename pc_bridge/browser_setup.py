"""Local GUI settings and explicit setup; rendering never opens browser sockets.

No Tk, filesystem probing, helper construction or process APIs are needed to
validate settings or display this model. Target handles live only in this GUI
session, never in saved configuration. The client owns all browser authority.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import json
import os
from pathlib import Path
import queue
import re
import stat
import tempfile
import threading
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .edge_cdp import EdgeCdpError, validate_loopback_endpoint

BACKENDS = ("agent_browser",)
CHANNELS = ("stable", "beta", "dev", "canary")
_CONFIG_KEYS = {"server_url", "token", "ca_file", "browser_backend", "agent_browser",
                "machine_id", "saved_actions"}
_SETTINGS_KEYS = {"channel", "endpoint", "user_data_dir", "snapshot_scope", "target_id"}
MAX_CONFIG_BYTES = 65536
_WINDOWS = os.name == "nt"


def validate_config(value, *, require_connection=False):
    """Validate syntax only and return a detached copy of supported local fields."""
    if not isinstance(value, dict) or set(value) - _CONFIG_KEYS:
        raise ValueError("bridge_config_invalid")
    result = deepcopy(value)
    for key in ("server_url", "token", "ca_file"):
        if key in result and (not isinstance(result[key], str) or "\0" in result[key]):
            raise ValueError("bridge_config_invalid")
    if "machine_id" in result:
        from .action_protocol import MACHINE_PATTERN
        if (not isinstance(result["machine_id"], str) or not MACHINE_PATTERN.fullmatch(result["machine_id"])):
            raise ValueError("bridge_config_invalid")
    if "saved_actions" in result:
        # The catalog validates absolute paths and current hashes locally.  It
        # is intentionally separate from the browser/workspace settings and
        # is never used to broaden a server manifest workspace constraint.
        from .action_protocol import ActionCatalog, ActionProtocolError
        try:
            ActionCatalog(result["saved_actions"])
        except (ActionProtocolError, OSError, ValueError):
            raise ValueError("saved_actions_catalog_invalid") from None
    if require_connection:
        # Deferred to avoid importing the client/backend for ordinary settings.
        from .client import websocket_url
        websocket_url(result.get("server_url", ""))
        if not result.get("token", "").strip():
            raise ValueError("bridge_token_required")
    if result.get("browser_backend", "agent_browser") not in {*BACKENDS, "extension", "managed_profile"}:
        raise ValueError("browser_backend_unknown")
    # Migrate saved mode labels to the sole general helper entry. Media uses
    # its independent account/profile selection, never this mode field.
    result["browser_backend"] = "agent_browser"
    settings = result.get("agent_browser", {})
    if not isinstance(settings, dict) or set(settings) - _SETTINGS_KEYS:
        raise ValueError("agent_browser_config_invalid")
    if settings.get("channel", "stable") not in CHANNELS:
        raise ValueError("edge_channel_invalid")
    if "endpoint" in settings:
        # Syntax only: never read an Edge profile or probe the endpoint here.
        try:
            validate_loopback_endpoint(settings["endpoint"])
        except EdgeCdpError:
            raise ValueError("edge_cdp_endpoint_invalid") from None
    if "user_data_dir" in settings:
        profile = settings["user_data_dir"]
        if not isinstance(profile, str) or not profile or "\0" in profile:
            raise ValueError("edge_profile_path_invalid")
    if "target_id" in settings:
        target = settings["target_id"]
        if target is not None and (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target)):
            raise ValueError("agent_browser_target_invalid")
    if "snapshot_scope" in settings:
        scope = settings["snapshot_scope"]
        if (not isinstance(scope, dict) or set(scope) - {"selector", "depth", "max_depth"}
                or {"depth", "max_depth"} <= set(scope)):
            raise ValueError("agent_browser_scope_invalid")
        if "selector" in scope:
            selector = scope["selector"]
            if (not isinstance(selector, str) or not selector or len(selector) > 4096
                    or selector.startswith("--") or "\0" in selector):
                raise ValueError("agent_browser_scope_invalid")
        for key in ("depth", "max_depth"):
            if key in scope and (type(scope[key]) is not int or not 1 <= scope[key] <= 100):
                raise ValueError("agent_browser_scope_invalid")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("bridge_config_invalid")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("bridge_config_invalid")


def load_config(path: Path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("bridge_config_invalid")
    return validate_config(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                      parse_constant=_reject_constant))


def connection_config(current, *, server_url, token, ca_file, backend, channel,
                      endpoint="", user_data_dir=""):
    """Keep GUI settings, starting a fresh target selection for each GUI client."""
    result = validate_config(current)
    result.update(server_url=server_url.strip(), token=token.strip(), ca_file=ca_file.strip(),
                  browser_backend=backend)
    settings = result.setdefault("agent_browser", {})
    if backend == "agent_browser":
        settings.pop("target_id", None)
    settings["channel"] = channel
    for key, value in (("endpoint", endpoint), ("user_data_dir", user_data_dir)):
        if value.strip():
            settings[key] = value.strip()
        else:
            settings.pop(key, None)
    return validate_config(result, require_connection=True)


def save_config(path: Path, value):
    value = validate_config(value, require_connection=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("bridge_config_invalid")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = path.lstat()
    except FileNotFoundError:
        previous = None
    if previous is not None:
        attributes = getattr(previous, "st_file_attributes", 0)
        # Never bypass a protected destination by replacing its directory entry.
        if not stat.S_ISREG(previous.st_mode) or attributes & (0x400 | 0x4000):
            raise OSError("bridge_config_path_protected")
        if not previous.st_mode & 0o222 or attributes & 1:
            raise PermissionError("bridge_config_readonly")
        # Validate actual write permission without truncating or writing bytes.
        with path.open("r+b"):
            pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".bridge-config-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if previous is not None:
                if _WINDOWS:
                    _copy_config_dacl(path, temporary)
                else:
                    os.chmod(temporary, stat.S_IMODE(previous.st_mode))
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        identity = lambda info: None if info is None else (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode,
            getattr(info, "st_file_attributes", 0))
        if identity(current) != identity(previous):
            raise OSError("bridge_config_changed")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_config_dacl(source, destination):
    """Preserve the existing Windows DACL before writing any credential bytes."""
    import win32security
    descriptor = win32security.GetFileSecurity(str(source), win32security.DACL_SECURITY_INFORMATION)
    control, _ = descriptor.GetSecurityDescriptorControl()
    inheritance = (win32security.PROTECTED_DACL_SECURITY_INFORMATION
                   if control & win32security.SE_DACL_PROTECTED
                   else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION)
    flags = (win32security.DACL_SECURITY_INFORMATION | inheritance) & 0xffffffff
    # pywin32 SetFileSecurity parses a Windows signed C long, then passes the
    # same 32 bits as SECURITY_INFORMATION. Preserve the high-bit flag safely.
    if flags >= 0x80000000:
        flags -= 0x100000000
    win32security.SetFileSecurity(str(destination), flags, descriptor)


# Never render arbitrary exception text: it can contain debugger endpoints,
# profile paths, page contents or credentials. Only fixed diagnostic codes pass.
_MESSAGES = {
    "profile_driver_connection_required": "Bridgeに接続してから人格・アカウントを選んでください",
    "profile_driver_profile_selection_required": "専用ブラウザを開いて対象タブを選んでください",
    "profile_driver_catalog_target_required": "本人のアカウント一覧から選んでください",
    "profile_driver_start_failed": "専用ブラウザを開始できません。接続を確認してから手動でやり直してください",
    "profile_driver_close_unconfirmed": "専用ブラウザの終了を確認できません。profileは再利用できません",
    "edge_debugging_endpoint_missing": "Edgeのデバッグ接続先が見つかりません",
    "edge_debugging_endpoint_unreadable": "Edgeのデバッグ接続先を読み取れません",
    "agent_browser_bundle_unavailable": "専用ブラウザhelperが利用できません",
    "agent_browser_manifest_unavailable": "専用ブラウザhelperのmanifestが見つかりません",
    "agent_browser_binary_unavailable": "専用ブラウザhelperの実行ファイルが見つかりません",
    "agent_browser_timeout": "接続確認がタイムアウトしました。許可の結果は不明です",
    "agent_browser_outcome_unknown": "操作の結果は不明です",
    "agent_browser_focus_required": "対象タブの入力欄にフォーカスできませんでした。入力は実行していません",
    "agent_browser_connection_blocked": "Edgeへの接続を確認できません。許可状態は不明です",
    "agent_browser_command_blocked": "ブラウザ操作を完了できませんでした。許可状態は不明です",
    "agent_browser_command_failed": "ブラウザ処理に失敗しました。許可状態は不明です",
    "agent_browser_response_invalid": "ブラウザhelperの応答を確認できません",
    "agent_browser_output_limit": "ブラウザhelperの応答が上限を超えました",
    "agent_browser_permission_pending": "Edge側の許可を待っています",
    "agent_browser_permission_unknown": "Edge側の許可状態は不明です",
    "agent_browser_target_selection_required": "操作対象のタブを選択してください",
    "agent_browser_no_targets": "選択できるページがありません",
    "agent_browser_connection_required": "「Edgeに接続・タブを確認」を押してください",
    "agent_browser_session_faulted": "ブラウザ接続が停止しました",
    "agent_browser_reconnect_required": "ブラウザ接続の再起動が必要です",
    "agent_browser_cancelled": "ブラウザ接続を停止しました",
    "bridge_command_cancelled": "Bridge接続を停止しています",
    "bridge_busy": "別の操作を実行中です。完了後にもう一度操作してください",
    "agent_browser_run_active": "ブラウザ操作中です。完了後にタブを選択してください",
    "browser_setup_failed": "ブラウザ設定を確認できませんでした",
    "browser_setup_result_invalid": "ブラウザ設定の応答を確認できませんでした",
    "browser_setup_client_required": "先にBridgeを接続してください",
    "browser_setup_target_invalid": "一覧から操作対象のタブを選択してください",
}
_SAFE_CODES = frozenset(_MESSAGES) | frozenset({
    "edge_active_port_invalid", "edge_active_port_link_rejected", "edge_profile_path_invalid",
    "edge_cdp_endpoint_invalid", "edge_local_app_data_unavailable", "edge_channel_invalid",
    "edge_process_verification_requires_windows", "edge_listener_owner_unverified",
    "edge_process_identity_unverified", "edge_process_path_invalid", "edge_listener_not_edge",
    "edge_installation_path_unavailable", "edge_channel_path_mismatch", "edge_listener_owner_changed",
    "edge_process_identity_changed", "edge_listener_not_loopback", "edge_listener_table_invalid",
    "edge_listener_query_failed", "agent_browser_manifest_invalid", "agent_browser_binary_path_invalid",
    "agent_browser_binary_size_mismatch", "agent_browser_binary_hash_mismatch",
    "agent_browser_preflight_failed", "agent_browser_bridge_contract_mismatch",
    "agent_browser_runtime_unavailable", "agent_browser_connection_unverified",
    "agent_browser_target_gone", "agent_browser_target_changed", "agent_browser_target_mismatch",
    "agent_browser_initial_connect_required", "agent_browser_target_invalid",
    "agent_browser_scope_invalid", "agent_browser_config_invalid", "browser_backend_unknown",
    "bridge_config_invalid", "bridge_token_required",
})


def safe_error(error):
    value = str(error)
    return value if value in _SAFE_CODES else "browser_setup_failed"


def safe_policy_summary(value):
    """Fixed local observation vocabulary; never display registry payloads."""
    if not isinstance(value, dict):
        return None
    remote = {"deny_observed", "allow_observed", "conflict", "not_observed", "invalid", "unreadable"}
    tools = {"restricted_observed", "allow_observed", "extension_restricted_observed",
             "conflict", "not_observed", "invalid", "unreadable"}
    remote_value, tools_value = value.get("remote_debugging"), value.get("developer_tools")
    remote_valid = isinstance(remote_value, str) and remote_value in remote
    tools_valid = isinstance(tools_value, str) and tools_value in tools
    return {"effective_policy": "unknown", "edge_process_presence": "unknown",
            "cloud_precedence": "not_resolved", "url_scope": "not_observed",
            "platform": "windows" if value.get("platform") == "windows" else "unsupported",
            "remote_debugging": remote_value if remote_valid else "unreadable",
            "developer_tools": tools_value if tools_valid else "unreadable",
            "conflicts": value.get("conflicts") is True,
            "partial_read": value.get("partial_read") is True or not (remote_valid and tools_valid)}


def diagnostic_text(diagnostic, *, busy=False, error=None):
    """Pure display function. A timeout is never evidence of denied permission."""
    code = safe_error(error or diagnostic.get("error")) if error or diagnostic.get("error") else None
    if diagnostic.get("backend") == "managed_profile":
        if busy:
            return "専用ブラウザの準備中…"
        if code:
            return _MESSAGES.get(code, "専用ブラウザの状態を確認できません") + " (" + code + ")"
        if diagnostic.get("blocked") is True:
            return "結果・終了は未確認です。自動で再入力しません。Bridgeを切断して状態を確認してください。"
        if diagnostic.get("connected") is True:
            return "最終確認時点: 専用ブラウザ接続済み・" + ("タブ選択済み" if diagnostic.get("selected") else "タブ未選択") + "。アカウント・保存版の照合はOperationsで行います。"
        return "本人のProject・人格・アカウントを選んで専用ブラウザを開いてください。認証情報は移行しません。"
    if busy:
        return "Edge接続を確認中… Edgeに許可画面が出た場合は内容を確認してください"
    if code:
        text = _MESSAGES.get(code, "ブラウザ接続を確認できません") + " (" + code + ")"
    elif diagnostic.get("permission_pending") is True:
        text = "Edge側の許可を待っています"
    elif diagnostic.get("connected") is True:
        text = "最終確認時点: Edge接続・タブ選択済み" if diagnostic.get("selected") is True else "最終確認時点: Edge接続・タブ未選択"
    else:
        text = "Edge接続は未確認・許可状態は未確認"
    verified = diagnostic.get("last_verified_at")
    if type(verified) in (int, float) and math.isfinite(verified):
        try:
            text += "（" + datetime.fromtimestamp(verified, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") + "）"
        except (OSError, OverflowError, ValueError):
            pass
    if diagnostic.get("connected") is True:
        text += "。現在の接続状態は未確認です"
    if diagnostic.get("blocked") is True:
        text += "。Bridgeを切断してから手動で再接続してください（自動再試行なし）"
    policy = safe_policy_summary(diagnostic.get("edge_policy"))
    if policy:
        notes = []
        if policy["remote_debugging"] == "deny_observed":
            notes.append("RemoteDebuggingAllowedの禁止値を観測")
        if policy["developer_tools"] == "restricted_observed":
            notes.append("開発者ツールの制限値を観測")
        if policy["conflicts"]:
            notes.append("ローカルpolicy値が競合")
        if policy["partial_read"]:
            notes.append("policyの読取は一部未確認")
        if notes:
            text += "。" + "／".join(notes) + "（接続準備時の観測、Edgeの実効設定・適用範囲は未確認）"
    return text


def safe_targets(result):
    if not isinstance(result, dict) or not isinstance(result.get("targets"), list) or len(result["targets"]) > 4096:
        raise ValueError("browser_setup_result_invalid")
    rows, seen = [], set()
    for item in result["targets"]:
        if (not isinstance(item, dict) or type(item.get("tab_id")) is not int
                or not 0 < item["tab_id"] < 2**53 or item["tab_id"] in seen
                or item.get("type") != "page" or not isinstance(item.get("origin"), str)
                or type(item.get("selected")) is not bool):
            raise ValueError("browser_setup_result_invalid")
        title = item.get("title", "")
        if not isinstance(title, str) or len(title) > 512:
            raise ValueError("browser_setup_result_invalid")
        title = "".join(char for char in title if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}).strip()
        origin = item["origin"]
        # Even malformed helper metadata must not put path/query/userinfo into Tk.
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(ord(char) <= 32 for char in origin)):
                origin = "(origin非公開)"
            else:
                host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
                origin = urlunsplit((parsed.scheme, host + (":" + str(parsed.port) if parsed.port else ""), "", "", ""))
        except ValueError:
            origin = "(origin非公開)"
        rows.append({"tab_id": item["tab_id"], "origin": origin, "title": title,
                     "type": "page", "selected": item["selected"]})
        seen.add(item["tab_id"])
    return rows


def launch_background(function):
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    return thread


class BrowserSetupController:
    """GUI-thread model; worker callbacks are applied only by ``poll``.

    ``browser_diagnostics`` is the client's cached getter and is the only API
    called during a poll. It may not probe Edge, spawn or connect a helper.
    """
    def __init__(self, *, launch=launch_background, purpose="general"):
        if purpose not in {"general", "media"}:
            raise ValueError("bridge_browser_purpose_invalid")
        self.purpose = purpose
        self._launch, self._events = launch, queue.Queue()
        self._generation = 0
        self.client = None
        self.busy = False
        self.targets = []
        self.profiles = []
        self.diagnostics = {}
        self.error = None

    def bind(self, client):
        self._generation += 1
        self.client = client
        self.busy = False
        self.targets = []
        self.profiles = []
        self.diagnostics = {}
        self.error = None

    @property
    def enabled(self):
        if self.client is None:
            return False
        return (self.client.browser_backend == "agent_browser" if self.purpose == "general"
                else getattr(self.client, "media_preparation_backend", None) == "managed_profile")

    def _request(self, action, params):
        if not self.enabled:
            self.error = "browser_setup_client_required"
            return False
        if self.busy or self.diagnostics.get("blocked") is True:
            return False
        client, generation = self.client, self._generation
        self.busy, self.error = True, None

        def work():
            try:
                result = client.browser_setup(action, params, purpose=self.purpose)
                event = (generation, action, result, None)
            except Exception as exc:
                event = (generation, action, None, safe_error(exc))
            self._events.put(event)

        self._launch(work)
        return True

    def discover(self):
        return self._request("discover_targets", {})

    def open_profile(self, account_id):
        if self.purpose != "media":
            self.error = "profile_driver_catalog_target_required"
            return False
        if not any(row["platform_account_id"] == account_id for row in self.profiles):
            self.error = "profile_driver_catalog_target_required"
            return False
        self.targets = []
        return self._request("open_profile", {"platform_account_id": account_id})

    def select(self, tab_id):
        if type(tab_id) is not int or not any(row["tab_id"] == tab_id for row in self.targets):
            self.error = "browser_setup_target_invalid"
            return False
        return self._request("select_target", {"tab_id": tab_id})

    def poll(self):
        while True:
            try:
                generation, action, result, error = self._events.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation:
                continue
            self.busy, self.error = False, error
            if error:
                continue
            try:
                if action in {"discover_targets", "open_profile"}:
                    self.targets = safe_targets(result)
                    if not self.targets:
                        self.error = "agent_browser_no_targets"
                elif (not isinstance(result, dict) or result.get("selected") is not True
                      or type(result.get("tab_id")) is not int
                      or not any(row["tab_id"] == result["tab_id"] for row in self.targets)):
                    raise ValueError("browser_setup_result_invalid")
                else:
                    for row in self.targets:
                        row["selected"] = row["tab_id"] == result["tab_id"]
            except ValueError as exc:
                self.error = safe_error(exc)
                self.targets = []
        if self.enabled:
            try:
                raw = self.client.browser_diagnostics(purpose=self.purpose)
                if self.purpose == "media":
                    from .managed_profile_backend import profile_catalog
                    self.profiles = profile_catalog(raw.get("profiles", []))
                self.diagnostics = {key: raw.get(key) is True for key in ("connected", "selected", "blocked")}
                self.diagnostics["error"] = safe_error(raw["error"]) if raw.get("error") else None
                count = raw.get("connect_attempts", 0)
                self.diagnostics["connect_attempts"] = count if type(count) is int and count >= 0 else 0
                verified = raw.get("last_verified_at")
                self.diagnostics["last_verified_at"] = (verified if type(verified) in (int, float)
                                                         and math.isfinite(verified) else None)
                pending = raw.get("permission_pending")
                self.diagnostics["permission_pending"] = pending if type(pending) is bool else None
                self.diagnostics["cached"] = True
                self.diagnostics["backend"] = (self.client.browser_backend if self.purpose == "general"
                                               else self.client.media_preparation_backend)
                self.diagnostics["current_connection_verified"] = False
                self.diagnostics["edge_policy"] = safe_policy_summary(raw.get("edge_policy"))
            except Exception:
                self.diagnostics = {"error": "browser_setup_failed"}
        return self.snapshot()

    def snapshot(self):
        usable = self.enabled and not self.busy and self.diagnostics.get("blocked") is not True
        return {"targets": deepcopy(self.targets), "profiles": deepcopy(self.profiles), "diagnostics": deepcopy(self.diagnostics),
                "open_enabled": usable and bool(self.profiles),
                "busy": self.busy, "discover_enabled": usable,
                "select_enabled": usable and bool(self.targets),
                "text": diagnostic_text(self.diagnostics, busy=self.busy, error=self.error)}
