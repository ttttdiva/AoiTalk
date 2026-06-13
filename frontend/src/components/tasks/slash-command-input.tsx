"use client";

import {
  useState,
  useRef,
  useEffect,
  useCallback,
  type KeyboardEvent,
  type ChangeEvent,
  type ComponentProps,
  type Ref,
} from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

const TASK_COMMAND_ALIASES: Record<string, string[]> = {
  "/due": ["/d"],
  "/start": ["/s", "/sd"],
  "/status": [],
  "/priority": [],
  "/t": [],
  "/m": [],
};

export function getTaskCommandVariants(command: string): string[] {
  return [command, ...(TASK_COMMAND_ALIASES[command] || [])];
}

export function normalizeTaskCommand(command: string): string | null {
  const normalized = command.toLowerCase();
  for (const [canonical, aliases] of Object.entries(TASK_COMMAND_ALIASES)) {
    if (canonical === normalized || aliases.includes(normalized)) {
      return canonical;
    }
  }
  return null;
}

/** スラッシュコマンド定義 */
export interface SlashCommandDef {
  /** コマンド名（先頭の / 含む） */
  command: string;
  /** 日本語説明 */
  label: string;
  /** 使い方例 */
  usage: string;
}

/** 値プレビュー関数: コマンド名と入力値からプレビュー文字列を返す（null で非表示） */
export type ValuePreviewFn = (
  command: string,
  rawValue: string,
) => string | null;

/** コマンド値の候補（/m のプロジェクト名など） */
export interface CommandCandidate {
  /** 補完する値（プロジェクト名など） */
  value: string;
  /** 表示ラベル（省略時は value を使用） */
  label?: string;
  /** 候補に色ドットを表示（タグ用） */
  color?: string;
  /** 選択済みチェックマーク表示 */
  checked?: boolean;
}

/** タスク用デフォルトコマンド */
export const TASK_SLASH_COMMANDS: SlashCommandDef[] = [
  { command: "/due", label: "期限設定", usage: "/due tomorrow" },
  { command: "/start", label: "開始日設定", usage: "/start today" },
  { command: "/status", label: "ステータス設定", usage: "/status wip" },
  { command: "/priority", label: "優先度設定", usage: "/priority high" },
  { command: "/t", label: "タグ切替", usage: "/t meeting" },
  { command: "/m", label: "プロジェクト移動", usage: "/m ProjectName" },
];

type InputProps = ComponentProps<typeof Input>;

function assignRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (!ref) return;
  if (typeof ref === "function") {
    ref(value);
    return;
  }
  ref.current = value;
}

interface SlashCommandInputProps extends Omit<
  InputProps,
  "onChange" | "value"
> {
  value: string;
  onChange: (value: string) => void;
  commands?: SlashCommandDef[];
  /** Enter キーでフォーム送信させたい場合のコールバック（補完中でなければ呼ばれる） */
  onSubmitIntent?: () => void;
  submitOnEnter?: boolean;
  /** 値入力中にプレビューを返す関数 */
  getValuePreview?: ValuePreviewFn;
  /** Tab 時にプレビュー候補を入力欄へ補完する関数 */
  getValueCompletion?: ValuePreviewFn;
  /** スラッシュコマンドをインラインでパースする（blurせずにEnterで処理） */
  onParseSlashCommands?: (text: string) => string;
  /** コマンド別の値候補（例: { "/m": [{value:"Project1"}, ...] }） */
  commandCandidates?: Record<string, CommandCandidate[]>;
  /** 候補アイテムに追加のUI（三点リーダー等）を表示する */
  renderCandidateAction?: (
    command: string,
    candidate: CommandCandidate,
  ) => React.ReactNode;
  inputRef?: Ref<HTMLInputElement>;
  onNavigateDown?: () => void;
}

/**
 * スラッシュコマンド補完付き Input。
 * テキスト中の最後の "/" 以降を検索し、候補をポップアップ表示する。
 * Enter で補完（フォーム送信はブロック）、Esc で閉じる。
 *
 * `/m` など commandCandidates が設定されたコマンドでは、
 * コマンド確定後に値候補をドロップダウン表示し、先頭一致で絞り込み＆Enterで補完する。
 */
export function SlashCommandInput({
  value,
  onChange,
  commands = TASK_SLASH_COMMANDS,
  onSubmitIntent,
  submitOnEnter = true,
  getValuePreview,
  getValueCompletion,
  onParseSlashCommands,
  commandCandidates,
  renderCandidateAction,
  inputRef: externalInputRef,
  onNavigateDown,
  className,
  onBlur,
  ...rest
}: SlashCommandInputProps) {
  // --- コマンド補完メニュー ---
  const [showMenu, setShowMenu] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [filtered, setFiltered] = useState<SlashCommandDef[]>([]);
  const [slashPos, setSlashPos] = useState(-1);

  // --- 値候補メニュー ---
  const [showCandidates, setShowCandidates] = useState(false);
  const [candidateList, setCandidateList] = useState<CommandCandidate[]>([]);
  const [candidateIdx, setCandidateIdx] = useState(0);
  const [candidateCmd, setCandidateCmd] = useState<string>(""); // "/m" etc
  const [candidateSlashPos, setCandidateSlashPos] = useState(-1);

  // --- 値プレビュー ---
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewCmd, setPreviewCmd] = useState<SlashCommandDef | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const setInputRef = useCallback(
    (node: HTMLInputElement | null) => {
      inputRef.current = node;
      assignRef(externalInputRef, node);
    },
    [externalInputRef],
  );

  // ─── 最後の有効なコマンド "/" を探す（先頭 or 空白直後の "/" のみ） ───
  const findLastCommandSlash = useCallback((text: string): number => {
    for (let i = text.length - 1; i >= 0; i--) {
      if (text[i] === "/" && (i === 0 || text[i - 1] === " ")) {
        return i;
      }
    }
    return -1;
  }, []);

  // ─── カーソル位置のスラッシュトークンを検出 ───
  const detectSlash = useCallback(
    (text: string) => {
      const lastSlash = findLastCommandSlash(text);
      if (lastSlash < 0) {
        setShowMenu(false);
        setShowCandidates(false);
        setPreviewText(null);
        setPreviewCmd(null);
        return;
      }

      const fragment = text.slice(lastSlash);
      // コマンド名部分を抽出（最初のスペースまで）
      const spaceIdx = fragment.indexOf(" ");

      if (spaceIdx >= 0) {
        // コマンド確定済み → 値入力モード
        setShowMenu(false);
        const cmdPart = fragment.slice(0, spaceIdx).toLowerCase();
        const valuePart = fragment.slice(spaceIdx + 1);
        const canonicalCommand = normalizeTaskCommand(cmdPart) || cmdPart;
        const matchedCmd = commands.find(
          (c) => c.command.toLowerCase() === canonicalCommand,
        );

        // コマンド候補がある場合（/m など） → 値候補ドロップダウン
        if (
          matchedCmd &&
          commandCandidates &&
          commandCandidates[matchedCmd.command]
        ) {
          const allCandidates = commandCandidates[matchedCmd.command];
          const query = valuePart.toLowerCase();
          const matches = query
            ? allCandidates.filter(
                (c) =>
                  c.value.toLowerCase().startsWith(query) ||
                  (c.label && c.label.toLowerCase().startsWith(query)),
              )
            : allCandidates; // 空入力時は全候補

          setCandidateList(matches);
          setCandidateIdx(0);
          setCandidateCmd(matchedCmd.command);
          setCandidateSlashPos(lastSlash);
          setShowCandidates(matches.length > 0);
          // プレビューは候補が0件の場合のみ表示
          if (matches.length === 0 && valuePart.length > 0 && getValuePreview) {
            const preview = getValuePreview(matchedCmd.command, valuePart);
            setPreviewText(preview);
            setPreviewCmd(preview ? matchedCmd : null);
          } else {
            setPreviewText(null);
            setPreviewCmd(null);
          }
          return;
        }

        // 候補なしコマンド → 従来のプレビューモード
        setShowCandidates(false);
        if (matchedCmd && valuePart.length > 0 && getValuePreview) {
          const preview = getValuePreview(matchedCmd.command, valuePart);
          setPreviewText(preview);
          const completion = getValueCompletion?.(
            matchedCmd.command,
            valuePart,
          );
          setPreviewCmd(preview || completion ? matchedCmd : null);
        } else {
          setPreviewText(null);
          setPreviewCmd(null);
        }
        return;
      }

      // コマンド名入力中 → 補完メニュー
      setShowCandidates(false);
      setPreviewText(null);
      setPreviewCmd(null);
      const query = fragment.toLowerCase(); // e.g. "/s", "/du"
      const matches = commands.filter((c) =>
        getTaskCommandVariants(c.command).some((variant) =>
          variant.toLowerCase().startsWith(query),
        ),
      );

      if (matches.length > 0) {
        setFiltered(matches);
        setSelectedIdx(0);
        setSlashPos(lastSlash);
        setShowMenu(true);
      } else {
        setShowMenu(false);
      }
    },
    [
      commands,
      getValuePreview,
      getValueCompletion,
      findLastCommandSlash,
      commandCandidates,
    ],
  );

  // 値が変わるたびに検出
  const handleChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const newVal = e.target.value;
      onChange(newVal);
      detectSlash(newVal);
    },
    [onChange, detectSlash],
  );

  // コマンド補完を実行
  const applyCommand = useCallback(
    (cmd: SlashCommandDef) => {
      // slashPos から末尾までを cmd.command + " " に置換
      const before = value.slice(0, slashPos);
      const newVal = before + cmd.command + " ";
      onChange(newVal);
      setShowMenu(false);
      // コマンド確定後に候補を検出
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        detectSlash(newVal);
      });
    },
    [value, slashPos, onChange, detectSlash],
  );

  // 値候補を確定
  const applyCandidateSelection = useCallback(
    (candidate: CommandCandidate) => {
      // `/m ProjectName` の形にする → 即座にパース
      const before = value.slice(0, candidateSlashPos);
      const newVal = before + candidateCmd + " " + candidate.value + " ";
      setShowCandidates(false);
      setCandidateList([]);

      // パース処理を実行（コマンドを消費して反映）
      if (onParseSlashCommands) {
        const result = onParseSlashCommands(newVal);
        onChange(result);
      } else {
        onChange(newVal);
      }
      requestAnimationFrame(() => inputRef.current?.focus());
    },
    [value, candidateSlashPos, candidateCmd, onChange, onParseSlashCommands],
  );

  const applyPreviewCompletion = useCallback(() => {
    if (!previewCmd || !getValueCompletion) return false;
    const lastSlash = findLastCommandSlash(value);
    if (lastSlash < 0) return false;

    const fragment = value.slice(lastSlash);
    const spaceIdx = fragment.indexOf(" ");
    if (spaceIdx < 0) return false;

    const cmdPart = fragment.slice(0, spaceIdx).toLowerCase();
    const canonicalCommand = normalizeTaskCommand(cmdPart) || cmdPart;
    if (canonicalCommand !== previewCmd.command) return false;

    const rawValue = fragment.slice(spaceIdx + 1).trim();
    if (!rawValue) return false;

    const completion = getValueCompletion(previewCmd.command, rawValue)?.trim();
    if (!completion || completion === rawValue) return false;

    const before = value.slice(0, lastSlash);
    const newVal = `${before}${previewCmd.command} ${completion} `;
    onChange(newVal);
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      detectSlash(newVal);
    });
    return true;
  }, [
    detectSlash,
    findLastCommandSlash,
    getValueCompletion,
    onChange,
    previewCmd,
    value,
  ]);

  // テキストに未処理のスラッシュコマンドが含まれているか判定
  const hasUnprocessedSlash = useCallback(
    (text: string): boolean => {
      return commands.some((cmd) => {
        const variants = getTaskCommandVariants(cmd.command)
          .map((variant) => variant.replace("/", "\\/"))
          .join("|");
        const regex = new RegExp(`(?:^|\\s)(?:${variants})\\s`, "i");
        return regex.test(text);
      });
    },
    [commands],
  );

  // キー操作
  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      // --- 値候補メニューが表示中 ---
      if (showCandidates && candidateList.length > 0) {
        switch (e.key) {
          case "ArrowDown":
            e.preventDefault();
            setCandidateIdx((prev) => (prev + 1) % candidateList.length);
            return;
          case "ArrowUp":
            e.preventDefault();
            setCandidateIdx(
              (prev) =>
                (prev - 1 + candidateList.length) % candidateList.length,
            );
            return;
          case "Enter":
          case "Tab":
            e.preventDefault();
            e.stopPropagation();
            applyCandidateSelection(candidateList[candidateIdx]);
            return;
          case "Escape":
            e.preventDefault();
            setShowCandidates(false);
            return;
        }
      }

      // --- コマンド補完メニューが表示中 ---
      if (showMenu) {
        switch (e.key) {
          case "ArrowDown":
            e.preventDefault();
            setSelectedIdx((prev) => (prev + 1) % filtered.length);
            break;
          case "ArrowUp":
            e.preventDefault();
            setSelectedIdx(
              (prev) => (prev - 1 + filtered.length) % filtered.length,
            );
            break;
          case "Enter":
          case "Tab":
            e.preventDefault();
            e.stopPropagation();
            applyCommand(filtered[selectedIdx]);
            break;
          case "Escape":
            e.preventDefault();
            setShowMenu(false);
            break;
        }
        return;
      }

      if (e.key === "ArrowDown" && onNavigateDown) {
        e.preventDefault();
        onNavigateDown();
        return;
      }

      // --- メニューなし ---
      if (e.key === "Tab") {
        if (applyPreviewCompletion()) {
          e.preventDefault();
          e.stopPropagation();
        }
        return;
      }

      if (e.key === "Enter") {
        const isSubmitShortcut = e.ctrlKey || e.metaKey;
        if (onSubmitIntent && isSubmitShortcut) {
          e.preventDefault();
          onSubmitIntent();
          return;
        }
        // 未処理のスラッシュコマンドが残っている場合はフォーム送信を阻止し、
        // インラインでパースしてフォーカスを維持する
        if (hasUnprocessedSlash(value)) {
          e.preventDefault();
          e.stopPropagation();
          if (onParseSlashCommands) {
            const newTitle = onParseSlashCommands(value);
            onChange(newTitle);
            // プレビューをクリア
            setPreviewText(null);
            setPreviewCmd(null);
            setShowCandidates(false);
          }
          return;
        }
        if (onSubmitIntent && submitOnEnter) {
          e.preventDefault();
          onSubmitIntent();
          return;
        }
      }
    },
    [
      showMenu,
      filtered,
      selectedIdx,
      applyCommand,
      showCandidates,
      candidateList,
      candidateIdx,
      applyCandidateSelection,
      applyPreviewCompletion,
      hasUnprocessedSlash,
      value,
      onParseSlashCommands,
      onChange,
      onSubmitIntent,
      submitOnEnter,
      onNavigateDown,
    ],
  );

  // メニュー外クリックで閉じる
  useEffect(() => {
    if (!showMenu && !showCandidates) return;
    const handler = (e: MouseEvent) => {
      if (
        menuRef.current &&
        !menuRef.current.contains(e.target as Node) &&
        inputRef.current &&
        !inputRef.current.contains(e.target as Node)
      ) {
        setShowMenu(false);
        setShowCandidates(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showMenu, showCandidates]);

  return (
    <div className="relative">
      <Input
        ref={setInputRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onBlur={(e) => {
          // メニュークリック時は閉じない
          if (menuRef.current?.contains(e.relatedTarget as Node)) return;
          setShowMenu(false);
          setShowCandidates(false);
          setPreviewText(null);
          setPreviewCmd(null);
          onBlur?.(e);
        }}
        className={className}
        {...rest}
      />

      {/* コマンド補完メニュー */}
      {showMenu && filtered.length > 0 && (
        <div
          ref={menuRef}
          className="absolute left-0 right-0 top-full z-50 mt-1 rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {filtered.map((cmd, idx) => (
            <div
              key={cmd.command}
              className={cn(
                "flex items-center justify-between rounded-sm px-2 py-1.5 text-sm cursor-pointer",
                idx === selectedIdx && "bg-accent text-accent-foreground",
              )}
              onMouseEnter={() => setSelectedIdx(idx)}
              onMouseDown={(e) => {
                e.preventDefault(); // blur 防止
                applyCommand(cmd);
              }}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono font-medium">{cmd.command}</span>
                <span className="text-muted-foreground">{cmd.label}</span>
              </div>
              <span className="text-xs text-muted-foreground">{cmd.usage}</span>
            </div>
          ))}
        </div>
      )}

      {/* 値候補ドロップダウン（/m のプロジェクト名など） */}
      {showCandidates && candidateList.length > 0 && (
        <div
          ref={menuRef}
          className="absolute left-0 right-0 top-full z-50 mt-1 rounded-md border bg-popover p-1 text-popover-foreground shadow-md max-h-48 overflow-y-auto"
        >
          <div className="px-2 py-1 text-[10px] text-muted-foreground border-b mb-1">
            {candidateCmd === "/m"
              ? "📁 プロジェクト選択"
              : candidateCmd === "/t"
                ? "🏷️ タグ選択"
                : "候補"}
            <span className="ml-2 opacity-60">↑↓ 選択 / Enter・Tab 確定</span>
          </div>
          {candidateList.map((c, idx) => (
            <div
              key={c.value}
              className={cn(
                "flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm cursor-pointer",
                idx === candidateIdx && "bg-accent text-accent-foreground",
              )}
              onMouseEnter={() => setCandidateIdx(idx)}
              onMouseDown={(e) => {
                e.preventDefault();
                applyCandidateSelection(c);
              }}
            >
              {c.color && (
                <span
                  className="size-2.5 rounded-full shrink-0"
                  style={{ backgroundColor: c.color }}
                />
              )}
              <span
                className={cn("flex-1", c.color && "font-medium")}
                style={c.color ? { color: c.color } : undefined}
              >
                {c.label || c.value}
              </span>
              {c.checked && <span className="text-primary text-xs">✓</span>}
              {renderCandidateAction && (
                <div onMouseDown={(e) => e.stopPropagation()}>
                  {renderCandidateAction(candidateCmd, c)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 値プレビュー（候補がないコマンドの場合） */}
      {previewText && previewCmd && !showCandidates && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
          <div className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm">
            <span className="font-mono font-medium text-muted-foreground">
              {previewCmd.command}
            </span>
            <span className="text-muted-foreground">{previewCmd.label}</span>
            <span className="mx-1 text-muted-foreground">→</span>
            <span className="font-medium">{previewText}</span>
          </div>
        </div>
      )}
    </div>
  );
}
