"use client";

import {
  useEffect,
  useRef,
  useState,
} from "react";
import {
  SIDEBAR_COLLAPSED_KEY,
  readCollapsed,
} from "../docs-workspace-shared";

// アウトライン本体とサイドバーの折りたたみ状態を保持するフック。
// collapsed の最新値を同期する collapsedRef もあわせて提供する。
// 折りたたみの実操作（toggle/expand 等）は tree ローディングに依存するため
// DocsWorkspace 側に残し、ここでは状態と ref 同期のみを切り出している（挙動不変）。
export function useDocsCollapse() {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => readCollapsed());
  const [sidebarCollapsed, setSidebarCollapsed] = useState<Set<string>>(() => readCollapsed(SIDEBAR_COLLAPSED_KEY));
  const collapsedRef = useRef(collapsed);

  useEffect(() => {
    collapsedRef.current = collapsed;
  }, [collapsed]);

  return {
    collapsed,
    setCollapsed,
    sidebarCollapsed,
    setSidebarCollapsed,
    collapsedRef,
  };
}
