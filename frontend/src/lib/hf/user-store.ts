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
import { db } from "@/db";
import { sql } from "drizzle-orm";
import {
  decryptTextIfNeeded,
  encryptText,
} from "@/lib/server/field-crypto";
import {
  DEFAULT_HYDRUS_API_URL,
  effectiveHydrusProfile,
  inspectHydrusEndpoint,
  isNativeLocalPersonal,
  type HydrusPolicyOptions,
  type HydrusEndpointRejectReason,
} from "@/lib/server/hydrus-policy";
import type { RepoType } from "./client";

// Re-export the policy entry points from the historical user-store module so
// server callers/tests do not accidentally grow a second Hydrus URL policy.
export {
  DEFAULT_HYDRUS_API_URL,
  effectiveHydrusProfile,
  inspectHydrusEndpoint,
  isLoopbackHost,
  isNativeLocalPersonal,
  isPrivateHost,
  validateHydrusApiUrl,
} from "@/lib/server/hydrus-policy";

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
const LEGACY_MIGRATION_SOURCE = "legacy_env_v1";
const LEGACY_MIGRATION_METHOD = "explicit_current_principal_claim";
const LEGACY_MIGRATION_LOCK_KEY = "aoitalk:hydrus:legacy-env-owner-claim";
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
  // Never return a URL that is outside the canonical endpoint policy.  This
  // protects both the settings API and the FastAPI credential resolver from a
  // stale/legacy row containing an internal destination.
  const endpoint = await inspectHydrusEndpoint(apiUrl);
  if (!endpoint.allowed) return null;
  const settings = toObject(parseJsonValue(row.settings_json));
  const displayName = typeof settings.displayName === "string" ? settings.displayName : undefined;
  return { apiUrl: endpoint.url, accessKey, displayName };
}

function hydrusUrlValidationError(reason: HydrusEndpointRejectReason): Error {
  switch (reason) {
    case "embedded-credentials":
      return new Error("Hydrus API URLに埋め込み認証情報は指定できません");
    case "loopback-requires-native":
      return new Error("Hydrus API URLのloopback接続はWindows Personalのnative local実行でのみ許可されます");
    case "private-host":
      return new Error("Hydrus API URLのprivate/localhost接続は管理ポリシーで許可されていません");
    case "private-resolution":
      return new Error("Hydrus API URLの解決先がprivateネットワークです");
    case "dns-failure":
      return new Error("Hydrus API URLのDNS解決に失敗しました");
    case "unsupported-protocol":
    case "invalid-url":
    default:
      return new Error("Hydrus API URLが不正です");
  }
}

export async function saveUserHydrusSettings(
  userId: string,
  settings: UserHydrusSettings,
): Promise<void> {
  const endpoint = await inspectHydrusEndpoint(settings.apiUrl);
  if (!endpoint.allowed) throw hydrusUrlValidationError(endpoint.reason);
  if (!settings.accessKey.trim()) throw new Error("Hydrus access keyが必要です");
  await db.transaction(async (tx) => {
    const executor = tx as unknown as typeof db;
    // Serialize explicit saves with the one-time process-global legacy claim.
    // Without the shared lock, a concurrent manual save by another principal
    // could commit between the migration's row-count check and its INSERT.
    await executor.execute(
      sql`SELECT pg_advisory_xact_lock(hashtextextended(${LEGACY_MIGRATION_LOCK_KEY}, 0))`,
    );
    await lockUser(executor, userId);
    const existing = await queryOne(
      "user_hydrus_credentials",
      userId,
      executor,
      true,
      true,
    );
    const existingSettings = existing
      ? toObject(parseJsonValue(existing.settings_json))
      : {};
    const migration = toObject(existingSettings.migration);
    const persistedSettings: Record<string, unknown> = {};
    // Keep the secret-free owner-claim marker across later manual saves.  The
    // marker is also intentionally retained by the delete tombstone path so
    // a deleted legacy claim cannot be silently re-imported.
    if (Object.keys(migration).length > 0) {
      persistedSettings.migration = migration;
    }
    if (settings.displayName) persistedSettings.displayName = settings.displayName;
    await writeRow(
      "user_hydrus_credentials",
      userId,
      encryptText(
        JSON.stringify({
          apiUrl: endpoint.url,
          accessKey: settings.accessKey.trim(),
        }),
        HYDRUS_AAD,
      ),
      persistedSettings,
      executor,
    );
  });
}

/** Public, non-secret status values returned by the legacy migration action. */
export type LegacyHydrusMigrationStatus =
  | "migrated"
  | "already_migrated"
  | "legacy_unavailable"
  | "endpoint_rejected"
  | "credential_conflict"
  | "enterprise_disabled"
  | "profile_not_personal"
  | "native_local_required"
  | "invalid_user"
  | "internal_error";

export interface LegacyHydrusMigrationResult {
  status: LegacyHydrusMigrationStatus;
  /** True for both a newly imported row and an idempotent retry. */
  migrated: boolean;
  /** Normalized endpoint only; the encrypted access key is never returned. */
  apiUrl?: string;
}

function legacyEnvironment(env: NodeJS.ProcessEnv = process.env): {
  apiUrl: string;
  accessKey: string;
  endpointDefaulted: boolean;
} | null {
  const accessKey = env.HYDRUS_ACCESS_KEY;
  if (typeof accessKey !== "string" || !accessKey.trim()) return null;
  // Hydrus historically defaulted to the local Client port.  Keep that
  // default for existing installations that only carried HYDRUS_ACCESS_KEY.
  const configuredUrl = env.HYDRUS_API_URL?.trim() || "";
  const apiUrl = configuredUrl || DEFAULT_HYDRUS_API_URL;
  return { apiUrl, accessKey: accessKey.trim(), endpointDefaulted: !configuredUrl };
}

function enabledValue(row: Record<string, unknown>): boolean {
  return row.enabled !== false && row.enabled !== "false";
}

function isLegacyMigrationRow(row: Record<string, unknown>): boolean {
  const settings = toObject(parseJsonValue(row.settings_json));
  const marker = toObject(settings.migration);
  return (
    marker.source === LEGACY_MIGRATION_SOURCE &&
    marker.method === LEGACY_MIGRATION_METHOD &&
    typeof marker.claimedAt === "string" &&
    marker.claimedAt.length > 0 &&
    typeof marker.endpointDefaulted === "boolean"
  );
}

function countRows(value: unknown): number | null {
  const result = value as
    | Array<Record<string, unknown>>
    | { rows?: Array<Record<string, unknown>> };
  const row = Array.isArray(result) ? result[0] : result?.rows?.[0];
  if (!row || typeof row !== "object") return null;
  const raw = row.count ?? row.row_count ?? row.total;
  const count = typeof raw === "number" ? raw : Number(raw);
  return Number.isFinite(count) && count >= 0 ? Math.trunc(count) : null;
}

/** Count all rows, including disabled tombstones, in the Hydrus table. */
async function countHydrusCredentialRows(executor: typeof db): Promise<number> {
  const result = await executor.execute(
    sql`SELECT COUNT(*) AS count FROM ${sql.identifier("user_hydrus_credentials")}`,
  );
  const count = countRows(result);
  if (count === null) throw new Error("Hydrus credential row count unavailable");
  return count;
}

function migrationProfileStatus(
  options: Pick<HydrusPolicyOptions, "env" | "platform"> = {},
): LegacyHydrusMigrationStatus | null {
  const profile = effectiveHydrusProfile(options.env || process.env);
  if (profile === "enterprise") return "enterprise_disabled";
  if (profile !== "personal") return "profile_not_personal";
  if (!isNativeLocalPersonal(options)) return "native_local_required";
  return null;
}

/**
 * Return whether an authenticated principal can see a safe legacy-import
 * affordance.  The result deliberately contains no reason or secret; a
 * caller only learns that an import is available for its own account.
 */
export async function getLegacyHydrusAvailability(
  userId: string,
  options: HydrusPolicyOptions = {},
): Promise<boolean> {
  if (!validUserId(userId) || migrationProfileStatus(options)) return false;
  const legacy = legacyEnvironment(options.env || process.env);
  if (!legacy) return false;
  try {
    // The old environment was process-global.  Any row for any principal,
    // including a disabled tombstone, means ownership has already been
    // established or explicitly declined and must block a new claim.
    if ((await countHydrusCredentialRows(db)) > 0) return false;
  } catch {
    return false;
  }
  const endpoint = await inspectHydrusEndpoint(legacy.apiUrl, options);
  // Legacy HYDRUS_* settings were process-global and historically pointed at
  // the local desktop client.  A one-time ownership claim must never broaden
  // that contract to an arbitrary public/LAN destination, even when the
  // administrator has enabled private-host integrations for normal saves.
  return endpoint.allowed && endpoint.kind === "loopback";
}

/**
 * Atomically claim the process-global legacy Hydrus environment for the
 * authenticated principal.  The caller must have supplied the explicit
 * confirmation phrase; this function only performs the server-side claim.
 */
export async function migrateLegacyHydrusSettings(
  userId: string,
  options: HydrusPolicyOptions = {},
): Promise<LegacyHydrusMigrationResult> {
  if (!validUserId(userId)) {
    return { status: "invalid_user", migrated: false };
  }
  const profileStatus = migrationProfileStatus(options);
  if (profileStatus) return { status: profileStatus, migrated: false };

  const env = options.env || process.env;
  try {
    return await db.transaction(async (tx) => {
      const executor = tx as unknown as typeof db;
      // One global lock is intentional: the legacy key is process-global, so
      // two users must never race to claim it.  The per-user lock protects
      // this row from concurrent manual saves in the same transaction scope.
      await executor.execute(
        sql`SELECT pg_advisory_xact_lock(hashtextextended(${LEGACY_MIGRATION_LOCK_KEY}, 0))`,
      );
      await lockUser(executor, userId);
      const existing = await queryOne(
        "user_hydrus_credentials",
        userId,
        executor,
        true,
        true,
      );
      if (existing) {
        if (enabledValue(existing) && isLegacyMigrationRow(existing)) {
          const secret = rowSecret(existing, "user_hydrus_credentials");
          const payload = toObject(parseJsonValue(secret));
          const apiUrl = typeof payload.apiUrl === "string" ? payload.apiUrl : undefined;
          return { status: "already_migrated", migrated: true, ...(apiUrl ? { apiUrl } : {}) };
        }
        // A disabled row is deliberately a conflict too.  Otherwise deleting
        // a user's manual integration would make a global secret claimable by
        // whoever happens to be logged in next.
        return { status: "credential_conflict", migrated: false };
      }

      // Ownership is global, not per-user: the legacy environment contains a
      // single process-wide key.  A row owned by another principal therefore
      // blocks this claim even though the current principal has no row.
      if ((await countHydrusCredentialRows(executor)) > 0) {
        return { status: "credential_conflict", migrated: false };
      }

      const legacy = legacyEnvironment(env);
      if (!legacy) return { status: "legacy_unavailable", migrated: false };
      const endpoint = await inspectHydrusEndpoint(legacy.apiUrl, options);
      if (!endpoint.allowed || endpoint.kind !== "loopback") {
        // A native gate failure is normally caught before opening the
        // transaction, but retain the typed distinction if policy options are
        // supplied by a test or an embedding server.
        if (!endpoint.allowed && endpoint.reason === "loopback-requires-native") {
          return { status: "native_local_required", migrated: false };
        }
        return { status: "endpoint_rejected", migrated: false };
      }

      const claimedAt = new Date().toISOString();
      const settings = {
        migration: {
          source: LEGACY_MIGRATION_SOURCE,
          method: LEGACY_MIGRATION_METHOD,
          claimedAt,
          endpointDefaulted: legacy.endpointDefaulted,
        },
      };
      await writeRow(
        "user_hydrus_credentials",
        userId,
        encryptText(
          JSON.stringify({
            apiUrl: endpoint.url,
            accessKey: legacy.accessKey,
          }),
          HYDRUS_AAD,
        ),
        settings,
        executor,
      );
      return { status: "migrated", migrated: true, apiUrl: endpoint.url };
    });
  } catch {
    // Do not send SQL/crypto errors (which could contain configuration
    // material) to the browser.  The route exposes only this safe status.
    return { status: "internal_error", migrated: false };
  }
}

// Descriptive alias for callers that want to emphasize the owner-claim
// semantics.  Keep one implementation so the locking and marker contract
// cannot drift between routes.
export const claimLegacyHydrusSettings = migrateLegacyHydrusSettings;

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
