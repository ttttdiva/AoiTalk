"use client";

import {
  useEffect,
  useRef,
  useState,
} from "react";
import {
  EXPANDED_KEY,
  SIDEBAR_COLLAPSED_KEY,
  readCollapsed,
} from "../docs-workspace-shared";

// アウトライン本体とサイドバーの折りたたみ状態を保持するフック。
// collapsed の最新値を同期する collapsedRef もあわせて提供する。
// 折りたたみの実操作（toggle/expand 等）は tree ローディングに依存するため
// DocsWorkspace 側に残し、ここでは状態と ref 同期のみを切り出している（挙動不変）。
// サイドバー展開はセッション内 state のみ。旧 localStorage key は初回 mount で削除する。
export function useDocsCollapse() {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed());
  const [sidebarCollapsed, setSidebarCollapsed] = useState<Set<string>>(() => new Set());
  // ユーザーが明示的に展開したノードID。再訪時の子先読み（展開復元）の唯一の根拠にする。
  // useRef の引数は毎レンダー評価されるため、localStorage 読込は初回だけに限定する。
  const [initialExpanded] = useState<Set<string>>(() => readCollapsed(EXPANDED_KEY));
  const expandedRef = useRef(initialExpanded);
  const collapsedRef = useRef(collapsed);

  useEffect(() => {
    collapsedRef.current = collapsed;
  }, [collapsed]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.removeItem(SIDEBAR_COLLAPSED_KEY);
    } catch {
      // localStorage が無効な環境では無視する。
    }
  }, []);

  return {
    collapsed,
    setCollapsed,
    sidebarCollapsed,
    setSidebarCollapsed,
    collapsedRef,
    expandedRef,
  };
}
