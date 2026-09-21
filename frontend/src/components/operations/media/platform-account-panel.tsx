"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import {
  Ban,
  CheckCircle2,
  FileKey2,
  History,
  Loader2,
  Plus,
  RefreshCw,
  RotateCw,
  ShieldCheck,
  UploadCloud,
} from "lucide-react";

import {
  mediaOperationsSetupApi,
  type CredentialConnectionType,
  type PlatformAccount,
  type PlatformCredentialAuditEvent,
  type PlatformCredentialCapabilities,
  type PlatformCredentialDetail,
} from "@/lib/media-operations-setup-api";
import type { MediaPlatform } from "@/lib/media-operations-api";
import { AppSelect } from "@/components/ui/app-select";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";

const MAX_PACKAGE_FILE_BYTES = 2 * 1024 * 1024;

const PLATFORMS: readonly MediaPlatform[] = [
  "x",
  "pixiv",
  "dlsite",
  "patreon",
  "youtube",
  "instagram",
];

const CONNECTION_TYPES: readonly {
  value: CredentialConnectionType;
  label: string;
}[] = [
  { value: "cookie_export", label: "Cookie" },
  { value: "api_token", label: "API token" },
  { value: "oauth", label: "OAuth" },
];

const CAPABILITY_LABELS: Record<string, string> = {
  available: "利用可能",
  configured: "設定済み",
  unknown: "不明",
  unsupported: "非対応",
  unavailable: "利用不可",
};

const STATUS_LABELS: Record<string, string> = {
  unknown: "不明",
  not_configured: "未設定",
  pending: "確認待ち",
  verification_pending: "確認待ち",
  configured: "設定済み",
  verified: "確認済み",
  invalid: "無効",
  disabled: "無効化済み",
  unavailable: "利用不可",
  unsupported: "非対応",
  key_unavailable: "鍵を利用できません",
};

type CredentialLoadState = {
  credential: PlatformCredentialDetail | null;
  loading: boolean;
  error?: unknown;
};

function newIdempotencyKey(prefix = "platform-credential"): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }

  return `${prefix}-${Date.now()}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

function statusLabel(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "不明";
  return STATUS_LABELS[value] ?? value;
}

function capabilityLabel(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "不明";
  return CAPABILITY_LABELS[value] ?? value;
}

function readableError(error: unknown): string {
  if (
    error instanceof Error &&
    (error.message.includes("2MB以下") ||
      error.message.includes("接続パッケージファイルを選択") ||
      error.message.includes("Characterを選択") ||
      error.message.includes("account refと表示名"))
  ) {
    return error.message;
  }
  if (
    typeof error === "object" &&
    error !== null &&
    "status" in error
  ) {
    const status = Number((error as { status?: unknown }).status);
    if (status === 401) return "ログイン状態を確認してください。";
    if (status === 403) return "このCharacterの接続を操作する権限がありません。";
    if (status === 404) return "接続または認証情報が見つかりません。";
    if (status === 409) return "状態が更新されています。最新状態を読み込んで再試行してください。";
    if (status === 413) return "ファイルが大きすぎます。2MB以下のファイルを選択してください。";
    if (status === 422) return "入力または認証情報の形式を確認してください。";
  }
  return "外部接続の処理に失敗しました。しばらくしてから再試行してください。";
}

function accountName(account: PlatformAccount): string {
  const revision = account.current_revision;
  return revision?.display_name || account.account_ref || "Platform account";
}

function accountCapabilities(
  account: PlatformAccount,
): PlatformCredentialCapabilities {
  const revision = account.current_revision;
  return {
    identity: "unknown",
    publish: revision?.publish_capability ?? "unknown",
    media: revision?.media_capability ?? "unknown",
    analytics: revision?.analytics_capability ?? "unknown",
  };
}

function capabilityRows(
  capabilities: PlatformCredentialCapabilities,
): readonly [string, string][] {
  return [
    ["本人確認", capabilityLabel(capabilities.identity)],
    ["投稿", capabilityLabel(capabilities.publish)],
    ["メディア", capabilityLabel(capabilities.media)],
    ["分析", capabilityLabel(capabilities.analytics)],
  ];
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("ja-JP", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function auditStatus(event: PlatformCredentialAuditEvent): string | null {
  if (typeof event.status === "string" && event.status.trim()) {
    return event.status;
  }
  const snapshot = event.snapshot;
  const value = snapshot?.status;
  return typeof value === "string" && value.trim() ? value : null;
}

function auditRevision(event: PlatformCredentialAuditEvent): number | null {
  if (typeof event.revision === "number") return event.revision;
  const value = event.snapshot?.revision;
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isTooLarge(file: File): boolean {
  return file.size > MAX_PACKAGE_FILE_BYTES;
}

function packageFileError(file: File | null): Error | null {
  if (!file) return new Error("接続パッケージファイルを選択してください。");
  if (isTooLarge(file)) {
    return new Error("ファイルが大きすぎます。2MB以下のファイルを選択してください。");
  }
  return null;
}

function errorStatus(error: unknown): number | null {
  if (typeof error !== "object" || error === null || !("status" in error)) {
    return null;
  }
  const value = Number((error as { status?: unknown }).status);
  return Number.isFinite(value) ? value : null;
}

function AccountStatus({ account }: { account: PlatformAccount }) {
  return (
    <span className="rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">
      {statusLabel(account.status)}
    </span>
  );
}

function CredentialStatus({
  credential,
  error,
}: {
  credential: PlatformCredentialDetail | null;
  error?: unknown;
}) {
  if (error) {
    return (
      <span
        className="rounded-full border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-[11px] text-destructive"
        data-credential-status="error"
      >
        認証状態を取得できません
      </span>
    );
  }
  return (
    <span
      className="rounded-full border border-border bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground"
      data-credential-status={credential?.status ?? "not_configured"}
    >
      認証 {statusLabel(credential?.status ?? "not_configured")}
    </span>
  );
}

function CredentialCapabilities({
  capabilities,
}: {
  capabilities: PlatformCredentialCapabilities;
}) {
  return (
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
      {capabilityRows(capabilities).map(([label, value]) => (
        <div key={label} className="flex items-center justify-between gap-2">
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function AuditHistory({
  events,
}: {
  events: PlatformCredentialAuditEvent[];
}) {
  if (!events.length) {
    return <p className="text-xs text-muted-foreground">監査履歴はありません。</p>;
  }

  return (
    <ul className="space-y-1.5" data-testid="credential-audit-list">
      {events.map((event) => (
        <li
          key={event.id}
          className="flex flex-wrap items-center justify-between gap-2 rounded border border-border/60 px-2 py-1.5 text-[11px]"
        >
          <span className="font-medium">{event.event_type}</span>
          <span className="text-muted-foreground">
            {auditStatus(event) ? statusLabel(auditStatus(event)) : "—"}
            {auditRevision(event) != null ? ` · rev ${auditRevision(event)}` : ""}
            {` · ${formatDate(event.created_at)}`}
          </span>
        </li>
      ))}
    </ul>
  );
}

export type PlatformAccountPanelProps = {
  characterId: string;
  onChanged?: () => void;
};

export function PlatformAccountPanel({
  characterId,
  onChanged,
}: PlatformAccountPanelProps) {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [credentials, setCredentials] = useState<Record<string, CredentialLoadState>>({});
  const [audits, setAudits] = useState<Record<string, PlatformCredentialAuditEvent[]>>({});
  const [auditOpen, setAuditOpen] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [dragActive, setDragActive] = useState(false);
  const [platform, setPlatform] = useState<MediaPlatform>("x");
  const [accountRef, setAccountRef] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [connectionType, setConnectionType] = useState<CredentialConnectionType>("cookie_export");
  const packageFileRef = useRef<File | null>(null);
  const [packageSelected, setPackageSelected] = useState(false);
  const packageInputRef = useRef<HTMLInputElement | null>(null);
  const rotateInputRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const requestIdRef = useRef(0);
  const generationRef = useRef(0);

  const loadCredentials = useCallback(async (nextAccounts: PlatformAccount[]) => {
    const requestId = requestIdRef.current;
    const initial: Record<string, CredentialLoadState> = {};
    nextAccounts.forEach((account) => {
      initial[account.id] = { credential: null, loading: true };
    });
    setCredentials(initial);

    await Promise.all(
      nextAccounts.map(async (account) => {
        try {
          const credential = await mediaOperationsSetupApi.getPlatformCredential(account.id);
          if (requestId !== requestIdRef.current) return;
          setCredentials((current) => ({
            ...current,
            [account.id]: { credential, loading: false },
          }));
        } catch (nextError) {
          if (requestId !== requestIdRef.current) return;
          if (errorStatus(nextError) === 404) {
            // A missing credential is a valid unconfigured state.
            setCredentials((current) => ({
              ...current,
              [account.id]: { credential: null, loading: false },
            }));
            return;
          }
          setCredentials((current) => ({
            ...current,
            [account.id]: { credential: null, loading: false, error: nextError },
          }));
          setError(nextError);
        }
      }),
    );
  }, []);

  const loadAccounts = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);

    // Character is required for this scoped projection.  Avoid issuing a
    // broad account list request while navigation is between selections.
    if (!characterId.trim()) {
      setAccounts([]);
      setCredentials({});
      setAudits({});
      setAuditOpen({});
      setLoading(false);
      return;
    }

    try {
      const nextAccounts = await mediaOperationsSetupApi.listPlatformAccounts(
        null,
        characterId,
      );
      if (requestId !== requestIdRef.current) return;
      setAccounts(nextAccounts);
      setAudits({});
      setAuditOpen({});
      await loadCredentials(nextAccounts);
    } catch (nextError) {
      if (requestId === requestIdRef.current) setError(nextError);
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [characterId, loadCredentials]);

  const clearPackageFile = useCallback(() => {
    packageFileRef.current = null;
    setPackageSelected(false);
    if (packageInputRef.current) packageInputRef.current.value = "";
  }, []);

  const clearRotateInputs = useCallback(() => {
    Object.values(rotateInputRefs.current).forEach((input) => {
      if (input) input.value = "";
    });
  }, []);

  const acceptPackageFile = useCallback((file: File | null) => {
    if (!file) return;
    const nextError = packageFileError(file);
    if (nextError) {
      setError(nextError);
      clearPackageFile();
      return;
    }
    setError(null);
    packageFileRef.current = file;
    setPackageSelected(true);
  }, [clearPackageFile]);

  const onPackageChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    acceptPackageFile(event.currentTarget.files?.[0] ?? null);
  }, [acceptPackageFile]);

  const onPackageDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    acceptPackageFile(event.dataTransfer.files?.[0] ?? null);
  }, [acceptPackageFile]);

  useEffect(() => {
    generationRef.current += 1;
    requestIdRef.current += 1;
    // Clear the previous Character's projection synchronously. The deferred
    // reload below must never leave a stale account or selected package in a
    // render after the Character id changes.
    setBusyAction(null);
    setAccounts([]);
    setCredentials({});
    setAudits({});
    setAuditOpen({});
    setError(null);
    setAccountRef("");
    setDisplayName("");
    setPlatform("x");
    setConnectionType("cookie_export");
    clearPackageFile();
    clearRotateInputs();
    const task = window.setTimeout(() => {
      void loadAccounts();
    }, 0);
    return () => window.clearTimeout(task);
  }, [characterId, clearPackageFile, clearRotateInputs, loadAccounts]);

  const createConnection = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    if (!characterId.trim()) {
      setError(new Error("Characterを選択してください。"));
      return;
    }
    if (!accountRef.trim() || !displayName.trim()) {
      setError(new Error("account refと表示名を入力してください。"));
      return;
    }
    const file = packageFileRef.current;
    const fileError = packageFileError(file);
    if (fileError) {
      setError(fileError);
      return;
    }
    if (!file) return;

    setBusyAction("create");
    setError(null);
    try {
      await mediaOperationsSetupApi.addCharacterPlatformConnection(
        characterId,
        {
          platform,
          account_ref: accountRef.trim(),
          display_name: displayName.trim(),
          connection_type: connectionType,
          package: file,
        },
        newIdempotencyKey("platform-connection"),
      );
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      setAccountRef("");
      setDisplayName("");
      clearPackageFile();
      await loadAccounts();
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        onChanged?.();
      }
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
        clearPackageFile();
      }
    }
  };

  const withCredential = useCallback(
    (accountId: string): PlatformCredentialDetail | null =>
      credentials[accountId]?.credential ?? null,
    [credentials],
  );

  const replaceCredential = useCallback((accountId: string, credential: PlatformCredentialDetail) => {
    setCredentials((current) => ({
      ...current,
      [accountId]: { credential, loading: false },
    }));
  }, []);

  const verifyCredential = async (account: PlatformAccount) => {
    const credential = withCredential(account.id);
    if (!credential) return;
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    const action = `verify:${account.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const result = await mediaOperationsSetupApi.verifyPlatformCredential(
        account.id,
        credential.revision,
        newIdempotencyKey("credential-verify"),
      );
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      replaceCredential(account.id, result);
      onChanged?.();
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
      }
    }
  };

  const disableCredential = async (account: PlatformAccount) => {
    const credential = withCredential(account.id);
    if (!credential) return;
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    const action = `disable:${account.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const result = await mediaOperationsSetupApi.disablePlatformCredential(
        account.id,
        credential.revision,
        newIdempotencyKey("credential-disable"),
      );
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      replaceCredential(account.id, result);
      onChanged?.();
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
      }
    }
  };

  const createCredential = async (account: PlatformAccount, file: File | null) => {
    if (!file) return;
    const input = rotateInputRefs.current[account.id];
    const fileError = packageFileError(file);
    if (fileError) {
      setError(fileError);
      if (input) input.value = "";
      return;
    }
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    const action = `create-credential:${account.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const result = await mediaOperationsSetupApi.createPlatformCredential(
        account.id,
        { package: file, connection_type: connectionType },
        newIdempotencyKey("credential-create"),
      );
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      replaceCredential(account.id, result);
      onChanged?.();
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
        if (input) input.value = "";
      }
    }
  };

  const rotateCredential = async (account: PlatformAccount, file: File | null) => {
    const credential = withCredential(account.id);
    if (!credential || !file) return;
    const fileError = packageFileError(file);
    if (fileError) {
      setError(fileError);
      const input = rotateInputRefs.current[account.id];
      if (input) input.value = "";
      return;
    }
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    const action = `rotate:${account.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const result = await mediaOperationsSetupApi.rotatePlatformCredential(
        account.id,
        {
          package: file,
          expected_revision: credential.revision,
          connection_type: credential.connection_type,
        },
        newIdempotencyKey("credential-rotate"),
      );
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      replaceCredential(account.id, result);
      onChanged?.();
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
        const input = rotateInputRefs.current[account.id];
        if (input) input.value = "";
      }
    }
  };

  const showAudit = async (account: PlatformAccount) => {
    const isOpen = auditOpen[account.id] === true;
    if (isOpen) {
      setAuditOpen((current) => ({ ...current, [account.id]: false }));
      return;
    }
    const generation = generationRef.current;
    const actionCharacterId = characterId;
    const action = `audit:${account.id}`;
    setBusyAction(action);
    setError(null);
    try {
      const events = await mediaOperationsSetupApi.listPlatformCredentialAudit(account.id);
      if (
        generationRef.current !== generation ||
        characterId !== actionCharacterId
      ) {
        return;
      }
      setAudits((current) => ({ ...current, [account.id]: events }));
      setAuditOpen((current) => ({ ...current, [account.id]: true }));
    } catch (nextError) {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setError(nextError);
      }
    } finally {
      if (
        generationRef.current === generation &&
        characterId === actionCharacterId
      ) {
        setBusyAction(null);
      }
    }
  };

  return (
    <Card size="sm" data-testid="platform-account-panel">
      <CardHeader className="border-b border-border/70">
        <CardTitle className="flex items-center gap-2 text-sm">
          <FileKey2 className="size-4" aria-hidden="true" />
          外部接続と認証情報
        </CardTitle>
        <CardDescription>
          Characterごとの安全な接続を管理します。認証パッケージは暗号化して保存し、画面には状態だけを表示します。
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground">
            {accounts.length}件のCharacter接続
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void loadAccounts()}
            disabled={loading || busyAction !== null}
          >
            <RefreshCw className={loading ? "mr-1 size-3.5 animate-spin" : "mr-1 size-3.5"} />
            更新
          </Button>
        </div>

        {loading && !accounts.length ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground" role="status">
            <Loader2 className="size-4 animate-spin" /> 読み込み中
          </div>
        ) : accounts.length ? (
          <div className="space-y-3" data-testid="platform-account-list">
            {accounts.map((account) => {
              const credentialState = credentials[account.id];
              const credential = credentialState?.credential ?? null;
              const capabilities = credential?.capabilities ?? accountCapabilities(account);
              const isBusy = busyAction?.endsWith(`:${account.id}`) ?? false;
              const credentialBlocked = credentialState?.error != null;
              return (
                <article key={account.id} className="rounded-md border border-border/70 px-3 py-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h3 className="truncate text-sm font-medium">{accountName(account)}</h3>
                        <AccountStatus account={account} />
                        {credentialState?.loading ? (
                          <span className="text-[11px] text-muted-foreground">認証状態を確認中…</span>
                        ) : (
                          <CredentialStatus credential={credential} error={credentialState?.error} />
                        )}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {account.platform} · {account.account_ref}
                      </p>
                    </div>
                    {credential ? (
                      <div className="text-right text-[10px] text-muted-foreground">
                        <p>最終確認 {formatDate(credential.last_verified_at)}</p>
                        <p>更新 {formatDate(credential.updated_at)}</p>
                      </div>
                    ) : null}
                  </div>

                  <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto]">
                    <CredentialCapabilities capabilities={capabilities} />
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => void verifyCredential(account)}
                        disabled={!credential || credentialBlocked || credential?.status === "disabled" || busyAction !== null || isBusy}
                      >
                        {isBusy && busyAction === `verify:${account.id}` ? <Loader2 className="mr-1 size-3 animate-spin" /> : <ShieldCheck className="mr-1 size-3" />}
                        検証
                      </Button>
                      <input
                        ref={(element) => {
                          rotateInputRefs.current[account.id] = element;
                        }}
                        id={`platform-account-rotate-${account.id}`}
                        type="file"
                        className="sr-only"
                        accept=".json,.txt,.har,.cookie,.cookies,application/json,text/plain,application/octet-stream"
                        aria-label={credential ? "接続パッケージファイルをローテーション" : "接続パッケージファイルを添付"}
                        onChange={(event) => {
                          const file = event.currentTarget.files?.[0] ?? null;
                          if (credential) {
                            void rotateCredential(account, file);
                          } else {
                            void createCredential(account, file);
                          }
                        }}
                      />
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => rotateInputRefs.current[account.id]?.click()}
                        disabled={credentialBlocked || credentialState?.loading === true || busyAction !== null || isBusy}
                      >
                        {isBusy && (busyAction === `rotate:${account.id}` || busyAction === `create-credential:${account.id}`) ? <Loader2 className="mr-1 size-3 animate-spin" /> : credential ? <RotateCw className="mr-1 size-3" /> : <Plus className="mr-1 size-3" />}
                        {credential ? "ローテーション" : "認証情報を追加"}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => void disableCredential(account)}
                        disabled={!credential || credentialBlocked || credential?.status === "disabled" || busyAction !== null || isBusy}
                      >
                        {isBusy && busyAction === `disable:${account.id}` ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Ban className="mr-1 size-3" />}
                        無効化
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => void showAudit(account)}
                        disabled={credentialBlocked || credentialState?.loading === true || !credential || busyAction !== null || isBusy}
                      >
                        {isBusy && busyAction === `audit:${account.id}` ? <Loader2 className="mr-1 size-3 animate-spin" /> : <History className="mr-1 size-3" />}
                        監査履歴
                      </Button>
                    </div>
                  </div>

                  {auditOpen[account.id] ? (
                    <div className="mt-3 border-t border-border/60 pt-3">
                      <AuditHistory events={audits[account.id] ?? []} />
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : (
          <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
            このCharacterに接続はまだありません。
          </p>
        )}

        <form
          className="space-y-3 rounded-md border border-border/70 p-3"
          aria-label="Character接続追加フォーム"
          onSubmit={createConnection}
        >
          <div className="flex items-center gap-2">
            <Plus className="size-4 text-primary" aria-hidden="true" />
            <h3 className="text-sm font-medium">接続を追加</h3>
          </div>
          <p className="text-[11px] leading-4 text-muted-foreground">
            パッケージの内容はこの画面に表示せず、サーバー側で検証・暗号化します。2MBを超えるファイルは送信しません。
          </p>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-xs">
              <span>Platform</span>
              <AppSelect
                value={platform}
                onChange={(event) => setPlatform(event.target.value as MediaPlatform)}
              >
                {PLATFORMS.map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </AppSelect>
            </label>

            <label className="space-y-1 text-xs">
              <span>接続方式</span>
              <AppSelect
                value={connectionType}
                onChange={(event) => setConnectionType(event.target.value as CredentialConnectionType)}
              >
                {CONNECTION_TYPES.map(({ value, label }) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </AppSelect>
            </label>

            <label htmlFor="media-platform-account-ref" className="space-y-1 text-xs">
              <span>Account reference</span>
              <Input
                id="media-platform-account-ref"
                value={accountRef}
                onChange={(event) => setAccountRef(event.target.value)}
                maxLength={255}
                required
              />
            </label>

            <label htmlFor="media-platform-account-name" className="space-y-1 text-xs">
              <span>表示名</span>
              <Input
                id="media-platform-account-name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                maxLength={255}
                required
              />
            </label>
          </div>

          <div
            className={`rounded-md border border-dashed p-4 text-center transition-colors ${dragActive ? "border-primary bg-primary/5" : ""}`}
            role="button"
            tabIndex={0}
            aria-label="接続パッケージファイルを選択またはドロップ"
            onClick={() => packageInputRef.current?.click()}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                packageInputRef.current?.click();
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
            onDrop={onPackageDrop}
          >
            <input
              ref={packageInputRef}
              id="media-platform-connection-package"
              type="file"
              className="sr-only"
              aria-label="接続パッケージファイルを選択"
              accept=".json,.txt,.har,.cookie,.cookies,application/json,text/plain,application/octet-stream"
              onClick={(event) => event.stopPropagation()}
              onChange={onPackageChange}
            />
            <UploadCloud className="mx-auto size-6 text-muted-foreground" aria-hidden="true" />
            <p className="mt-2 text-sm font-medium">接続パッケージをドロップ</p>
            <p className="mt-1 text-xs text-muted-foreground">またはクリックして選択（2MB以下）</p>
            {packageSelected ? (
              <p className="mt-2 flex items-center justify-center gap-1 text-xs text-muted-foreground">
                <CheckCircle2 className="size-3.5 text-green-600" aria-hidden="true" />
                ファイルを選択しました（内容は表示しません）
              </p>
            ) : (
              <p className="mt-2 text-[11px] text-muted-foreground">ファイル名とパッケージ内容は画面に表示しません。</p>
            )}
          </div>

          <Button type="submit" size="sm" disabled={busyAction !== null || !packageSelected}>
            {busyAction === "create" ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <Plus className="mr-1 size-3.5" />}
            接続を追加
          </Button>
        </form>

        {error ? (
          <div role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {readableError(error)}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
