"use client";

import { useEffect, useCallback } from "react";

const RESTART_WAIT_TIMEOUT_MS = 300_000;
const INITIAL_POLL_DELAY_MS = 250;
const MAX_POLL_DELAY_MS = 5_000;
const HEALTH_REQUEST_TIMEOUT_MS = 3_000;
const RESTART_REQUEST_TIMEOUT_MS = 5_000;

type RestartOverlay = ReturnType<typeof createRestartOverlay>;

let activeRestartPromise: Promise<void> | null = null;
let activeRestartOverlay: RestartOverlay | null = null;

type RestartWaitResult = "ready" | "timeout";
type HealthSnapshot = { ready: boolean; bootId: string | null };

const RESTART_REFUSAL_MESSAGES: Record<string, string> = {
  operation_busy:
    "会話またはエージェント処理中のため再起動を保留しました。処理完了後に再試行してください。",
  restart_pending:
    "再起動要求を確認中です。しばらく待ってから状態を確認してください。",
  busy_state_timeout:
    "処理中の作業を確認できなかったため、再起動を保留しました。",
  busy_state_unknown:
    "処理中の作業を確認できなかったため、再起動を保留しました。",
  restart_unavailable: "再起動を開始できませんでした。状態を確認して再試行してください。",
};

async function restartRefusalMessage(response: Response): Promise<string | null> {
  if (response.status !== 409 && response.status !== 503) return null;
  try {
    const payload = (await response.json()) as { error?: unknown };
    return typeof payload.error === "string"
      ? RESTART_REFUSAL_MESSAGES[payload.error] ?? null
      : null;
  } catch {
    return null;
  }
}

type RestartWaitOptions = {
  checkHealth?: () => Promise<HealthSnapshot>;
  sleep?: (delayMs: number) => Promise<void>;
  now?: () => number;
  timeoutMs?: number;
  initialPollDelayMs?: number;
  maxPollDelayMs?: number;
  previousBootId?: string | null;
  initiallyDown?: boolean;
};

async function checkBackendHealth(): Promise<HealthSnapshot> {
  try {
    const response = await fetch("/api/python-proxy/health", {
      cache: "no-store",
      signal: AbortSignal.timeout(HEALTH_REQUEST_TIMEOUT_MS),
    });
    let bootId: string | null = null;
    if (response.ok) {
      try {
        const payload = (await response.json()) as { boot_id?: unknown };
        bootId = typeof payload.boot_id === "string" ? payload.boot_id : null;
      } catch {
        // 旧backendなどboot_idを返さない応答はdown→ready fallbackで扱う。
      }
    }
    return { ready: response.ok, bootId };
  } catch {
    return { ready: false, bootId: null };
  }
}

/** 再起動要求後、バックエンドのヘルスチェックが成功するまで待機する。 */
export async function waitForBackendRestart(
  options: RestartWaitOptions = {},
): Promise<RestartWaitResult> {
  const checkHealth = options.checkHealth ?? checkBackendHealth;
  const sleep =
    options.sleep ??
    ((delayMs: number) =>
      new Promise<void>((resolve) => window.setTimeout(resolve, delayMs)));
  const now = options.now ?? Date.now;
  const timeoutMs = options.timeoutMs ?? RESTART_WAIT_TIMEOUT_MS;
  const maxPollDelayMs = options.maxPollDelayMs ?? MAX_POLL_DELAY_MS;
  let pollDelayMs = options.initialPollDelayMs ?? INITIAL_POLL_DELAY_MS;
  const deadline = now() + timeoutMs;
  const previousBootId = options.previousBootId ?? null;
  let observedDown = options.initiallyDown ?? false;

  while (now() < deadline) {
    await sleep(Math.min(pollDelayMs, deadline - now()));

    const health = await checkHealth();
    if (previousBootId) {
      if (
        health.ready &&
        health.bootId !== null &&
        health.bootId !== previousBootId
      ) {
        return "ready";
      }
    } else if (!health.ready) {
      observedDown = true;
    } else if (observedDown) {
      return "ready";
    }

    pollDelayMs = Math.min(maxPollDelayMs, Math.ceil(pollDelayMs * 1.5));
  }

  return "timeout";
}

function createRestartOverlay() {
  const previousFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = document.createElement("div");
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "管理サーバーの再起動");
  overlay.dataset.restartOverlay = "true";
  overlay.dataset.state = "running";
  overlay.style.cssText = [
    "position:fixed",
    "inset:0",
    "background:rgba(0,0,0,0.6)",
    "display:flex",
    "align-items:center",
    "justify-content:center",
    "z-index:9999",
    "font-size:1.1rem",
    "color:#fff",
    "letter-spacing:0.02em",
  ].join(";");

  const panel = document.createElement("div");
  panel.style.cssText = [
    "display:flex",
    "flex-direction:column",
    "align-items:center",
    "gap:16px",
    "max-width:520px",
    "padding:24px",
    "text-align:center",
  ].join(";");

  const status = document.createElement("div");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");

  const retryButton = document.createElement("button");
  retryButton.type = "button";
  retryButton.textContent = "再起動を再試行";
  retryButton.dataset.restartRetry = "true";
  retryButton.style.cssText = [
    "display:none",
    "border:1px solid rgba(255,255,255,0.7)",
    "border-radius:6px",
    "background:rgba(255,255,255,0.12)",
    "padding:8px 16px",
    "color:#fff",
    "cursor:pointer",
  ].join(";");

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.textContent = "閉じる";
  closeButton.setAttribute("aria-label", "再起動状態画面を閉じる");
  closeButton.dataset.restartClose = "true";
  closeButton.style.cssText = [
    "display:none",
    "border:1px solid rgba(255,255,255,0.7)",
    "border-radius:6px",
    "background:transparent",
    "padding:8px 16px",
    "color:#fff",
    "cursor:pointer",
  ].join(";");

  panel.append(status, retryButton, closeButton);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  const result = {
    root: overlay,
    status,
    retryButton,
    closeButton,
    dismissible: false,
    previousFocus,
  };
  retryButton.onclick = () => {
    if (!result.dismissible || activeRestartPromise) return;
    void startRestartAttempt(result);
  };
  closeButton.onclick = () => {
    if (!result.dismissible || activeRestartOverlay !== result) return;
    result.root.remove();
    activeRestartOverlay = null;
    if (result.previousFocus?.isConnected) {
      result.previousFocus.focus({ preventScroll: true });
    }
  };
  return result;
}

function getRestartOverlay(): RestartOverlay {
  if (activeRestartOverlay?.status.isConnected) {
    return activeRestartOverlay;
  }
  activeRestartOverlay = createRestartOverlay();
  return activeRestartOverlay;
}

function isCurrentRestartOverlay(overlay: RestartOverlay): boolean {
  return activeRestartOverlay === overlay && overlay.root.isConnected;
}

function setRestartStatus(overlay: RestartOverlay, message: string): void {
  if (!isCurrentRestartOverlay(overlay)) return;
  overlay.status.textContent = message;
}

function showRestartFailure(
  overlay: RestartOverlay,
  message: string,
  state: "refused" | "failed" | "timeout",
): void {
  if (!isCurrentRestartOverlay(overlay)) return;
  overlay.root.dataset.state = state;
  overlay.status.textContent = message;
  overlay.dismissible = true;
  overlay.retryButton.disabled = false;
  overlay.retryButton.style.display = "block";
  overlay.closeButton.style.display = "block";
  overlay.closeButton.focus({ preventScroll: true });
}

function prepareRestartAttempt(overlay: RestartOverlay): void {
  if (!isCurrentRestartOverlay(overlay)) return;
  overlay.root.dataset.state = "running";
  overlay.dismissible = false;
  overlay.retryButton.disabled = true;
  overlay.retryButton.style.display = "none";
  overlay.closeButton.style.display = "none";
  overlay.status.textContent = "現在のサービスを確認しています...";
}

async function runRestartAttempt(overlay: RestartOverlay) {
  prepareRestartAttempt(overlay);
  const previousHealth = await checkBackendHealth();
  if (!isCurrentRestartOverlay(overlay)) return;
  setRestartStatus(overlay, "再起動を要求しています...");

  try {
    const response = await fetch("/api/python-proxy/admin/restart", {
      method: "POST",
      credentials: "include",
      signal: AbortSignal.timeout(RESTART_REQUEST_TIMEOUT_MS),
    });
    const refusal = await restartRefusalMessage(response);
    if (!isCurrentRestartOverlay(overlay)) return;
    if (refusal) {
      showRestartFailure(overlay, refusal, "refused");
      return;
    }
    const ambiguouslyAccepted = [502, 503, 504].includes(response.status);
    if (!response.ok && !ambiguouslyAccepted) {
      showRestartFailure(
        overlay,
        `再起動を開始できませんでした（HTTP ${response.status}）。`,
        "failed",
      );
      return;
    }
  } catch {
    // 再起動開始直後の切断やproxy timeoutでも、health監視で結果を確認する。
  }

  if (!isCurrentRestartOverlay(overlay)) return;
  setRestartStatus(overlay, "サービスの起動完了を確認しています...");
  const result = await waitForBackendRestart({
    previousBootId: previousHealth.bootId,
    initiallyDown: !previousHealth.ready,
  });
  if (!isCurrentRestartOverlay(overlay)) return;
  if (result === "ready") {
    setRestartStatus(overlay, "再起動が完了しました。再読み込みします...");
    window.location.reload();
    return;
  }

  showRestartFailure(
    overlay,
    "300秒以内に復帰を確認できませんでした。ページは保持しています。状態を確認して再試行してください。",
    "timeout",
  );
}

function startRestartAttempt(overlay?: RestartOverlay): Promise<void> {
  if (activeRestartPromise) {
    return activeRestartPromise;
  }

  const attemptOverlay = overlay ?? getRestartOverlay();
  prepareRestartAttempt(attemptOverlay);
  const attempt = Promise.resolve().then(() =>
    runRestartAttempt(attemptOverlay),
  );
  activeRestartPromise = attempt;
  attempt.then(
    () => {
      if (activeRestartPromise === attempt) activeRestartPromise = null;
    },
    () => {
      if (activeRestartPromise === attempt) activeRestartPromise = null;
    },
  );
  return attempt;
}

/**
 * バックエンド再起動を実行する共通関数。
 * オーバーレイ表示 → API呼び出し → ヘルスチェック → リロード。
 * 認可チェックは行わないため、呼び出し側で admin 判定を済ませること。
 */
export function performAdminRestart(): Promise<void> {
  if (activeRestartPromise) return activeRestartPromise;
  return startRestartAttempt(getRestartOverlay());
}

async function checkAndRestart() {
  // 毎回最新の設定をDBから確認する（ショートカット用: restart_shortcut_enabled をチェック）
  let enabled = false;
  try {
    const res = await fetch("/api/auth/status", { credentials: "include" });
    const d = await res.json();
    if (
      d.authenticated &&
      d.user?.role === "admin" &&
      d.user?.user_settings?.restart_shortcut_enabled === true
    ) {
      enabled = true;
    }
  } catch {
    return;
  }
  if (!enabled) return;

  await performAdminRestart();
}

export function GlobalAdminRestart() {
  const handler = useCallback(() => {
    checkAndRestart();
  }, []);

  useEffect(() => {
    window.addEventListener("global-admin-restart", handler);
    return () => window.removeEventListener("global-admin-restart", handler);
  }, [handler]);

  return null;
}
