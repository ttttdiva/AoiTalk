"use client";

import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { APP_VIEW_TABS } from "@/lib/app-navigation";
import { CHAT_SHORTCUT_HELP_ITEMS } from "@/lib/chat-keyboard-shortcuts";

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
    items: CHAT_SHORTCUT_HELP_ITEMS,
  },
  {
    title: "ページ移動",
    items: APP_VIEW_TABS.filter((tab) => Boolean(tab.shortcut)).map((tab) => ({
      keys: ["Alt", tab.shortcut],
      description: tab.title,
    })),
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
    title: "Docs",
    items: [
      { keys: ["Ctrl", "Alt", "I"], description: "クリップ取り込みを開く" },
    ],
  },
  {
    title: "タスク",
    items: [
      { keys: ["Ctrl", "Shift", "H"], description: "Todayを開く" },
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
      { keys: ["P"], description: "メモ帳を開閉（入力欄以外）" },
      { keys: ["Alt", "P"], description: "メモ帳を開閉" },
      { keys: ["S"], description: "タイマー停止（入力欄以外）" },
    ],
  },
  {
    title: "Files",
    items: [
      { keys: ["Ctrl", "D"], description: "現在のフォルダをブックマークへ登録" },
      { keys: ["Ctrl", "Shift", "D"], description: "フォーカス中のファイルをランチャーへ登録" },
      { keys: ["Alt", "Q"], description: "ブックマークへ切り替えて一覧にフォーカス" },
      { keys: ["Alt", "E"], description: "ランチャーへ切り替えて一覧にフォーカス" },
      { keys: ["Alt", "J"], description: "ランチャー項目の親フォルダへ移動" },
      { keys: ["↑", "↓"], description: "ブックマーク／ランチャー項目を移動・並び替え" },
      { keys: ["Enter"], description: "フォーカス項目を開く／フォルダへ移動" },
      { keys: ["Esc"], description: "Files本体へフォーカスを戻す" },
      { keys: ["Delete"], description: "フォーカス項目を削除" },
      { keys: ["Alt", "←"], description: "戻る" },
      { keys: ["Backspace"], description: "戻る" },
      { keys: ["Alt", "Backspace"], description: "戻る" },
      { keys: ["Alt", "→"], description: "進む" },
      { keys: ["Alt", "↑"], description: "上の階層へ移動" },
      { keys: ["Ctrl", "H"], description: "現在のタブのホームへ移動" },
      {
        keys: ["Ctrl", "←/→"],
        description:
          "Filesタブ（Project Files / User Files / HF / Hydrus）を切り替え",
      },
      { keys: [":"], description: "サムネイル表示" },
      { keys: [";"], description: "リスト表示" },
      { keys: ["F8"], description: "名前順" },
      { keys: ["F9"], description: "更新日時順" },
      { keys: ["Ctrl", "S"], description: "ファイル名の即席フィルターを開閉" },
      {
        keys: ["Ctrl", "F"],
        description:
          "ファイル名・フォルダ名の検索（F3も可・正規表現・置換対応）",
      },
      { keys: ["Ctrl", "N"], description: "新規フォルダ作成" },
      { keys: ["Ctrl", "Shift", "N"], description: "新規テキストファイル作成" },
      { keys: ["F7"], description: "新規フォルダ作成" },
      { keys: ["Shift", "F7"], description: "新規テキストファイル作成" },
      { keys: ["Delete"], description: "削除" },
      {
        keys: ["Ctrl", "Z"],
        description: "直前の削除・リネーム・置換・移動を元に戻す（最大3操作）",
      },
      {
        keys: ["Ctrl", "Y"],
        description: "元に戻した操作をやり直す（Ctrl+Shift+Z も同じ）",
      },
      { keys: ["Ctrl", "I"], description: "選択中の項目をZIP圧縮" },
      { keys: ["Ctrl", "U"], description: "選択中の圧縮ファイルを展開" },
      {
        keys: ["Ctrl", "Shift", "L"],
        description: "選択中の項目をダウンロード",
      },
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
      <DialogContent size="lg" className="max-h-[80vh] overflow-y-auto">
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
                    key={`${item.description}-${item.keys.join("-")}`}
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
