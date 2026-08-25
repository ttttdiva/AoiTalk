#!/usr/bin/env python3
"""AoiTalk Playwright 経由で ChatGPT Web へ送受信する Director CLI。"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_HUMAN = 2
EXIT_BUSY = 3
EXIT_TIMEOUT = 4

REPO_ROOT = Path(__file__).resolve().parents[1]
REEXEC_ENV = "AOITALK_DIRECTOR_REEXEC"
SESSION_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")
CONVERSATION_URL_RE = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+")


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def grok_home() -> Path:
    raw = os.environ.get("GROK_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".grok"


def sessions_dir() -> Path:
    return grok_home() / "chatgpt-director" / "sessions"


def aoitalk_root() -> Path:
    raw = os.environ.get("AOITALK_ROOT", "").strip()
    return Path(raw).expanduser() if raw else REPO_ROOT


def default_profile_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "AoiTalk" / "chatgpt-web-profile"
    return Path.home() / ".aoitalk" / "chatgpt-web-profile"


def provider_python_candidates(root: Path) -> list[Path]:
    if sys.platform == "win32":
        return [
            root / "venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python.exe",
        ]
    return [
        root / "venv" / "bin" / "python",
        root / ".venv" / "bin" / "python",
    ]


def current_has_playwright() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


def interpreter_has_playwright(python: Path) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-c", "import playwright.async_api"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def find_aoitalk_python(root: Path) -> Path | None:
    for candidate in provider_python_candidates(root):
        if candidate.is_file() and interpreter_has_playwright(candidate):
            return candidate
    return None


def reexec_into_playwright() -> None:
    if current_has_playwright() or os.environ.get(REEXEC_ENV) == "1":
        return
    root = aoitalk_root()
    target = find_aoitalk_python(root)
    if target is None:
        emit(
            {
                "ok": False,
                "error": "playwright_missing",
                "message": (
                    "Playwright 付き Python が見つかりません。"
                    f" AOITALK_ROOT={root} の venv / .venv を確認してください。"
                ),
                "aoitalk_root": str(root),
                "python": sys.executable,
                "playwright": False,
            }
        )
        raise SystemExit(EXIT_ERROR)
    env = os.environ.copy()
    env[REEXEC_ENV] = "1"
    raise SystemExit(
        subprocess.call([str(target), str(Path(__file__).resolve()), *sys.argv[1:]], env=env)
    )


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def safe_session_key(raw: str | None) -> str:
    text = (raw or "").strip() or os.environ.get("GROK_SESSION_ID", "").strip() or "default"
    cleaned = SESSION_KEY_RE.sub("_", text).strip("._") or "default"
    return cleaned[:120]


def session_path(key: str) -> Path:
    return sessions_dir() / f"{key}.json"


def load_session(key: str) -> dict[str, Any]:
    path = session_path(key)
    if not path.is_file():
        return {"session": key, "conversation_url": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"session": key, "conversation_url": None}
    if not isinstance(data, dict):
        return {"session": key, "conversation_url": None}
    url = data.get("conversation_url")
    if not isinstance(url, str) or not CONVERSATION_URL_RE.match(url.strip()):
        url = None
    else:
        url = url.strip()
    data["session"] = key
    data["conversation_url"] = url
    return data


def save_session(key: str, conversation_url: str | None) -> Path:
    path = session_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": key,
        "conversation_url": conversation_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def clear_session(key: str) -> None:
    path = session_path(key)
    if path.is_file():
        path.unlink()


def classify_exception(exc: BaseException) -> tuple[str, int]:
    """Map provider failures to the CLI's stable machine-readable classes.

    Human escalation is intentionally type-driven.  Provider UI/Playwright
    failures may mention login or a human-check in their diagnostic text, but
    that text alone is not evidence that a user action is required.
    """

    exception_types = type(exc).__mro__
    name = type(exc).__name__
    message = str(exc)
    lowered = message.lower()
    if any(cls.__name__ == "ChatGPTWebUIInteractionError" for cls in exception_types):
        return "error", EXIT_ERROR
    if any(cls.__name__ == "ChatGPTWebNeedsHumanError" for cls in exception_types):
        return "needs_human", EXIT_NEEDS_HUMAN
    if name == "ChatGPTWebBusyError" or "使用中" in message:
        return "busy", EXIT_BUSY
    if "already in use" in lowered or "processsingleton" in lowered or "user data directory" in lowered:
        return "busy", EXIT_BUSY
    if name in {"TimeoutError", "asyncio.TimeoutError"} or "タイムアウト" in message or "timeout" in lowered:
        return "timeout", EXIT_TIMEOUT
    return "error", EXIT_ERROR


def load_provider_module(root: Path) -> Any:
    if not root.is_dir():
        raise FileNotFoundError(f"AOITALK_ROOT がありません: {root}")
    provider_path = root / "src" / "llm" / "chatgpt_web_provider.py"
    if not provider_path.is_file():
        raise FileNotFoundError(f"ChatGPTWebProvider が見つかりません: {provider_path}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if "src" not in sys.modules:
        src_pkg = types.ModuleType("src")
        src_pkg.__path__ = [str(root / "src")]
        sys.modules["src"] = src_pkg
    if "src.llm" not in sys.modules:
        llm_pkg = types.ModuleType("src.llm")
        llm_pkg.__path__ = [str(root / "src" / "llm")]
        sys.modules["src.llm"] = llm_pkg
    spec = importlib.util.spec_from_file_location(
        "src.llm.chatgpt_web_provider",
        provider_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"ChatGPTWebProvider を読めません: {provider_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.llm.chatgpt_web_provider"] = module
    spec.loader.exec_module(module)
    return module


def provider_config(profile_dir: Path) -> dict[str, Any]:
    timeout_raw = os.environ.get("CHATGPT_DIRECTOR_TIMEOUT", "").strip()
    timeout = int(timeout_raw) if timeout_raw.isdigit() else 900
    return {
        "chatgpt_web": {
            "profile_dir": str(profile_dir),
            "response_timeout_seconds": max(1, timeout),
        }
    }


def read_message(file_path: str | None) -> str:
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    return sys.stdin.read().strip()


def resolve_send_url(args: argparse.Namespace, key: str) -> str | None:
    if args.new:
        return None
    if args.url:
        url = args.url.strip()
        if not CONVERSATION_URL_RE.match(url):
            raise ValueError("ChatGPT会話URLは https://chatgpt.com/c/<id> 形式で指定してください。")
        return url
    saved = load_session(key).get("conversation_url")
    return saved if isinstance(saved, str) else None


async def wait_conversation_url(provider: Any) -> str | None:
    url = provider.current_conversation_url()
    if url:
        return url
    for _ in range(10):
        await asyncio.sleep(0.5)
        url = provider.current_conversation_url()
        if url:
            return url
    return None


async def cmd_status() -> int:
    root = aoitalk_root()
    profile_dir = default_profile_dir()
    payload: dict[str, Any] = {
        "ok": False,
        "command": "status",
        "logged_in": False,
        "needs_human": False,
        "busy": False,
        "profile_dir": str(profile_dir),
        "profile_exists": profile_dir.is_dir(),
        "aoitalk_root": str(root),
        "python": sys.executable,
        "playwright": current_has_playwright(),
        "conversation_url": None,
    }
    try:
        module = load_provider_module(root)
        provider = module.ChatGPTWebProvider(provider_config(profile_dir))
        payload["profile_dir"] = str(provider.profile_dir)
        payload["profile_exists"] = provider.profile_dir.is_dir()
        logged_in = await provider.check_login()
        payload["logged_in"] = bool(logged_in)
        payload["ok"] = bool(logged_in)
        if logged_in:
            return EXIT_OK
        payload["needs_human"] = True
        payload["error"] = "needs_human"
        payload["message"] = (
            "ChatGPT にログインしていません。"
            "AoiTalk の ChatGPT 設定ブラウザ（同じプロファイル）でログインしてから再試行してください。"
        )
        return EXIT_NEEDS_HUMAN
    except BaseException as exc:
        kind, code = classify_exception(exc)
        payload["error"] = kind
        payload["message"] = str(exc)
        payload["needs_human"] = kind == "needs_human"
        payload["busy"] = kind == "busy"
        return code
    finally:
        emit(payload)


async def cmd_send(args: argparse.Namespace) -> int:
    key = safe_session_key(args.session)
    root = aoitalk_root()
    profile_dir = default_profile_dir()
    payload: dict[str, Any] = {
        "ok": False,
        "command": "send",
        "session": key,
        "conversation_url": None,
        "response": None,
        "profile_dir": str(profile_dir),
        "aoitalk_root": str(root),
        "python": sys.executable,
    }
    try:
        message = read_message(args.file)
        if not message:
            payload["error"] = "empty_message"
            payload["message"] = "送信文が空です。--file または stdin で渡してください。"
            return EXIT_ERROR
        url = resolve_send_url(args, key)
        module = load_provider_module(root)
        provider = module.ChatGPTWebProvider(provider_config(profile_dir))
        payload["profile_dir"] = str(provider.profile_dir)
        async with provider.operation("director"):
            await provider.open_conversation(url)
            response = await provider.send(message)
            conversation_url = await wait_conversation_url(provider)
        save_session(key, conversation_url)
        payload["ok"] = True
        payload["conversation_url"] = conversation_url
        payload["response"] = response
        return EXIT_OK
    except BaseException as exc:
        kind, code = classify_exception(exc)
        payload["error"] = kind
        payload["message"] = str(exc)
        payload["needs_human"] = kind == "needs_human"
        payload["busy"] = kind == "busy"
        return code
    finally:
        emit(payload)


def cmd_new(args: argparse.Namespace) -> int:
    key = safe_session_key(args.session)
    clear_session(key)
    emit(
        {
            "ok": True,
            "command": "new",
            "session": key,
            "conversation_url": None,
            "message": "保存済み会話URLを破棄しました。次の send は新しい ChatGPT 会話を開始します。",
        }
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatgpt_web_director.py",
        description=(
            "AoiTalk Playwright ChatGPT Web 経由の Director CLI。"
            " Cursor Browser / ChatGPT API / 独自 Playwright は使わない。"
        ),
    )
    parser.epilog = (
        "終了コード: 0 成功（status はログイン済み）, "
        "2 未ログイン/人手必要, 3 プロファイル使用中, 4 タイムアウト, 1 その他エラー"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="ログイン/プロファイル/Playwright 状態を JSON で出す（送信しない）")

    send = sub.add_parser("send", help="メッセージを送り、完了した返信を待つ")
    send.add_argument(
        "--session",
        default=None,
        help="セッションキー。省略時は GROK_SESSION_ID、それも無ければ default",
    )
    send.add_argument("--url", default=None, help="既存 ChatGPT 会話 URL（https://chatgpt.com/c/<id>）")
    send.add_argument("--new", action="store_true", help="保存URLを無視して新しい会話を開始する")
    send.add_argument("--file", default=None, help="送信文ファイル。省略時は stdin")

    new = sub.add_parser("new", help="保存済み会話URLを捨てる（次の send が新規会話）")
    new.add_argument(
        "--session",
        default=None,
        help="セッションキー。省略時は GROK_SESSION_ID、それも無ければ default",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"status", "send"}:
        reexec_into_playwright()
        if args.command == "status":
            return asyncio.run(cmd_status())
        return asyncio.run(cmd_send(args))
    if args.command == "new":
        return cmd_new(args)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
