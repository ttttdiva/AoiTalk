"""Playwright で Web 版 ChatGPT を人間と同じ UI 操作だけで利用する。

このプロバイダーは Director 専用であり、通常の LLM provider factory には
登録しない。Playwright は既存環境の import を壊さないよう遅延 import する。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, AsyncIterator, Iterable

from .chatgpt_web_selectors import (
    ASSISTANT_MESSAGE_SELECTOR,
    ATTACH_BUTTON_NAMES,
    BLOCKING_MODAL_FALLBACK_SELECTORS,
    BLOCKING_MODAL_DISMISS_NAMES,
    BLOCKING_MODAL_SELECTORS,
    BLOCKING_MODAL_TEXTS,
    CHALLENGE_SELECTORS,
    CHALLENGE_TITLE_TEXTS,
    CHALLENGE_TEXTS,
    COMPOSER_SELECTORS,
    LOGIN_LINK_NAMES,
    SEND_BUTTON_NAMES,
    SEND_BUTTON_SELECTORS,
    STOP_BUTTON_NAMES,
    STOP_BUTTON_SELECTORS,
    UPLOAD_MENU_NAMES,
)
from ..security.browser_scope import (
    _DIRECTOR_SCOPE_CAPABILITY,
    _agent_team_role_bound,
    create_director_browser_scope,
)
from ..utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)

CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_CONVERSATION_RE = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+")
RESPONSE_STABLE_SECONDS = 2.0
RESPONSE_STABLE_WITHOUT_STOP_SECONDS = 15.0
LOGIN_STATE_TIMEOUT_SECONDS = 20.0
LOGIN_STATE_POLL_SECONDS = 0.25
LOGIN_STATE_STABLE_SECONDS = 1.0
PROFILE_LOCK_POLL_SECONDS = 0.05
BROWSER_CLOSE_TIMEOUT_SECONDS = 10.0


class ChatGPTWebError(RuntimeError):
    """ChatGPT Web 接続の基底例外。"""


class ChatGPTWebBusyError(ChatGPTWebError):
    """専用ブラウザプロファイルが別操作で使用中。"""


class ChatGPTWebNeedsHumanError(ChatGPTWebError):
    """ログイン・チャレンジ等、人手が必要な状態。"""


class ChatGPTWebUIInteractionError(ChatGPTWebError):
    """ChatGPT Web の DOM / Playwright 操作に失敗した状態。"""


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if hasattr(config, "get"):
        return config.get(key, default)
    if isinstance(config, dict):
        current: Any = config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def resolve_chatgpt_profile_dir(config: Any) -> Path:
    raw = str(_config_get(config, "chatgpt_web.profile_dir", "") or "").strip()
    if not raw:
        local = os.environ.get("LOCALAPPDATA")
        return (
            Path(local) / "AoiTalk" / "chatgpt-web-profile"
            if local
            else Path.home() / ".aoitalk" / "chatgpt-web-profile"
        )
    expanded = os.path.expandvars(os.path.expanduser(raw))
    # Windows 以外では %LOCALAPPDATA% が未展開のまま残り得る。
    if "%" in expanded and raw.startswith("%LOCALAPPDATA%"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            expanded = raw.replace("%LOCALAPPDATA%", local, 1)
    return Path(expanded).resolve()


def resolve_system_chrome() -> Path | None:
    """手動ログインに使う通常のChromeを探す。"""

    candidates: list[Path] = []
    if sys.platform == "win32":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        )

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    for command in (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ):
        executable = shutil.which(command)
        if executable:
            return Path(executable)
    return None


def _director_launch_options() -> dict[str, Any]:
    """Director用ブラウザの露骨な自動化識別を減らす起動設定。"""

    options: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1440, "height": 1000},
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled"],
        # Playwright otherwise inherits the full AoiTalk process environment,
        # including database/API credentials and browser session tokens.  A
        # Director browser only needs the small generic runtime allowlist.
        "env": build_aoitalk_subprocess_env(),
    }
    chrome = resolve_system_chrome()
    if chrome is not None:
        options["executable_path"] = str(chrome)
    return options


async def _start_playwright() -> Any:
    """Playwrightを遅延importして起動する。"""

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ChatGPTWebError(
            "Playwright がインストールされていません。"
            "`pip install -e .` と `playwright install chromium` を実行してください。"
        ) from exc
    return await async_playwright().start()


class _ProfileCoordinator:
    def __init__(self) -> None:
        # 接続確認はWebサーバー側、会話はmain側のevent loopで動くため、
        # process-globalな排他にasyncio.Lockを使わない。
        self._guard = threading.Lock()
        self._director_lock = threading.Lock()
        self.mode: str | None = None
        self.profile_dir: str | None = None

    async def acquire(self, mode: str, profile_dir: Path) -> None:
        # Agent Team children are leaf workers.  They must never acquire the
        # ChatGPT profile, whether they ask for the Director operation or the
        # settings/login browser.  The role is request-scoped and checked
        # here, at the process-global lock boundary, rather than relying on a
        # prompt or on callers choosing a safe mode string.
        if _agent_team_role_bound():
            raise ChatGPTWebBusyError(
                "Agent Team workers cannot access the Director browser or profile."
            )
        if mode not in {"director", "settings"}:
            raise ChatGPTWebBusyError(f"unsupported ChatGPT browser mode: {mode!r}")
        if mode == "director":
            with self._guard:
                if self.mode == "settings":
                    raise ChatGPTWebBusyError(
                        "ChatGPT専用プロファイルは設定ブラウザが使用中です。"
                        "先にそのウィンドウを閉じてください。"
                    )
            # 複数セッションのDirector送信は拒否せず、順番に実行する。
            while not self._director_lock.acquire(blocking=False):
                await asyncio.sleep(PROFILE_LOCK_POLL_SECONDS)
            try:
                with self._guard:
                    if self.mode == "settings":
                        raise ChatGPTWebBusyError(
                            "ChatGPT専用プロファイルは設定ブラウザが使用中です。"
                        )
                    self.mode = "director"
                    self.profile_dir = str(profile_dir)
            except BaseException:
                self._director_lock.release()
                raise
            return
        with self._guard:
            if self.mode is not None or self._director_lock.locked():
                label = "設定ブラウザ" if self.mode == "settings" else "Director"
                raise ChatGPTWebBusyError(
                    f"ChatGPT専用プロファイルは{label}が使用中です。"
                    "先にその操作を終了してください。"
                )
            self.mode = mode
            self.profile_dir = str(profile_dir)

    async def release(self, mode: str) -> None:
        release_director = False
        with self._guard:
            if self.mode == mode:
                self.mode = None
            if (
                mode == "director"
                and self._director_lock.locked()
            ):
                release_director = True
        if release_director:
            self._director_lock.release()

    def status(self) -> dict[str, Any]:
        with self._guard:
            return {
                "busy": self.mode is not None,
                "mode": self.mode,
                "settings_browser_open": self.mode == "settings",
                "director_running": self.mode == "director",
                "profile_dir": self.profile_dir,
            }


_PROFILE_COORDINATOR = _ProfileCoordinator()
_settings_browser_watch_task: asyncio.Task[Any] | None = None
_settings_browser_process: asyncio.subprocess.Process | None = None


def chatgpt_web_status(config: Any | None = None) -> dict[str, Any]:
    status = _PROFILE_COORDINATOR.status()
    # 既存APIレスポンスとの互換性を保つ。ブラウザはDirector操作中だけ開く。
    status["director_browser_open"] = status["director_running"]
    if config is not None:
        status["profile_dir"] = str(resolve_chatgpt_profile_dir(config))
    status["playwright_available"] = _playwright_available()
    return status


def _playwright_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("playwright.async_api") is not None
    except (ImportError, ValueError):
        return False


def _name_pattern(names: Iterable[str]) -> re.Pattern[str]:
    return re.compile(
        "^(?:" + "|".join(re.escape(name) for name in names) + ")$",
        re.IGNORECASE,
    )


def _is_playwright_timeout(exc: BaseException) -> bool:
    """Playwrightのtimeoutだけを添付メニューfallbackの合図にする。"""

    return isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
        type(exc).__name__ == "TimeoutError"
    )


def _normalize_ui_text(value: Any) -> str:
    """UI本文の空白差分を吸収して比較する。"""

    # 日本語の見出しは折り返しで文字間に改行・空白が入ることがあるため、
    # 空白をすべて除去してから比較する。
    return "".join(str(value or "").split()).casefold()


def _contains_blocking_modal_text(value: Any) -> bool:
    normalized = _normalize_ui_text(value)
    return any(
        _normalize_ui_text(marker) in normalized
        for marker in BLOCKING_MODAL_TEXTS
    )


def _contains_human_action_text(value: Any) -> bool:
    normalized = _normalize_ui_text(value)
    return any(
        _normalize_ui_text(marker) in normalized
        for marker in (*CHALLENGE_TEXTS, *LOGIN_LINK_NAMES)
    )


def _response_is_complete(
    *,
    saw_generation: bool,
    stop_visible: bool,
    response_changed: bool,
    stable_seconds: float,
) -> bool:
    if stop_visible:
        return False
    if saw_generation:
        return stable_seconds >= RESPONSE_STABLE_SECONDS
    # 停止ボタンを一度も捕捉できなかった時は、新しい回答が十分長く
    # 静止した場合だけfallbackする。長い会話ではDOMが仮想化され、
    # 新規回答でもメッセージ件数が増えないため、件数だけには依存しない。
    return response_changed and stable_seconds >= RESPONSE_STABLE_WITHOUT_STOP_SECONDS


def _response_changed(
    *,
    previous_count: int,
    previous_text: str,
    previous_message_id: str | None,
    current_count: int,
    current_text: str,
    current_message_id: str | None,
) -> bool:
    # 両方の回答IDを取得できる現行UIでは、本文の遅延描画や仮想化による
    # 既存回答の入れ替わりを新規回答と誤認しないようID変更を必須にする。
    if previous_message_id is not None and current_message_id is not None:
        return current_message_id != previous_message_id
    # IDがない旧UIだけ、従来どおり件数と本文差分へfallbackする。
    return current_count > previous_count or (
        bool(current_text) and current_text != previous_text
    )


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _finish_browser_cleanup(
    awaitable: Any,
    *,
    label: str,
) -> asyncio.CancelledError | None:
    task = asyncio.create_task(awaitable)
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=BROWSER_CLOSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "%sが%.1f秒以内に完了しなかったため処理を続行します",
            label,
            BROWSER_CLOSE_TIMEOUT_SECONDS,
        )
        task.cancel()
        task.add_done_callback(_consume_task_result)
    except asyncio.CancelledError as exc:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return exc
    except Exception:
        logger.debug("%sに失敗しました", label, exc_info=True)
    return None


class ChatGPTWebProvider:
    """1回のDirector操作中だけブラウザコンテキストを所有する。"""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.profile_dir = resolve_chatgpt_profile_dir(config)
        self.response_timeout_seconds = max(
            1,
            int(
                _config_get(
                    config, "chatgpt_web.response_timeout_seconds", 900
                )
                or 900
            ),
        )
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._operation_mode: str | None = None
        self._browser_scope: Any = None

    @asynccontextmanager
    async def operation(self, mode: str = "director") -> AsyncIterator["ChatGPTWebProvider"]:
        await _PROFILE_COORDINATOR.acquire(mode, self.profile_dir)
        self._operation_mode = mode
        self._browser_scope = create_director_browser_scope(
            capability=_DIRECTOR_SCOPE_CAPABILITY,
            principal="parent",
        )
        try:
            await self._launch()
            yield self
        finally:
            try:
                await self.close()
            finally:
                if self._browser_scope is not None:
                    self._browser_scope.close("Director operation complete")
                    self._browser_scope = None
                await _PROFILE_COORDINATOR.release(mode)
                self._operation_mode = None

    async def _launch(self) -> None:
        if self._context is not None:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await _start_playwright()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                **_director_launch_options(),
            )
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
        except BaseException:
            playwright = self._playwright
            self._playwright = None
            if playwright is not None:
                await _finish_browser_cleanup(
                    playwright.stop(),
                    label="起動失敗後のPlaywright終了",
                )
            raise

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        cancelled: asyncio.CancelledError | None = None
        if context is not None:
            cancelled = await _finish_browser_cleanup(
                context.close(),
                label="ChatGPTブラウザコンテキストの終了",
            )
        if playwright is not None:
            playwright_cancelled = await _finish_browser_cleanup(
                playwright.stop(),
                label="Playwrightの終了",
            )
            cancelled = cancelled or playwright_cancelled
        if cancelled is not None:
            raise cancelled

    def _require_page(self) -> Any:
        if self._page is None:
            raise ChatGPTWebError(
                "ChatGPT Web 操作が開始されていません。operation() 内で呼び出してください。"
            )
        return self._page

    async def open_conversation(self, url: str | None) -> None:
        page = self._require_page()
        target = str(url or CHATGPT_HOME_URL).strip()
        if self._browser_scope is not None:
            self._browser_scope.assert_navigation_allowed(target)
        if url and not CHATGPT_CONVERSATION_RE.match(target):
            raise ValueError("ChatGPT会話URLは https://chatgpt.com/c/<id> 形式で指定してください。")
        await page.goto(target, wait_until="domcontentloaded")
        await self._raise_if_challenge()
        await self._dismiss_blocking_modals()

    def current_conversation_url(self) -> str | None:
        if self._page is None:
            return None
        url = str(self._page.url or "")
        match = CHATGPT_CONVERSATION_RE.match(url)
        return match.group(0) if match else None

    async def check_login(self) -> bool:
        if self._page is not None:
            return await self._check_login_on_page()
        async with self.operation("director"):
            await self.open_conversation(None)
            return await self._check_login_on_page()

    async def _check_login_on_page(self) -> bool:
        page = self._require_page()
        deadline = time.monotonic() + LOGIN_STATE_TIMEOUT_SECONDS
        login_visible_since: float | None = None
        while True:
            await self._raise_if_challenge()
            composer = await self._first_visible_css(COMPOSER_SELECTORS)
            if composer is not None:
                return True
            login = page.get_by_role("link", name=_name_pattern(LOGIN_LINK_NAMES))
            login_visible = (
                bool(await login.count())
                and await login.first.is_visible()
            )
            now = time.monotonic()
            if login_visible:
                if login_visible_since is None:
                    login_visible_since = now
                elif now - login_visible_since >= LOGIN_STATE_STABLE_SECONDS:
                    return False
            else:
                login_visible_since = None
            if now >= deadline:
                raise ChatGPTWebError(
                    "ChatGPT画面の読み込み状態を確認できませんでした。"
                    "ネットワークまたはChatGPT側の画面を確認してください。"
                )
            await asyncio.sleep(LOGIN_STATE_POLL_SECONDS)

    async def send(
        self,
        text: str,
        files: list[Path] | None = None,
    ) -> str:
        page = self._require_page()
        if not await self._check_login_on_page():
            raise ChatGPTWebNeedsHumanError(
                "ChatGPTにログインしていません。設定ブラウザからログインしてください。"
            )
        try:
            composer = await self._first_visible_css(COMPOSER_SELECTORS)
        except ChatGPTWebUIInteractionError:
            raise
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの入力欄を確認できません。"
            ) from exc
        if composer is None:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの入力欄を確認できません。"
            )

        try:
            assistant_messages = page.locator(ASSISTANT_MESSAGE_SELECTOR)
            previous_count = await assistant_messages.count()
            previous_text = (
                (await assistant_messages.last.inner_text()).strip()
                if previous_count
                else ""
            )
            previous_message_id = (
                await assistant_messages.last.get_attribute("data-message-id")
                if previous_count
                else None
            )
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの応答表示を確認できません。"
            ) from exc

        await self._dismiss_blocking_modals()
        try:
            await composer.focus()
        except Exception:
            try:
                await self._dismiss_blocking_modals()
                try:
                    await composer.click(force=True)
                except TypeError:
                    # 小さなテストダブルや古いPlaywright互換locatorは
                    # force引数を受け取らないことがある。
                    await composer.click()
            except ChatGPTWebUIInteractionError:
                raise
            except Exception as exc:
                raise ChatGPTWebUIInteractionError(
                    "ChatGPTの入力欄を操作できません。"
                ) from exc
        try:
            await page.keyboard.insert_text(str(text or ""))
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの入力内容を入力できません。"
            ) from exc
        if files:
            await self._attach_files([Path(item) for item in files])

        # ポップアップは入力や添付の後にも非同期で出現するため、送信ボタンを
        # 解決する直前にもう一度だけ確認する。ここで閉じた後に送信ボタンを
        # 再解決するが、送信クリック自体は曖昧な状態で再試行しない。
        await self._dismiss_blocking_modals()
        try:
            send_button = await self._first_visible_css(SEND_BUTTON_SELECTORS)
            if send_button is None:
                named_button = page.get_by_role(
                    "button",
                    name=_name_pattern(SEND_BUTTON_NAMES),
                )
                await named_button.first.wait_for(
                    state="visible",
                    timeout=10_000,
                )
                send_button = named_button.first
            await send_button.click()
        except ChatGPTWebUIInteractionError:
            raise
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの送信ボタンを操作できません。"
            ) from exc

        return await self._wait_for_response(
            previous_count=previous_count,
            previous_text=previous_text,
            previous_message_id=previous_message_id,
        )

    async def _attach_files(self, files: list[Path]) -> None:
        page = self._require_page()
        missing = [str(path) for path in files if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"添付ファイルが見つかりません: {', '.join(missing)}")

        try:
            attach = page.get_by_role(
                "button",
                name=_name_pattern(ATTACH_BUTTON_NAMES),
            )
            if not await attach.count():
                raise ChatGPTWebUIInteractionError(
                    "ChatGPTの添付ボタンを確認できません。"
                )
        except ChatGPTWebUIInteractionError:
            raise
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの添付ボタンを確認できません。"
            ) from exc
        direct_chooser: asyncio.Task[Any] | None = None
        try:
            direct_chooser = asyncio.create_task(
                page.wait_for_event("filechooser", timeout=1_500)
            )
            await attach.first.click()
            try:
                chooser = await direct_chooser
            except Exception as direct_exc:
                # 添付ボタンがメニューを開くUIでは、最初のfilechooser待機が
                # timeoutした後にメニュー項目を選ぶ。その他のPlaywright障害は
                # 後段で曖昧に握り潰さずUI操作エラーとして返す。
                if not _is_playwright_timeout(direct_exc):
                    raise
                upload = page.get_by_role(
                    "menuitem", name=_name_pattern(UPLOAD_MENU_NAMES)
                )
                async with page.expect_file_chooser(
                    timeout=10_000
                ) as chooser_info:
                    if not await upload.count() or not await upload.first.is_visible():
                        raise ChatGPTWebUIInteractionError(
                            "ChatGPTのアップロード項目を確認できません。"
                        )
                    await upload.first.click()
                chooser = await chooser_info.value
            await chooser.set_files([str(path.resolve()) for path in files])
        except ChatGPTWebUIInteractionError:
            raise
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTの画面上の添付操作を完了できませんでした。"
            ) from exc
        finally:
            if direct_chooser is not None and not direct_chooser.done():
                direct_chooser.cancel()
                await asyncio.gather(direct_chooser, return_exceptions=True)

    async def _wait_for_response(
        self,
        *,
        previous_count: int,
        previous_text: str,
        previous_message_id: str | None,
    ) -> str:
        page = self._require_page()
        deadline = time.monotonic() + self.response_timeout_seconds
        stable_since: float | None = None
        last_text = ""
        saw_generation = False

        while time.monotonic() < deadline:
            await self._dismiss_blocking_modals()
            await self._raise_if_human_action_needed()
            stop_visible = await self._stop_button_visible()
            if stop_visible:
                saw_generation = True
                stable_since = None

            messages = page.locator(ASSISTANT_MESSAGE_SELECTOR)
            count = await messages.count()
            current = (
                (await messages.last.inner_text()).strip()
                if count
                else ""
            )
            current_message_id = (
                await messages.last.get_attribute("data-message-id") if count else None
            )
            response_changed = _response_changed(
                previous_count=previous_count,
                previous_text=previous_text,
                previous_message_id=previous_message_id,
                current_count=count,
                current_text=current,
                current_message_id=current_message_id,
            )
            if response_changed and current:
                if current != last_text:
                    last_text = current
                    stable_since = None if stop_visible else time.monotonic()
                if not stop_visible and stable_since is None:
                    # 本文が先に完成し、次のpollで停止ボタンだけが消える
                    # 状態遷移でも、ここから安定時間を測り始める。
                    stable_since = time.monotonic()
                stable_seconds = (
                    time.monotonic() - stable_since
                    if stable_since is not None
                    else 0.0
                )
                if _response_is_complete(
                    saw_generation=saw_generation,
                    stop_visible=stop_visible,
                    response_changed=response_changed,
                    stable_seconds=stable_seconds,
                ):
                    return current
            await asyncio.sleep(0.5)

        state = "生成開始後" if saw_generation else "生成開始を確認できないまま"
        raise ChatGPTWebError(
            f"ChatGPTの回答待機がタイムアウトしました（{state}、"
            f"{self.response_timeout_seconds}秒）。"
        )

    async def _stop_button_visible(self) -> bool:
        if await self._first_visible_css(STOP_BUTTON_SELECTORS) is not None:
            return True
        page = self._require_page()
        stop = page.get_by_role("button", name=_name_pattern(STOP_BUTTON_NAMES))
        return bool(await stop.count() and await stop.first.is_visible())

    async def _first_visible_css(self, selectors: Iterable[str]) -> Any | None:
        page = self._require_page()
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = await locator.count()
                for index in range(count):
                    candidate = locator.first if index == 0 else locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except ChatGPTWebUIInteractionError:
                raise
            except Exception:
                continue
        return None

    async def _dismiss_blocking_modals(self) -> bool:
        """既知の自動dismiss可能なモーダルを最大1つ閉じる。

        戻り値は対象を検出して閉じた場合だけ ``True``、対象がなければ
        ``False``。対象を認識した後にボタン取得・クリック・消滅確認へ失敗
        した場合は、人間介入では解決しないUI操作エラーを返す。
        """

        modal = await self._find_blocking_modal()
        if modal is None:
            return False

        try:
            dismiss = modal.get_by_role(
                "button",
                name=_name_pattern(BLOCKING_MODAL_DISMISS_NAMES),
            )
            count = await dismiss.count()
            for index in range(count):
                candidate = dismiss.first if index == 0 else dismiss.nth(index)
                if await candidate.is_visible():
                    try:
                        await candidate.click()
                    except Exception as exc:
                        raise ChatGPTWebUIInteractionError(
                            "ChatGPTのレート制限ポップアップを閉じられません。"
                        ) from exc
                    # click後の再描画を待つが、別のモーダルを同じ呼び出しで
                    # 連続クリックしない（最大1 dismiss/call）。
                    await asyncio.sleep(0.1)
                    try:
                        # 再確認は今回クリックした対象だけに限定する。
                        # 別のblocking modalが残っていても、次の呼び出しで
                        # 1つずつ処理できるようにする。
                        if await modal.is_visible():
                            raise ChatGPTWebUIInteractionError(
                                "ChatGPTのレート制限ポップアップが閉じません。"
                            )
                    except ChatGPTWebUIInteractionError:
                        raise
                    except Exception as exc:
                        raise ChatGPTWebUIInteractionError(
                            "ChatGPTのレート制限ポップアップの状態を確認できません。"
                        ) from exc
                    return True
        except ChatGPTWebUIInteractionError:
            raise
        except Exception as exc:
            raise ChatGPTWebUIInteractionError(
                "ChatGPTのレート制限ポップアップを操作できません。"
            ) from exc

        raise ChatGPTWebUIInteractionError(
            "ChatGPTのレート制限ポップアップの了解ボタンを確認できません。"
        )

    async def _find_blocking_modal(self) -> Any | None:
        """表示中の旧固定モーダルまたは本文一致のdialogを返す。"""

        page = self._require_page()
        try:
            lowered_url = str(page.url or "").lower()
        except Exception:
            lowered_url = ""
        if any(part in lowered_url for part in ("/auth/", "challenge", "captcha")):
            # 人間確認・ログイン画面は自動dismiss対象にしない。
            return None

        # 旧DOMは本文がなくても既知のrate-limitモーダルとして扱う。
        for selector in BLOCKING_MODAL_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    candidate = locator.first if index == 0 else locator.nth(index)
                    try:
                        visible = await candidate.is_visible()
                    except AttributeError:
                        # 非modal locatorを返す軽量なテストダブル／旧UIは
                        # modal visibility APIを持たないため次の候補へ進む。
                        continue
                    except Exception as exc:
                        raise ChatGPTWebUIInteractionError(
                            "ChatGPTのレート制限ポップアップの状態を確認できません。"
                        ) from exc
                    if visible:
                        try:
                            text = await candidate.inner_text()
                        except (AttributeError, TypeError):
                            text = ""
                        except Exception as exc:
                            raise ChatGPTWebUIInteractionError(
                                "ChatGPTのレート制限ポップアップ本文を確認できません。"
                            ) from exc
                        if _contains_human_action_text(text):
                            continue
                        return candidate
            except ChatGPTWebUIInteractionError:
                raise
            except Exception:
                # Playwrightのlocatorは次のselectorで回復できることがある。
                # すべてが失敗して対象を判定できない場合はno-targetとして
                # 扱い、後段の必須UI操作でUIInteractionErrorにする。
                continue

        # 現行DOMは固定testid/idを持たないため、表示中のdialog/modal本文を
        # 確認してからのみ、その内部のdismissボタンを操作する。
        for selector in BLOCKING_MODAL_FALLBACK_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    candidate = locator.first if index == 0 else locator.nth(index)
                    try:
                        visible = await candidate.is_visible()
                    except AttributeError:
                        continue
                    except Exception as exc:
                        raise ChatGPTWebUIInteractionError(
                            "ChatGPTのblocking dialogの状態を確認できません。"
                        ) from exc
                    if not visible:
                        continue
                    try:
                        text = await candidate.inner_text()
                    except Exception:
                        try:
                            text = await candidate.text_content()
                        except Exception as exc:
                            raise ChatGPTWebUIInteractionError(
                                "ChatGPTのblocking dialog本文を確認できません。"
                            ) from exc
                    if (
                        not _contains_human_action_text(text)
                        and _contains_blocking_modal_text(text)
                    ):
                        return candidate
            except ChatGPTWebUIInteractionError:
                raise
            except Exception:
                continue
        return None

    async def _blocking_modal_visible(self) -> bool:
        return await self._find_blocking_modal() is not None

    async def _raise_if_challenge(self) -> None:
        page = self._require_page()
        url = str(page.url or "")
        lowered_url = url.lower()
        if any(
            part in lowered_url
            for part in ("/auth/", "challenge", "captcha")
        ):
            raise ChatGPTWebNeedsHumanError(
                "ChatGPTのログインまたは確認画面が表示されています。"
            )
        if await self._first_visible_css(CHALLENGE_SELECTORS) is not None:
            raise ChatGPTWebNeedsHumanError(
                "ChatGPTの人間確認画面が表示されています。"
                "設定ブラウザで確認を完了してください。"
            )
        if await self._first_visible_css(COMPOSER_SELECTORS) is not None:
            return
        title = str(await page.title() or "")
        conversation_open = bool(CHATGPT_CONVERSATION_RE.match(url))
        normalized_title = title.casefold().strip().rstrip(".…")
        if not conversation_open and any(
            normalized_title == marker.casefold()
            for marker in CHALLENGE_TITLE_TEXTS
        ):
            raise ChatGPTWebNeedsHumanError(
                "ChatGPTの人間確認画面が表示されています。"
                "設定ブラウザで確認を完了してください。"
            )
        if conversation_open:
            return
        for text in CHALLENGE_TEXTS:
            locator = page.get_by_text(text, exact=False)
            try:
                if await locator.count() and await locator.first.is_visible():
                    raise ChatGPTWebNeedsHumanError(
                        "ChatGPTの人間確認画面が表示されています。"
                    )
            except ChatGPTWebNeedsHumanError:
                raise
            except Exception:
                continue

    async def _raise_if_human_action_needed(self) -> None:
        await self._raise_if_challenge()
        page = self._require_page()
        login = page.get_by_role("link", name=_name_pattern(LOGIN_LINK_NAMES))
        if await login.count() and await login.first.is_visible():
            raise ChatGPTWebNeedsHumanError(
                "ChatGPTにログインしていません。"
            )


async def open_chatgpt_settings_browser(config: Any) -> dict[str, Any]:
    """通常のChromeを開き、ユーザーが閉じるまでプロファイルを占有する。"""

    global _settings_browser_process, _settings_browser_watch_task

    profile_dir = resolve_chatgpt_profile_dir(config)
    chrome = resolve_system_chrome()
    if chrome is None:
        raise ChatGPTWebError(
            "手動ログイン用のGoogle Chromeが見つかりません。"
            "Google Chromeをインストールしてください。"
        )

    await _PROFILE_COORDINATOR.acquire("settings", profile_dir)
    process: asyncio.subprocess.Process | None = None
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32"
            else 0
        )
        process = await asyncio.create_subprocess_exec(
            str(chrome),
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--disable-background-mode",
            "--new-window",
            CHATGPT_HOME_URL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # Keep the manually opened settings browser behind the same
            # process-environment boundary as the Playwright Director.
            env=build_aoitalk_subprocess_env(),
            creationflags=creationflags,
        )
        await asyncio.sleep(0.25)
        if process.returncode is not None:
            raise ChatGPTWebError(
                f"Google Chromeの起動に失敗しました（終了コード: {process.returncode}）。"
            )
    except BaseException:
        await _terminate_browser_process(process)
        await _PROFILE_COORDINATOR.release("settings")
        raise

    assert process is not None
    _settings_browser_process = process

    async def _watch_close() -> None:
        global _settings_browser_process, _settings_browser_watch_task
        try:
            await process.wait()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            await _PROFILE_COORDINATOR.release("settings")
            _settings_browser_process = None
            _settings_browser_watch_task = None

    _settings_browser_watch_task = asyncio.create_task(_watch_close())
    return chatgpt_web_status(config)


async def _terminate_browser_process(
    process: asyncio.subprocess.Process | None,
) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)
    except ProcessLookupError:
        return
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def close_chatgpt_web_resources() -> None:
    """サーバー終了時に設定ブラウザのリソースを解放する。"""

    global _settings_browser_process, _settings_browser_watch_task
    process = _settings_browser_process
    await _terminate_browser_process(process)
    task = _settings_browser_watch_task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await _PROFILE_COORDINATOR.release("settings")
    _settings_browser_process = None
    _settings_browser_watch_task = None
