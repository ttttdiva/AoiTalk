"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type ShortcutItem = {
  keys: string[];
  description: string;
};

type ShortcutSection = {
  title: string;
  items: ShortcutItem[];
};

const SHORTCUT_SECTIONS: ShortcutSection[] = [
  {
    title: "チャット",
    items: [
      { keys: ["Ctrl", "Shift", "O"], description: "新規チャットを開始" },
      { keys: ["Ctrl", "J"], description: "チャット入力欄にフォーカス" },
    ],
  },
  {
    title: "ページ移動",
    items: [
      { keys: ["Alt", "1"], description: "チャット" },
      { keys: ["Alt", "2"], description: "タスク" },
      { keys: ["Alt", "3"], description: "カレンダー" },
      { keys: ["Alt", "4"], description: "レポート" },
      { keys: ["Alt", "5"], description: "ファイラー" },
      { keys: ["Alt", "6"], description: "プロジェクト" },
      { keys: ["Alt", "7"], description: "シナリオ" },
      { keys: ["Alt", "8"], description: "設定" },
    ],
  },
  {
    title: "スペース切り替え",
    items: [
      {
        keys: ["Alt", "Shift", "1~9"],
        description: "スペースを番号で切り替え",
      },
    ],
  },
  {
    title: "タスク",
    items: [
      { keys: ["Alt", "T"], description: "タスク作成ダイアログを開く" },
      { keys: ["Ctrl", "J"], description: "先頭タスクにフォーカス" },
      { keys: ["Ctrl", "F"], description: "タスク検索にフォーカス" },
      { keys: ["↑", "↓"], description: "フォーカス中のタスクを移動" },
      { keys: ["Enter"], description: "フォーカス中のタスクを開く" },
      { keys: ["Alt", "S"], description: "フォーカス中のタスクのタイマー開始" },
      { keys: ["Ctrl", "Space"], description: "フォーカス中のタスクを選択" },
      {
        keys: ["Delete"],
        description: "選択中またはフォーカス中のタスクを削除",
      },
      {
        keys: ["Ctrl", "C"],
        description: "選択中またはフォーカス中のタスクをコピー",
      },
      {
        keys: ["Ctrl", "X"],
        description: "選択中またはフォーカス中のタスクを切り取り",
      },
      {
        keys: ["Ctrl", "V"],
        description:
          "フォーカス中タスクの直後にコピーまたは切り取りタスクを貼り付け",
      },
      {
        keys: ["Ctrl", "Shift", "←/→"],
        description: "タスク画面のプロジェクトタブを切り替え",
      },
      { keys: ["T"], description: "タスク作成（入力欄以外）" },
      { keys: ["L"], description: "タスク一覧へ移動（入力欄以外）" },
      { keys: ["C"], description: "カレンダーへ移動（入力欄以外）" },
      { keys: ["P"], description: "メモ帳を開く（入力欄以外）" },
      { keys: ["S"], description: "タイマー停止（入力欄以外）" },
    ],
  },
  {
    title: "ファイラー",
    items: [
      { keys: ["Alt", "←"], description: "戻る" },
      { keys: ["Alt", "Backspace"], description: "戻る" },
      { keys: ["Alt", "→"], description: "進む" },
      { keys: ["Alt", "↑"], description: "上の階層へ移動" },
      { keys: ["Ctrl", "I"], description: "選択中の項目をZIP圧縮" },
      { keys: ["Ctrl", "U"], description: "選択中のZIPを展開" },
    ],
  },
  {
    title: "エディタ",
    items: [
      { keys: ["Ctrl", "]"], description: "Markdown見出しレベルを上げる" },
      {
        keys: ["Ctrl", "Shift", "]"],
        description: "Markdown見出しレベルを下げる",
      },
    ],
  },
  {
    title: "その他",
    items: [
      { keys: ["Ctrl", "K"], description: "コマンドパレットを開く" },
      { keys: ["?"], description: "ショートカット一覧を表示" },
      {
        keys: ["Alt", "Shift", "R"],
        description: "バックエンドを即時再起動（管理者・設定で有効時）",
      },
    ],
  },
];

function KeyBadge({ label }: { label: string }) {
  return (
    <kbd className="inline-flex items-center rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-mono font-medium text-muted-foreground shadow-sm">
      {label}
    </kbd>
  );
}

export function ShortcutsHelpDialog() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handler = () => setOpen(true);
    window.addEventListener("global-shortcuts-help", handler);
    return () => window.removeEventListener("global-shortcuts-help", handler);
  }, []);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-lg max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>キーボードショートカット</DialogTitle>
        </DialogHeader>
        <div className="space-y-5 pt-1">
          {SHORTCUT_SECTIONS.map((section) => (
            <div key={section.title}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {section.title}
              </h3>
              <div className="space-y-1">
                {section.items.map((item) => (
                  <div
                    key={item.description}
                    className="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-accent/50"
                  >
                    <span className="text-sm">{item.description}</span>
                    <div className="flex items-center gap-0.5">
                      {item.keys.map((k, i) => (
                        <span key={k} className="flex items-center gap-0.5">
                          {i > 0 && (
                            <span className="mx-0.5 text-xs text-muted-foreground">
                              +
                            </span>
                          )}
                          <KeyBadge label={k} />
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
