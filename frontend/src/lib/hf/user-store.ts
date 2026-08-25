/**
 * User-scoped integration storage for Hugging Face and Hydrus.
 *
 * The old implementation kept credentials/references in process.env/.env.  That
 * made an account registered by one AoiTalk user visible to every other user.
 * This adapter intentionally talks to the dedicated user_*_credentials tables
 * using SQL rather than importing a generated schema symbol.  It therefore stays
 * compatible while the Drizzle/SQLAlchemy schema is migrated in a separate lane.
 * Secret fields are encrypted with the existing field-crypto service and are
 * never returned from this module's public DTOs.
 */

import crypto from "node:crypto";
import dns from "node:dns/promises";
import net from "node:net";
import { db } from "@/db";
import { sql } from "drizzle-orm";
import {
  decryptTextIfNeeded,
  encryptText,
} from "@/lib/server/field-crypto";
import type { RepoType } from "./client";

export interface UserHfAccount {
  /** Public opaque id.  It embeds no token and is only meaningful for owner. */
  id: string;
  username: string;
  label: string;
  source: "db";
}

export interface UserHfReferenceRepo {
  repoId: string;
  repoType: RepoType;
  accountId?: string;
}

export interface UserHydrusSettings {
  apiUrl: string;
  /** Secret is used only server-side and never sent to the browser. */
  accessKey: string;
  displayName?: string;
}

type StoredHfAccount = UserHfAccount & { token: string; accountKey: string };
const HF_AAD = "user_hf_credentials.encrypted_payload";
const HYDRUS_AAD = "user_hydrus_credentials.encrypted_payload";
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function validUserId(userId: string): boolean {
  return typeof userId === "string" && UUID_RE.test(userId);
}

function toObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function parseJsonValue(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

async function queryOne(
  table: "user_hf_credentials" | "user_hydrus_credentials",
  userId: string,
  executor: typeof db = db,
  forUpdate = false,
  includeDisabled = false,
): Promise<Record<string, unknown> | null> {
  if (!validUserId(userId)) return null;
  try {
    const lockClause = forUpdate ? sql` FOR UPDATE` : sql``;
    const enabledClause = includeDisabled ? sql`` : sql` AND enabled = true`;
    const rows = await executor.execute(
      sql`SELECT * FROM ${sql.identifier(table)} WHERE user_id = ${userId}${enabledClause} ORDER BY updated_at DESC LIMIT 1${lockClause}`,
    );
    const result = rows as unknown as
      | Array<Record<string, unknown>>
      | { rows?: Array<Record<string, unknown>> };
    const row = Array.isArray(result) ? result[0] : result.rows?.[0];
    return row && typeof row === "object" ? row : null;
  } catch {
    // A deployment may be running before the integration migration.  Never
    // fall back to global env values; an empty user scope is the safe result.
    return null;
  }
}

/** Serialize read/modify/write updates for one user's integration row. */
async function lockUser(executor: typeof db, userId: string): Promise<void> {
  await executor.execute(
    sql`SELECT pg_advisory_xact_lock(hashtextextended(${userId}, 0))`,
  );
}

function rowSecret(row: Record<string, unknown>, table: string): string | null {
  const raw = row.encrypted_payload ?? row._encrypted_payload;
  if (typeof raw !== "string" || !raw) return null;
  try {
    return decryptTextIfNeeded(
      raw,
      table === "user_hf_credentials" ? HF_AAD : HYDRUS_AAD,
    );
  } catch {
    return null;
  }
}

function rowId(row: Record<string, unknown>): string {
  const id = row.id ?? row.integration_id;
  return typeof id === "string" && id ? id : "unknown";
}

function publicAccountId(integrationId: string, accountKey: string): string {
  return `db:${integrationId}:${accountKey}`;
}

function parseHfPayload(
  row: Record<string, unknown>,
): { accounts: StoredHfAccount[]; references: UserHfReferenceRepo[] } {
  const integrationId = rowId(row);
  const decrypted = rowSecret(row, "user_hf_credentials");
  const payload = toObject(parseJsonValue(decrypted));
  const rawAccounts = Array.isArray(payload.accounts) ? payload.accounts : [];
  const accounts: StoredHfAccount[] = [];
  for (const item of rawAccounts) {
    const account = toObject(item);
    const username = typeof account.username === "string" ? account.username.trim() : "";
    const token = typeof account.token === "string" ? account.token : "";
    if (!username || !token) continue;
    const accountKey =
      typeof account.id === "string" && account.id.trim()
        ? account.id.trim()
        : crypto.createHash("sha256").update(username.toLowerCase()).digest("hex").slice(0, 32);
    const label =
      typeof account.label === "string" && account.label.trim()
        ? account.label.trim()
        : username;
    accounts.push({
      id: publicAccountId(integrationId, accountKey),
      accountKey,
      username,
      label,
      token,
      source: "db",
    });
  }

  const settings = toObject(parseJsonValue(row.settings_json));
  const rawReferences =
    Array.isArray(payload.references) ? payload.references : settings.references;
  const references: UserHfReferenceRepo[] = [];
  if (Array.isArray(rawReferences)) {
    const seen = new Set<string>();
    const ownedAccountIds = new Set(accounts.map((account) => account.id));
    for (const item of rawReferences) {
      const value = toObject(item);
      const repoId = typeof value.repoId === "string" ? value.repoId.trim() : "";
      const repoType = value.repoType === "dataset" || value.repoType === "model"
        ? value.repoType
        : null;
      const requestedAccountId =
        typeof value.accountId === "string" && value.accountId
          ? value.accountId
          : undefined;
      const accountId =
        requestedAccountId && ownedAccountIds.has(requestedAccountId)
          ? requestedAccountId
          : undefined;
      if (!repoId || !repoType || !repoId.includes("/")) continue;
      const key = `${repoType}:${repoId.toLowerCase()}`;
      if (seen.has(key)) continue;
      seen.add(key);
      references.push({ repoId, repoType, accountId });
    }
  }
  return { accounts, references };
}

async function writeRow(
  table: "user_hf_credentials" | "user_hydrus_credentials",
  userId: string,
  encryptedPayload: string,
  settings: Record<string, unknown>,
  executor: typeof db = db,
  rowIdOverride?: string,
): Promise<void> {
  if (!validUserId(userId)) throw new Error("ユーザーIDが不正です");
  // Keep the first integration id stable: public HF account ids include this
  // opaque row id and must continue to resolve after a subsequent read.
  const id = rowIdOverride || crypto.randomUUID();
  // Table names are closed over from a literal union above; all values are
  // parameterized by Drizzle's SQL template.
  await executor.execute(
    sql`INSERT INTO ${sql.identifier(table)} (id, user_id, encrypted_payload, settings_json, enabled, created_at, updated_at) VALUES (${id}, ${userId}, ${encryptedPayload}, ${JSON.stringify(settings)}::json, true, NOW(), NOW()) ON CONFLICT (user_id) DO UPDATE SET encrypted_payload = EXCLUDED.encrypted_payload, settings_json = EXCLUDED.settings_json, enabled = true, updated_at = NOW()`,
  );
}

export async function listUserHfAccounts(userId: string): Promise<UserHfAccount[]> {
  const row = await queryOne("user_hf_credentials", userId);
  if (!row) return [];
  return parseHfPayload(row).accounts.map((account) => ({
    id: account.id,
    username: account.username,
    label: account.label,
    source: account.source,
  }));
}

export async function resolveUserHfToken(
  userId: string,
  accountId?: string | null,
): Promise<{ accountId: string; username: string; token: string } | null> {
  const row = await queryOne("user_hf_credentials", userId);
  if (!row) return null;
  const accounts = parseHfPayload(row).accounts;
  const picked = accountId
    ? accounts.find((account) => account.id === accountId)
    : accounts[0];
  if (!picked) return null;
  return {
    accountId: picked.id,
    username: picked.username,
    token: picked.token,
  };
}

export async function listUserHfTokens(
  userId: string,
): Promise<Array<{ accountId: string; username: string; token: string }>> {
  const row = await queryOne("user_hf_credentials", userId);
  if (!row) return [];
  return parseHfPayload(row).accounts.map((account) => ({
    accountId: account.id,
    username: account.username,
    token: account.token,
  }));
}

export async function saveUserHfToken(
  userId: string,
  username: string,
  token: string,
  label?: string,
): Promise<UserHfAccount> {
  const normalizedUsername = username.trim();
  if (!validUserId(userId) || !normalizedUsername || !token) {
    throw new Error("HF資格情報が不正です");
  }
  return db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    await lockUser(executor, userId);
    const row = await queryOne("user_hf_credentials", userId, executor, true, true);
    const existing = row ? parseHfPayload(row) : { accounts: [], references: [] };
    const previous = existing.accounts.find(
      (account) => account.username.toLowerCase() === normalizedUsername.toLowerCase(),
    );
    const accountKey = previous?.accountKey ?? crypto.randomUUID();
    const integrationId = row ? rowId(row) : crypto.randomUUID();
    const account: StoredHfAccount = {
      id: publicAccountId(integrationId, accountKey),
      accountKey,
      username: normalizedUsername,
      label: label?.trim() || normalizedUsername,
      token,
      source: "db",
    };
    const accounts = [
      ...existing.accounts.filter((item) => item.accountKey !== accountKey),
      account,
    ];
    const payload = JSON.stringify({
      accounts: accounts.map((item) => ({
        id: item.accountKey,
        username: item.username,
        label: item.label,
        token: item.token,
      })),
    });
    const settings = { references: existing.references };
    const encrypted = encryptText(payload, HF_AAD);
    await writeRow(
      "user_hf_credentials",
      userId,
      encrypted,
      settings,
      executor,
      integrationId,
    );
    return {
      id: account.id,
      username: account.username,
      label: account.label,
      source: "db" as const,
    };
  });
}

/** Remove one HF integration account owned by the principal. */
export async function deleteUserHfAccount(
  userId: string,
  accountId: string,
): Promise<boolean> {
  if (!accountId) return false;
  return db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    await lockUser(executor, userId);
    const row = await queryOne("user_hf_credentials", userId, executor, true, true);
    if (!row) return false;
    const existing = parseHfPayload(row);
    const account = existing.accounts.find((item) => item.id === accountId);
    if (!account) return false;
    const accounts = existing.accounts.filter((item) => item.id !== accountId);
    const references = existing.references.map((entry) =>
      entry.accountId === accountId ? { ...entry, accountId: undefined } : entry,
    );
    await writeRow(
      "user_hf_credentials",
      userId,
      encryptText(
        JSON.stringify({
          accounts: accounts.map((item) => ({
            id: item.accountKey,
            username: item.username,
            label: item.label,
            token: item.token,
          })),
          references,
        }),
        HF_AAD,
      ),
      { references },
      executor,
    );
    return true;
  });
}

export async function listUserHfReferences(userId: string): Promise<UserHfReferenceRepo[]> {
  const row = await queryOne("user_hf_credentials", userId);
  return row ? parseHfPayload(row).references : [];
}

export async function addUserHfReferences(
  userId: string,
  entries: UserHfReferenceRepo[],
): Promise<void> {
  await db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    await lockUser(executor, userId);
    const row = await queryOne("user_hf_credentials", userId, executor, true, true);
    const existing = row ? parseHfPayload(row) : { accounts: [], references: [] };
    const byKey = new Map(
      existing.references.map((entry) => [`${entry.repoType}:${entry.repoId.toLowerCase()}`, entry]),
    );
    for (const entry of entries) {
      if (!entry.repoId || !entry.repoId.includes("/")) continue;
      if (entry.accountId) {
        const owns = existing.accounts.some((account) => account.id === entry.accountId);
        if (!owns) throw new Error("HFアカウントの所有権がありません");
      }
      byKey.set(`${entry.repoType}:${entry.repoId.toLowerCase()}`, entry);
    }
    const payload = JSON.stringify({
      accounts: existing.accounts.map((item) => ({
        id: item.accountKey,
        username: item.username,
        label: item.label,
        token: item.token,
      })),
      references: [...byKey.values()],
    });
    await writeRow(
      "user_hf_credentials",
      userId,
      encryptText(payload, HF_AAD),
      { references: [...byKey.values()] },
      executor,
    );
  });
}

export async function getUserHydrusSettings(userId: string): Promise<UserHydrusSettings | null> {
  const row = await queryOne("user_hydrus_credentials", userId);
  if (!row) return null;
  const secret = rowSecret(row, "user_hydrus_credentials");
  const payload = toObject(parseJsonValue(secret));
  const apiUrl = typeof payload.apiUrl === "string" ? payload.apiUrl.trim() : "";
  const accessKey = typeof payload.accessKey === "string" ? payload.accessKey : "";
  if (!apiUrl || !accessKey) return null;
  // Never echo a legacy URL containing credentials (or a disallowed private
  // endpoint) back to the settings UI.  The user must explicitly reconfigure
  // it through the validated save path.
  try {
    const parsed = new URL(apiUrl);
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    const allowPrivate = /^(1|true|yes)$/i.test(
      process.env.HYDRUS_ALLOW_PRIVATE_HOSTS ||
        process.env.AOITALK_HYDRUS_ALLOW_PRIVATE_URLS ||
        "",
    );
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      parsed.username ||
      parsed.password ||
      (isPrivateHost(host) && !allowPrivate)
    ) {
      return null;
    }
    if (!allowPrivate && !isPrivateHost(host)) {
      try {
        const addresses = await dns.lookup(host, { all: true, verbatim: true });
        // An empty answer is not proof of a public destination.  Fail closed
        // rather than echoing a legacy endpoint that cannot be validated.
        if (
          addresses.length === 0 ||
          addresses.some((entry) => isPrivateHost(entry.address))
        ) {
          return null;
        }
      } catch {
        // DNS errors are treated as invalid configuration.  The caller can
        // explicitly re-save once resolution works; no potentially internal
        // endpoint is echoed while the destination is unknown.
        return null;
      }
    }
  } catch {
    return null;
  }
  const settings = toObject(parseJsonValue(row.settings_json));
  const displayName = typeof settings.displayName === "string" ? settings.displayName : undefined;
  return { apiUrl, accessKey, displayName };
}

export async function saveUserHydrusSettings(
  userId: string,
  settings: UserHydrusSettings,
): Promise<void> {
  let apiUrl: URL;
  try {
    apiUrl = new URL(settings.apiUrl);
  } catch {
    throw new Error("Hydrus API URLが不正です");
  }
  if (apiUrl.protocol !== "http:" && apiUrl.protocol !== "https:") {
    throw new Error("Hydrus API URLが不正です");
  }
  if (apiUrl.username || apiUrl.password) {
    throw new Error("Hydrus API URLに埋め込み認証情報は指定できません");
  }
  const allowPrivate = /^(1|true|yes)$/i.test(
    process.env.HYDRUS_ALLOW_PRIVATE_HOSTS ||
      process.env.AOITALK_HYDRUS_ALLOW_PRIVATE_URLS ||
      "",
  );
  const host = apiUrl.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const privateHost = isPrivateHost(host);
  if (privateHost && !allowPrivate) {
    throw new Error("Hydrus API URLのprivate/localhost接続は管理ポリシーで許可されていません");
  }
  if (!privateHost && !allowPrivate) {
    try {
      const addresses = await dns.lookup(host, { all: true, verbatim: true });
      if (
        addresses.length === 0 ||
        addresses.some((entry) => isPrivateHost(entry.address))
      ) {
        throw new Error("Hydrus API URLの解決先がprivateネットワークです");
      }
    } catch (error) {
      if (error instanceof Error && error.message.includes("privateネットワーク")) {
        throw error;
      }
      throw new Error("Hydrus API URLのDNS解決に失敗しました");
    }
  }
  if (!settings.accessKey.trim()) throw new Error("Hydrus access keyが必要です");
  await db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    await lockUser(executor, userId);
    await writeRow(
      "user_hydrus_credentials",
      userId,
      encryptText(
        JSON.stringify({
          apiUrl: apiUrl.toString().replace(/\/$/, ""),
          accessKey: settings.accessKey,
        }),
        HYDRUS_AAD,
      ),
      settings.displayName ? { displayName: settings.displayName } : {},
      executor,
    );
  });
}

function isPrivateHost(host: string): boolean {
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) {
    return true;
  }
  const family = net.isIP(host);
  if (family === 4) {
    const octets = host.split(".").map(Number);
    return (
      octets[0] === 10 ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168) ||
      octets[0] === 127 ||
      (octets[0] === 169 && octets[1] === 254) ||
      octets[0] === 0
    );
  }
  if (family === 6) {
    const normalized = host.toLowerCase();
    if (normalized.startsWith("::ffff:")) {
      return isPrivateHost(normalized.slice("::ffff:".length));
    }
    return (
      normalized === "::1" ||
      normalized === "::" ||
      normalized.startsWith("fe80:") ||
      normalized.startsWith("fc") ||
      normalized.startsWith("fd")
    );
  }
  return false;
}

export async function deleteUserHydrusSettings(userId: string): Promise<void> {
  if (!validUserId(userId)) return;
  await db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    await lockUser(executor, userId);
    await executor.execute(
      sql`UPDATE ${sql.identifier("user_hydrus_credentials")} SET enabled = false, encrypted_payload = '', updated_at = NOW() WHERE user_id = ${userId}`,
    );
  });
}
