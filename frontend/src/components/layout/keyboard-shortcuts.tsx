"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { APP_ALT_SHORTCUTS } from "@/lib/app-navigation";
import { OPEN_DOCS_CLIP_INGEST_EVENT } from "@/lib/clip-ingest-shortcut";

export function KeyboardShortcuts() {
  const pathname = usePathname();

  useEffect(() => {
    function isInputFocused(): boolean {
      const el = document.activeElement;
      if (!el) return false;
      const tag = el.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT")
        return true;
      if ((el as HTMLElement).isContentEditable) return true;
      return false;
    }

    function handleOpenClipIngest() {
      window.dispatchEvent(new Event(OPEN_DOCS_CLIP_INGEST_EVENT));
    }

    function handleKeydown(e: KeyboardEvent) {
      // Ctrl+Alt+I: どの画面からでもDocsのクリップ取り込みを開く
      if (
        e.ctrlKey
        && e.altKey
        && !e.shiftKey
        && !e.metaKey
        && !e.repeat
        && e.key.toLowerCase() === "i"
      ) {
        e.preventDefault();
        handleOpenClipIngest();
        return;
      }

      // Ctrl+Shift+O: 新規チャット開始
      if (e.ctrlKey && e.shiftKey && (e.key === "o" || e.key === "O")) {
        e.preventDefault();
        localStorage.removeItem("aoitalk_last_session_id");
        window.location.href = "/chat";
        return;
      }

      // Ctrl+Shift+H: Today/Home オーバーレイ
      if (e.ctrlKey && e.shiftKey && (e.key === "h" || e.key === "H")) {
        e.preventDefault();
        window.dispatchEvent(new Event("global-open-home"));
        return;
      }

      // Ctrl+J: チャット入力欄にフォーカス
      // Files のインクリメンタル検索が「次の一致」として claim（preventDefault）した
      // 場合は譲る。claim されていない画面・コンテキストでは従来どおり処理する。
      if (e.ctrlKey && (e.key === "j" || e.key === "J")) {
        if (e.defaultPrevented) return;
        e.preventDefault();
        if (pathname.startsWith("/tasks")) {
          window.dispatchEvent(new Event("tasks-focus-first-row"));
          return;
        }
        const textarea = document.querySelector("textarea");
        if (textarea) textarea.focus();
        return;
      }

      // Alt+Shift+R: 管理者限定再起動
      if (e.altKey && e.shiftKey && (e.key === "r" || e.key === "R")) {
        e.preventDefault();
        window.dispatchEvent(new Event("global-admin-restart"));
        return;
      }

      // Alt+Shift+数字: スペース切り替え（Shift押下時e.keyが記号になるのでe.codeで判定）
      if (e.altKey && e.shiftKey && e.code.startsWith("Digit")) {
        const digit = parseInt(e.code.charAt(5), 10);
        if (digit >= 1 && digit <= 9) {
          e.preventDefault();
          window.dispatchEvent(
            new CustomEvent("global-switch-space", { detail: digit - 1 }),
          );
          return;
        }
      }

      // Alt+キー: 既存ショートカット（入力欄フォーカス中でも発火）
      if (e.altKey) {
        // Alt+P（修飾キーなし）: グローバルメモ帳を開閉
        // Alt+Shift+P はチャットの Project context 代替キーに譲る。
        if (
          !e.ctrlKey &&
          !e.metaKey &&
          !e.shiftKey &&
          (e.key === "p" || e.key === "P")
        ) {
          e.preventDefault();
          window.dispatchEvent(new Event("global-open-memo"));
          return;
        }

        // Alt+T: タスク作成ダイアログを開く
        if (e.key === "t" || e.key === "T") {
          e.preventDefault();
          window.dispatchEvent(new Event("global-create-task"));
          return;
        }

        // Alt+数字: ページ遷移
        if (APP_ALT_SHORTCUTS[e.key]) {
          e.preventDefault();
          window.location.href = APP_ALT_SHORTCUTS[e.key];
        }
        return;
      }

      // ?: ショートカット一覧（Shift+/なのでshiftKeyガードより前に処理）
      if (e.key === "?") {
        if (!isInputFocused()) {
          e.preventDefault();
          window.dispatchEvent(new Event("global-shortcuts-help"));
        }
        return;
      }

      // 単独キーショートカット: 入力欄にフォーカス中は無効
      if (e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (isInputFocused()) return;
      // ファイラーは一覧上でのインクリメンタル検索が単発キーを占有するため、
      // 単独キーショートカット全体を無効化する（Alt+ 系は手前で処理済み）
      if (pathname.startsWith("/filer")) return;

      switch (e.key) {
        case "t":
        case "T":
          e.preventDefault();
          window.dispatchEvent(new Event("global-create-task"));
          break;
        case "l":
        case "L":
          e.preventDefault();
          window.location.href = "/tasks";
          break;
        case "c":
        case "C":
          e.preventDefault();
          window.location.href = "/calendar";
          break;
        case "p":
        case "P":
          e.preventDefault();
          window.dispatchEvent(new Event("global-open-memo"));
          break;
        case "s":
        case "S":
          e.preventDefault();
          window.dispatchEvent(new Event("global-stop-timer"));
          break;
      }
    }

    window.addEventListener("keydown", handleKeydown);
    return () => window.removeEventListener("keydown", handleKeydown);
  }, [pathname]);

  return null;
}
