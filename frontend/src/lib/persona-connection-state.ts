import type { PlatformAccount, PlatformCredentialDetail } from "./media-operations-setup-api";

/** Only X and YouTube have an official 本人接続 (OAuth) path; other slots are placeholders. */
export const CONNECTABLE_PLATFORMS = ["x", "youtube"] as const;
export type ConnectablePlatform = typeof CONNECTABLE_PLATFORMS[number];
export type ConnectionTone = "ok" | "action" | "unknown";
export type PersonaConnectionState = { label: string; tone: ConnectionTone; nextAction: string | null };

export const PLATFORM_LABELS: Record<string, string> = { x: "X", youtube: "YouTube", pixiv: "pixiv", instagram: "Instagram", tiktok: "TikTok" };

export function isConnectablePlatform(platform: string): platform is ConnectablePlatform {
  return (CONNECTABLE_PLATFORMS as readonly string[]).includes(platform);
}

export function youtubeChannelRegistered(account: PlatformAccount): boolean {
  return /^UC[A-Za-z0-9_-]{22}$/u.test(account.account_ref);
}

export function oauthIdentityVerified(credential: PlatformCredentialDetail | null | undefined): boolean {
  return Boolean(credential && credential.connection_type === "oauth" && credential.status === "verified"
    && credential.verification_code === "identity_match" && credential.capabilities.identity === "available"
    && credential.last_verified_at);
}

/**
 * One label and one next step per slot. `credential` is `undefined` while it
 * is loading or unreadable and `null` when the slot has no credential.
 */
export function personaConnectionState(account: PlatformAccount, credential: PlatformCredentialDetail | null | undefined): PersonaConnectionState {
  const name = PLATFORM_LABELS[account.platform] ?? account.platform;
  if (account.platform === "youtube" && !youtubeChannelRegistered(account)) {
    return { label: "channel ID 未登録", tone: "action", nextAction: "YouTube の channel ID を登録する" };
  }
  if (account.status !== "active") {
    return account.platform === "youtube"
      ? { label: "停止中", tone: "action", nextAction: "YouTube 枠を有効化する" }
      : { label: "停止中", tone: "unknown", nextAction: null };
  }
  if (credential === undefined) return { label: "確認中", tone: "unknown", nextAction: null };
  if (oauthIdentityVerified(credential)) return { label: "本人接続済み", tone: "ok", nextAction: null };
  if (credential?.verification_code === "credential_expired") {
    return { label: "認証期限切れ", tone: "action", nextAction: `${name} で本人接続し直す` };
  }
  if (credential && credential.connection_type !== "oauth") {
    return { label: "OAuth 未接続（旧 Cookie のみ）", tone: "action", nextAction: `${name} で本人接続する` };
  }
  if (credential?.status === "verification_pending") return { label: "本人確認待ち", tone: "action", nextAction: "接続状態を再取得する" };
  return { label: "未接続", tone: "action", nextAction: `${name} で本人接続する` };
}
