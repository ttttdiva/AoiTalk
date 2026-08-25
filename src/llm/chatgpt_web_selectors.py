"""ChatGPT Web UI のロケーター定義。

UI 変更時の修正箇所をこのモジュールへ集約する。内部 API や座標クリックには
依存せず、表示 DOM の role / aria / data 属性だけを使う。
"""

from __future__ import annotations

COMPOSER_SELECTORS = (
    "#prompt-textarea",
    "main [contenteditable='true'][role='textbox']",
    "textarea[placeholder*='Message']",
    "textarea[placeholder*='メッセージ']",
)

ASSISTANT_MESSAGE_SELECTOR = "main [data-message-author-role='assistant']"

SEND_BUTTON_SELECTORS = (
    "button[data-testid='send-button']",
    "button#composer-submit-button",
)

SEND_BUTTON_NAMES = (
    "Send prompt",
    "Send message",
    "送信",
    "プロンプトを送信する",
)

STOP_BUTTON_NAMES = (
    "Stop streaming",
    "Stop generating",
    "応答を停止",
    "生成を停止",
)

# 現行ChatGPTでは停止ボタンのaccessible nameが安定しない一方、
# data-testidは送信ボタンと切り替わる形で公開されている。
STOP_BUTTON_SELECTORS = ("button[data-testid='stop-button']",)

ATTACH_BUTTON_NAMES = (
    "Attach files",
    "Add photos & files",
    "ファイルを添付",
    "写真やファイルを追加",
)

UPLOAD_MENU_NAMES = (
    "Upload from computer",
    "Upload files",
    "コンピューターからアップロード",
    "ファイルをアップロード",
)

LOGIN_LINK_NAMES = (
    "Log in",
    "ログイン",
)

CHALLENGE_TEXTS = (
    "Verify you are human",
    "Performing security verification",
    "Cloudflare",
    "人間であることを確認",
    "セキュリティ確認",
)

CHALLENGE_TITLE_TEXTS = (
    "Just a moment",
    "しばらくお待ちください",
)

CHALLENGE_SELECTORS = (
    "input[id^='cf-chl-widget-']",
    "#challenge-running",
    "#cf-challenge-running",
    "iframe[src*='challenges.cloudflare.com']",
)

# レート制限や履歴制限など、composer 操作を遮るモーダル。
BLOCKING_MODAL_SELECTORS = (
    "[data-testid='modal-conversation-history-rate-limit']",
    "#modal-conversation-history-rate-limit",
)

# 現行 UI は rate-limit モーダルの固定 testid/id を持たない場合がある。
# その場合でも表示中の dialog/modal の本文を確認してから操作する。
BLOCKING_MODAL_FALLBACK_SELECTORS = (
    "[role='dialog']",
    "dialog",
    "[aria-modal='true']",
)

BLOCKING_MODAL_TEXTS = (
    "リクエストが多すぎます",
)

BLOCKING_MODAL_DISMISS_NAMES = (
    "了解",
    "OK",
    "Got it",
    "Dismiss",
    "閉じる",
)
