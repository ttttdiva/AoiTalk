"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileKey2, Loader2, RefreshCw, Trash2, UploadCloud } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";
import { useConfirm } from "@/hooks/use-confirm";
import {
  COOKIE_STATUSES,
  X_COOKIE_ENDPOINT,
  CookieUploadPreparationError,
  cookieStatusLabel,
  normalizeXCookieStatus,
  prepareXCookieUpload,
  type CookieStatus,
  type XCookieStatusResponse,
} from "./cookie-management";

// Keep the browser guard aligned with the BFF/server body limit. The server
// still performs its own validation and size enforcement.
const MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024;
const COOKIE_FILE_READ_ERROR =
  "Cookieファイルを読み取れませんでした。UTF-8のファイルを選択してください。";

type Feedback = { kind: "success" | "error"; message: string };

/** Service cards are intentionally data-shaped so additional cookie-backed
 * services can be added without changing the surrounding Cookie管理 target. */
const COOKIE_SERVICE_DEFINITIONS = [{ id: "x", label: "X" }] as const;

function isCookieStatus(value: unknown): value is CookieStatus {
  return typeof value === "string" && (COOKIE_STATUSES as readonly string[]).includes(value);
}

async function readSafeJson(response: Response): Promise<unknown> {
  if (typeof response.json !== "function") return null;
  return response.json().catch(() => null);
}

async function readFileBytes(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") {
    try {
      return await file.arrayBuffer();
    } catch {
      throw new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR);
    }
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(reader.result);
      } else {
        reject(new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR));
      }
    };
    reader.onerror = () => reject(new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR));
    try {
      reader.readAsArrayBuffer(file);
    } catch {
      reject(new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR));
    }
  });
}

async function readFileContents(file: File): Promise<string> {
  let bytes: ArrayBuffer;
  try {
    bytes = await readFileBytes(file);
  } catch (error) {
    if (error instanceof CookieUploadPreparationError) throw error;
    throw new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(bytes));
  } catch {
    throw new CookieUploadPreparationError(COOKIE_FILE_READ_ERROR);
  }
}

function statusFromPayload(value: unknown): CookieStatus | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const direct = record.status;
  if (isCookieStatus(direct)) return direct;
  const detail = record.detail;
  if (typeof detail === "string" && isCookieStatus(detail)) return detail;
  if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
    const detailRecord = detail as Record<string, unknown>;
    const nested = detailRecord.status ?? detailRecord.code;
    if (isCookieStatus(nested)) return nested;
  }
  return null;
}

function actionForStatus(status: CookieStatus): string {
  switch (status) {
    case "unconfigured":
      return "Netscape形式またはChrome/EdgeのHARを選択してください。";
    case "available":
      return "現在の設定を置き換える場合は、別のファイルを選択してください。";
    case "invalid_format":
      return "Netscape形式のCookieファイル、または必要なCookieを含むHARを選び直してください。";
    case "missing_required_cookie":
      return "auth_token と ct0 を含むファイルを選び直してください。";
    case "expired":
      return "x.comへ再ログインして、Cookieをエクスポートし直してください。";
    case "unavailable":
    default:
      return "再試行するか、運用者にサーバー状態を確認してください。";
  }
}

function sourceDescription(status: XCookieStatusResponse): string | null {
  if (status.source === "personal") {
    if (status.status === "unconfigured") {
      return "個人Cookieは未設定です。このユーザーでは運用共有のフォールバックも無効です。";
    }
    if (status.status === "available") {
      return "個人設定のCookieを使用中です。新しいファイルを読み込むと、この設定を置き換えます。";
    }
    return "個人設定のCookieに問題があります。新しいファイルを読み込むと、この設定を置き換えます。";
  }
  if (status.source === "server_shared") {
    switch (status.status) {
      case "available":
        return "運用者が管理する共有サーバー設定を使用中です。個人Cookieを読み込むと、この設定を上書きできます。";
      case "unconfigured":
        return "運用者が管理する共有Cookieは設定されていないため、利用できません。個人Cookieを読み込んで設定できます。";
      case "invalid_format":
        return "運用者が管理する共有Cookieの形式に問題があります。個人Cookieを読み込むと、この設定を上書きできます。";
      case "missing_required_cookie":
        return "運用者が管理する共有Cookieに必須Cookieがありません。個人Cookieを読み込むと、この設定を上書きできます。";
      case "expired":
        return "運用者が管理する共有Cookieの有効期限が切れています。個人Cookieを読み込むと、この設定を上書きできます。";
      case "unavailable":
      default:
        return "運用者が管理する共有Cookieを利用できません。運用者の設定を確認してください。個人Cookieを読み込むと、この設定を上書きできます。";
    }
  }
  return null;
}

function feedbackForStatus(status: CookieStatus, action: "upload" | "delete"): string {
  if (action === "delete") return "Cookie設定を削除できませんでした。もう一度お試しください。";
  switch (status) {
    case "invalid_format":
      return "Cookieファイルの形式を確認できませんでした。別のファイルを選択してください。";
    case "missing_required_cookie":
      return "必須Cookieが見つかりませんでした。auth_token と ct0 を含むファイルを選択してください。";
    case "expired":
      return "Cookieの有効期限が切れています。再ログインして取得し直してください。";
    case "unavailable":
      return "Cookie設定を更新できませんでした。しばらくしてから再試行してください。";
    case "unconfigured":
    case "available":
    default:
      return "Cookie設定を更新できませんでした。ファイルを確認して再試行してください。";
  }
}

export function CookieManagementSection() {
  const confirm = useConfirm();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const statusRef = useRef<XCookieStatusResponse | null>(null);
  const feedbackTimerRef = useRef<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [status, setStatus] = useState<XCookieStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"upload" | "delete" | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [dragActive, setDragActive] = useState(false);

  const setSafeStatus = useCallback((value: unknown) => {
    const next = normalizeXCookieStatus(value);
    statusRef.current = next;
    setStatus(next);
    return next;
  }, []);

  const showFeedback = useCallback((next: Feedback | null) => {
    if (feedbackTimerRef.current !== null && typeof window !== "undefined") {
      window.clearTimeout(feedbackTimerRef.current);
      feedbackTimerRef.current = null;
    }
    setFeedback(next);
    if (next && typeof window !== "undefined") {
      feedbackTimerRef.current = window.setTimeout(() => {
        feedbackTimerRef.current = null;
        setFeedback(null);
      }, 6_000);
    }
  }, []);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch(X_COOKIE_ENDPOINT, {
        credentials: "include",
        cache: "no-store",
      });
      const payload = await readSafeJson(response);
      if (!response.ok) throw new Error("cookie-status-request-failed");
      setSafeStatus(payload);
      showFeedback(null);
    } catch {
      if (!statusRef.current) {
        setSafeStatus({ status: "unavailable", configured: false, source: "none" });
      }
      showFeedback({
        kind: "error",
        message: "Cookie設定を確認できませんでした。再試行してください。",
      });
    } finally {
      setLoading(false);
    }
  }, [setSafeStatus, showFeedback]);

  useEffect(() => {
    void loadStatus();
    return () => {
      if (feedbackTimerRef.current !== null && typeof window !== "undefined") {
        window.clearTimeout(feedbackTimerRef.current);
      }
    };
  }, [loadStatus]);

  const resetFileInput = useCallback(() => {
    if (inputRef.current) inputRef.current.value = "";
  }, []);

  const handleFile = useCallback(
    async (file: File | null) => {
      if (!file || busy) {
        resetFileInput();
        return;
      }
      setBusy("upload");
      showFeedback(null);
      try {
        if (file.size > MAX_COOKIE_FILE_BYTES) {
          showFeedback({
            kind: "error",
            message: "ファイルが大きすぎます。2MB以下のファイルを選択してください。",
          });
          return;
        }
        const contents = await readFileContents(file);
        const prepared = prepareXCookieUpload(contents, { filename: file.name });
        const response = await fetch(X_COOKIE_ENDPOINT, {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/octet-stream" },
          body: prepared.body,
        });
        const payload = await readSafeJson(response);
        const returnedStatus = statusFromPayload(payload);
        if (!response.ok || returnedStatus !== "available") {
          showFeedback({
            kind: "error",
            message: feedbackForStatus(returnedStatus ?? "unavailable", "upload"),
          });
          return;
        }
        setSafeStatus(payload);
        showFeedback({ kind: "success", message: "XのCookie設定を更新しました。" });
      } catch (error) {
        showFeedback({
          kind: "error",
          message:
            error instanceof CookieUploadPreparationError
              ? error.message
              : "Cookie設定を更新できませんでした。ファイルを確認して再試行してください。",
        });
      } finally {
        setBusy(null);
        resetFileInput();
      }
    },
    [busy, resetFileInput, setSafeStatus, showFeedback],
  );

  const handleDelete = useCallback(async () => {
    const deleteDescription =
      statusRef.current?.source === "server_shared"
        ? "このユーザーでは運用共有のCookieフォールバックを無効にします。"
        : "保存済みの個人Cookieを削除します。このユーザーでは運用共有のフォールバックも無効になります。";
    const accepted = await confirm({
      title: "XのCookie設定を削除しますか？",
      description: deleteDescription,
      confirmLabel: "削除する",
      cancelLabel: "キャンセル",
      destructive: true,
    });
    if (!accepted || busy) return;
    setBusy("delete");
    showFeedback(null);
    try {
      const response = await fetch(X_COOKIE_ENDPOINT, {
        method: "DELETE",
        credentials: "include",
      });
      const payload = await readSafeJson(response);
      if (!response.ok) throw new Error("cookie-delete-request-failed");
      const next = setSafeStatus(payload);
      if (next.status !== "unconfigured") {
        showFeedback({ kind: "error", message: feedbackForStatus(next.status, "delete") });
        return;
      }
      showFeedback({ kind: "success", message: "XのCookie設定を削除しました。共有フォールバックも無効です。" });
    } catch {
      // The previous status is deliberately retained when DELETE fails.
      showFeedback({ kind: "error", message: feedbackForStatus("unavailable", "delete") });
    } finally {
      setBusy(null);
    }
  }, [busy, confirm, setSafeStatus, showFeedback]);

  const statusCopy = status
    ? cookieStatusLabel(status.status)
    : loading
      ? "確認中"
      : "利用できません";
  // Keep a delete action for invalid/expired personal credentials too: the
  // backend deliberately reports safe metadata (configured=false) for those,
  // but users still need a way to remove the stale credential. A disabled
  // tombstone (`personal` + `unconfigured`) has nothing left to delete.
  const canDelete = Boolean(
    status &&
      status.status !== "unconfigured" &&
      (status.source === "personal" || status.source === "server_shared"),
  );
  const sourceCopy = status ? sourceDescription(status) : null;

  return (
    <SettingsDisclosure
      id="cookie-management"
      targetId="cookie-management"
      title="Cookie管理"
      icon={<FileKey2 className="size-4" />}
      open={expanded}
      onOpenChange={setExpanded}
      summary={<Badge variant={status?.status === "available" ? "default" : "secondary"}>{statusCopy}</Badge>}
    >
      <div className="space-y-4" data-settings-surface="cookie-management">
        <div className="space-y-2" aria-label="Cookie管理サービス一覧">
          <p className="text-xs text-muted-foreground">
            サービスごとのCookieを管理します。現在は次のサービスに対応しています。
          </p>
          {COOKIE_SERVICE_DEFINITIONS.map((service) => (
            <div key={service.id} data-cookie-service={service.id} className="flex items-center gap-2">
              <h3 id={`cookie-management-service-${service.id}`} className="text-sm font-semibold">{service.label}</h3>
              <Badge variant="outline">サービス</Badge>
            </div>
          ))}
        </div>
        <div className="space-y-1 text-sm">
          <p>
            状態: <span className="font-medium">{statusCopy}</span>
          </p>
          {sourceCopy ? <p className="text-xs text-muted-foreground">{sourceCopy}</p> : null}
          <p className="text-xs text-muted-foreground">{status ? actionForStatus(status.status) : "設定を確認しています。"}</p>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            <Loader2 className="size-4 animate-spin" />
            Cookie設定を確認中...
          </div>
        ) : null}

        <div
          className={`rounded-md border border-dashed p-4 text-center transition-colors ${dragActive ? "border-primary bg-primary/5" : ""}`}
          role="button"
          tabIndex={0}
          aria-label="Cookieファイルを選択またはドロップ"
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              inputRef.current?.click();
            }
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (event.currentTarget === event.target) setDragActive(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragActive(false);
            void handleFile(event.dataTransfer.files?.[0] ?? null);
          }}
        >
          <input
            ref={inputRef}
            id="x-cookie-file"
            type="file"
            className="sr-only"
            aria-label="Cookieファイルを選択"
            accept=".txt,.cookies,.har,text/plain,application/octet-stream,application/json"
            onClick={(event) => event.stopPropagation()}
            onChange={(event) => void handleFile(event.currentTarget.files?.[0] ?? null)}
          />
          <UploadCloud className="mx-auto size-6 text-muted-foreground" aria-hidden="true" />
          <p className="mt-2 text-sm font-medium">Cookieファイルをドロップ</p>
          <p className="mt-1 text-xs text-muted-foreground">またはクリックして選択（Netscape / Chrome・Edge HAR、2MB以下）</p>
          <p className="mt-2 text-[11px] text-muted-foreground">ファイル名やCookie値は画面に表示しません。</p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => void loadStatus()} disabled={loading || busy !== null}>
            {loading ? <Loader2 className="mr-1 size-3 animate-spin" /> : <RefreshCw className="mr-1 size-3" />}
            再試行
          </Button>
          {canDelete ? (
            <Button type="button" variant="outline" size="sm" onClick={() => void handleDelete()} disabled={busy !== null}>
              {busy === "delete" ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Trash2 className="mr-1 size-3" />}
              {status?.source === "server_shared" ? "共有フォールバックを無効化" : "個人Cookieを削除"}
            </Button>
          ) : null}
        </div>

        {feedback ? (
          <p role={feedback.kind === "error" ? "alert" : "status"} className={feedback.kind === "error" ? "text-xs text-destructive" : "text-xs text-green-600 dark:text-green-400"}>
            {feedback.message}
          </p>
        ) : null}

        <details className="rounded-md border p-3">
          <summary className="cursor-pointer text-sm font-medium">取得手順（HttpOnly対応）</summary>
          <div className="mt-3 space-y-3 text-xs text-muted-foreground">
            <ol className="list-decimal space-y-2 pl-5">
              <li>ChromeまたはEdgeで x.com にログインします。</li>
              <li>F12でNetworkを開き、ドメインフィルターに x.com または twitter.com を指定して再読み込みします。対象リクエストの Cookie ヘッダーに auth_token と ct0 があることを確認してください。</li>
              <li>DevToolsのSettings（歯車）→Preferences→Networkで「Allow to generate HAR with sensitive data」を有効にします。</li>
              <li>ドメインフィルターで絞り込んだ対象リクエストを選び、「Save all listed as HAR with sensitive data」でHARを保存し、すぐにこの画面へドロップまたは選択します。</li>
            </ol>
            <p>Consoleの <code>document.cookie</code> は使えません。auth_tokenは通常HttpOnlyのため、Consoleからは見えません。</p>
            <p>HARはこのブラウザ内だけで処理し、HAR全体をサーバーへアップロードしません。抽出した必須CookieだけがAoiTalkサーバーへ送信され、暗号化して保存されます。</p>
            <p className="font-medium text-foreground">Cookieはパスワードと同等です。共有・貼り付け・他サービスへのアップロードはしないでください。取り込み後はHARを削除し、必要ならログアウト・Cookieのローテーション・専用アカウントの利用を検討してください。</p>
            <p>Netscape形式のCookieエクスポートファイルにも対応しています。</p>
          </div>
        </details>
      </div>
    </SettingsDisclosure>
  );
}
