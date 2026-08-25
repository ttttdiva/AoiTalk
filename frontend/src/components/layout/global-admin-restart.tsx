"use client";

import { useEffect, useCallback } from "react";

const RESTART_WAIT_TIMEOUT_MS = 180_000;
const INITIAL_POLL_DELAY_MS = 250;
const MAX_POLL_DELAY_MS = 5_000;
const HEALTH_REQUEST_TIMEOUT_MS = 3_000;
const RESTART_REQUEST_TIMEOUT_MS = 5_000;

type RestartOverlay = ReturnType<typeof createRestartOverlay>;

let activeRestartPromise: Promise<void> | null = null;
let activeRestartOverlay: RestartOverlay | null = null;

type RestartWaitResult = "ready" | "timeout";
type HealthSnapshot = { ready: boolean; bootId: string | null };

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
  const overlay = document.createElement("div");
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
  retryButton.style.cssText = [
    "display:none",
    "border:1px solid rgba(255,255,255,0.7)",
    "border-radius:6px",
    "background:rgba(255,255,255,0.12)",
    "padding:8px 16px",
    "color:#fff",
    "cursor:pointer",
  ].join(";");

  panel.append(status, retryButton);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  return { status, retryButton };
}

function getRestartOverlay(): RestartOverlay {
  if (activeRestartOverlay?.status.isConnected) {
    return activeRestartOverlay;
  }
  activeRestartOverlay = createRestartOverlay();
  return activeRestartOverlay;
}

async function runRestartAttempt({ status, retryButton }: RestartOverlay) {
  retryButton.disabled = true;
  retryButton.style.display = "none";
  status.textContent = "現在のサービスを確認しています...";
  const previousHealth = await checkBackendHealth();
  status.textContent = "再起動を要求しています...";

  try {
    const response = await fetch("/api/python-proxy/admin/restart", {
      method: "POST",
      credentials: "include",
      signal: AbortSignal.timeout(RESTART_REQUEST_TIMEOUT_MS),
    });
    const ambiguouslyAccepted = [502, 503, 504].includes(response.status);
    if (!response.ok && !ambiguouslyAccepted) {
      status.textContent = `再起動を開始できませんでした（HTTP ${response.status}）。`;
      retryButton.disabled = false;
      retryButton.style.display = "block";
      return;
    }
  } catch {
    // 再起動開始直後の切断やproxy timeoutでも、health監視で結果を確認する。
  }

  status.textContent = "サービスの起動完了を確認しています...";
  const result = await waitForBackendRestart({
    previousBootId: previousHealth.bootId,
    initiallyDown: !previousHealth.ready,
  });
  if (result === "ready") {
    status.textContent = "再起動が完了しました。再読み込みします...";
    window.location.reload();
    return;
  }

  status.textContent =
    "180秒以内に復帰を確認できませんでした。ページは保持しています。状態を確認して再試行してください。";
  retryButton.disabled = false;
  retryButton.style.display = "block";
}

function startRestartAttempt(): Promise<void> {
  if (activeRestartPromise) {
    return activeRestartPromise;
  }

  const overlay = getRestartOverlay();
  const attempt = runRestartAttempt(overlay);
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
  const overlay = getRestartOverlay();
  overlay.retryButton.onclick = () => {
    void startRestartAttempt();
  };
  return startRestartAttempt();
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
