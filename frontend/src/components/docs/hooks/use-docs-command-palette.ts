"use client";

import {
  useCallback,
  useState,
} from "react";
import type {
  DocsCommandMode,
} from "../docs-workspace-shared";

// コマンドパレットの開閉・モード・検索クエリを保持するフック。
// 挙動不変のため、DocsWorkspace が従来 useState/useCallback で持っていた状態と
// openCommand ヘルパーをそのまま切り出したもの。
export function useDocsCommandPalette() {
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandMode, setCommandMode] = useState<DocsCommandMode>({ kind: "root" });
  const [commandQuery, setCommandQuery] = useState("");

  const openCommand = useCallback((mode: DocsCommandMode = { kind: "root" }) => {
    setCommandQuery("");
    setCommandMode(mode);
    setCommandOpen(true);
  }, []);

  return {
    commandOpen,
    setCommandOpen,
    commandMode,
    setCommandMode,
    commandQuery,
    setCommandQuery,
    openCommand,
  };
}
