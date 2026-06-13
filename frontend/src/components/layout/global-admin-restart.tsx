"use client";

import { useEffect, useCallback } from "react";

/**
 * バックエンド再起動を実行する共通関数。
 * オーバーレイ表示 → API呼び出し → ヘルスチェック → リロード。
 * 認可チェックは行わないため、呼び出し側で admin 判定を済ませること。
 */
export async function performAdminRestart() {
  // 視覚フィードバック: 画面全体にオーバーレイ表示
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
  overlay.textContent = "再起動中...";
  document.body.appendChild(overlay);

  try {
    await fetch("/api/python-proxy/admin/restart", {
      method: "POST",
      credentials: "include",
    });
  } catch {
    /* プロセス終了で接続が切れるため無視 */
  }

  // ヘルスチェックして復帰したらリロード
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const res = await fetch("/api/python-proxy/health", {
        signal: AbortSignal.timeout(2000),
      });
      if (res.ok) {
        window.location.reload();
        return;
      }
    } catch {
      /* まだ起動中 */
    }
  }
  window.location.reload();
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
