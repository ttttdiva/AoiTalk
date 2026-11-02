"use client";

import { useState } from "react";
import { Loader2, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  mediaOperationsSetupApi,
  type PlatformAccount,
} from "@/lib/media-operations-setup-api";
import { PersonaAccountConnection, usePlatformCredential } from "@/components/operations/media/persona-account-connection";
import { youtubeChannelRegistered } from "@/lib/persona-connection-state";
import type { PersonaWorkspaceScope } from "@/lib/persona-workspace-model";

const YOUTUBE_CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/u;

function newIdempotencyKey(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : "youtube-channel-" + Date.now() + "-" + Math.random().toString(36).slice(2);
}

function readableError(error: unknown): string {
  if (typeof error === "object" && error !== null && "status" in error) {
    const status = Number((error as { status?: unknown }).status);
    if (status === 401 || status === 403) return "この接続先を変更する権限がありません。";
    if (status === 404) return "接続先が見つかりません。最新状態を読み込んでください。";
    if (status === 409) return "接続先が別の操作で更新されました。最新状態を読み込んでください。";
    if (status === 422) return "YouTube channel IDの形式かslotの状態を確認してください。";
  }
  return "接続先を更新できませんでした。最新状態を読み込んでください。";
}

/**
 * One persona's YouTube slot: register the real channel ID, activate the slot,
 * then 本人接続 (OAuth). Registration only changes the ID and slot state.
 */
export function YouTubeChannelSlot({
  scope,
  account,
  onUpdated,
}: {
  scope: PersonaWorkspaceScope;
  account: PlatformAccount;
  onUpdated: () => Promise<unknown>;
}) {
  const credential = usePlatformCredential(scope.ownerUserId, account.id);
  const registered = youtubeChannelRegistered(account);
  const [channelId, setChannelId] = useState(registered ? account.account_ref : "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (registered && account.status === "active") return <PersonaAccountConnection scope={scope} account={account} />;

  // Identity changes need a confirmed absence of credentials; an unreadable state keeps them disabled.
  const locked = Boolean(credential.data) || Boolean(account.connection_id);
  const allowMutation = credential.data === null && !locked && !saving;
  const placeholder = account.account_ref.startsWith("sns-v6:");
  const canRegister = !registered && placeholder && YOUTUBE_CHANNEL_ID.test(channelId) && allowMutation;

  const update = async (activate: boolean) => {
    setSaving(true);
    setError(null);
    try {
      await mediaOperationsSetupApi.updateYouTubeChannelSlot(
        account.id,
        {
          expected_version: account.current_revision.version,
          channel_id: activate ? account.account_ref : channelId,
          activate,
        },
        newIdempotencyKey(),
      );
      await onUpdated();
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-w-0 space-y-2" data-testid={"youtube-channel-slot-" + account.id}>
      <p className="break-words text-xs font-medium">
        YouTube の本人接続 · {registered ? account.account_ref : "channel ID 未登録"}
      </p>
      <ol className="flex flex-wrap gap-x-3 text-[11px] text-muted-foreground" aria-label="YouTube 接続の段階">
        {["channel ID を登録", "枠を有効化", "本人接続"].map((label, index) => {
          const current = registered ? 1 : 0;
          return <li key={label} aria-current={index === current ? "step" : undefined} className={index === current ? "font-semibold text-foreground" : index < current ? "line-through" : undefined}>{index + 1}. {label}</li>;
        })}
      </ol>
      {registered ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">本人接続の前に、この枠を有効化してください。</span>
          <Button type="button" size="sm" disabled={!allowMutation} onClick={() => void update(true)}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            slotを有効化
          </Button>
        </div>
      ) : (
        <form
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
          onSubmit={(event) => {
            event.preventDefault();
            if (canRegister) void update(false);
          }}
        >
          <div className="min-w-0 flex-1">
            <label className="mb-1 block text-xs text-muted-foreground" htmlFor={"youtube-channel-id-" + account.id}>
              YouTube channel ID（本人接続の前に登録）
            </label>
            <Input
              id={"youtube-channel-id-" + account.id}
              value={channelId}
              maxLength={24}
              autoComplete="off"
              spellCheck={false}
              placeholder="UCから始まる24文字"
              aria-invalid={Boolean(channelId) && !YOUTUBE_CHANNEL_ID.test(channelId)}
              onChange={(event) => setChannelId(event.target.value.trim())}
              disabled={!allowMutation || !placeholder}
              className="h-8 font-mono text-xs"
            />
          </div>
          <Button type="submit" size="sm" disabled={!canRegister}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
            channel IDを登録
          </Button>
        </form>
      )}
      {locked ? (
        <p className="text-xs text-muted-foreground">この枠には既存の認証情報があるため、この画面から変更できません。</p>
      ) : null}
      {credential.error ? <p role="alert" className="text-xs text-destructive">認証状態を取得できません。状態を再取得してください。</p> : null}
      {error ? <p role="alert" className="text-xs text-destructive">{error}</p> : null}
    </div>
  );
}
