import type { Page } from "@playwright/test";
import { SignJWT } from "jose";
import { createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import postgres from "postgres";

const DEFAULT_E2E_USER_ID = "00000000-0000-4000-8000-000000000001";
const E2E_USERNAME = "__playwright_e2e__";

function readEnvValue(key: string) {
  const envPath = resolve(process.cwd(), ".env");
  try {
    const line = readFileSync(envPath, "utf8")
      .split(/\r?\n/)
      .find((item) => item.startsWith(`${key}=`));
    return line?.slice(key.length + 1).trim();
  } catch {
    return undefined;
  }
}

const SECRET = new TextEncoder().encode(
  process.env.NEXTAUTH_SECRET ||
    readEnvValue("NEXTAUTH_SECRET") ||
    "fallback-secret",
);

export const E2E_USER_ID = process.env.E2E_USER_ID || DEFAULT_E2E_USER_ID;

const E2E_VERIFICATION_SOURCE = "frontend/e2e/support/auth.ts::ensureE2EUser";
const EPHEMERAL_DATABASE_PREFIXES = [
  "aoitalk_canonical_",
  "aoitalk_e2e_",
  "aoitalk_test_",
  "aoitalk_gc_",
];
const POSTGRES_SCHEMA_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export type VerificationRun = {
  runId: string;
  source: string;
};

/**
 * Return a machine-readable run context for live verification.
 *
 * Live E2E previously relied on title/username prefixes and left rows in the
 * normal database whenever a test aborted.  The context is sent as headers
 * to verification-aware API routes; server-side code remains the authority
 * for signing/recording the run and for destructive cleanup.
 */
export function createVerificationRun(source: string): VerificationRun {
  const configured = process.env.AOITALK_VERIFICATION_RUN_ID?.trim();
  const runId = configured || randomUUID();
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      runId,
    )
  ) {
    throw new Error(`invalid AOITALK_VERIFICATION_RUN_ID: ${runId}`);
  }
  const configuredSource = process.env.AOITALK_VERIFICATION_SOURCE?.trim();
  const effectiveSource = configuredSource || source.trim();
  if (
    !effectiveSource ||
    effectiveSource.length > 255 ||
    /[\u0000-\u001f]/.test(effectiveSource)
  ) {
    throw new Error(
      "AOITALK_VERIFICATION_SOURCE must be a non-empty value of at most 255 characters without control characters",
    );
  }
  return { runId: runId.toLowerCase(), source: effectiveSource };
}

export function verificationRunHeaders(
  run: VerificationRun,
): Record<string, string> {
  const harness =
    process.env.AOITALK_VERIFICATION_HARNESS?.trim() || run.source;
  if (!harness || harness.length > 255 || /[\u0000-\u001f]/.test(harness)) {
    throw new Error(
      "AOITALK_VERIFICATION_HARNESS must be a non-empty value of at most 255 characters without control characters",
    );
  }
  const secret = process.env.AOITALK_VERIFICATION_HARNESS_KEY?.trim();
  if (!secret || secret.length < 16) {
    throw new Error(
      "AOITALK_VERIFICATION_HARNESS_KEY (at least 16 characters) is required for live verification",
    );
  }
  const signedPayload = `${run.runId}\n${harness}\n${run.source}\ntrue`;
  const signature = createHmac("sha256", secret)
    .update(signedPayload)
    .digest("hex");
  return {
    "X-AoiTalk-Verification-Run-Id": run.runId,
    "X-AoiTalk-Verification-Harness": harness,
    "X-AoiTalk-Verification-Source": run.source,
    "X-AoiTalk-Verification-Disposable": "true",
    "X-AoiTalk-Verification-Signature": signature,
  };
}

/** Attach the run marker to all requests issued by this Playwright page. */
export async function installVerificationRun(
  page: Page,
  run: VerificationRun,
): Promise<void> {
  await page.setExtraHTTPHeaders(verificationRunHeaders(run));
}

/**
 * Refuse opt-in specs that write entities without a disposable storage
 * boundary.  A signed run protects supported server-owned artifacts, but
 * Story/TRPG rows currently have no generic cleanup adapter; those specs must
 * therefore run only against the canonical runner's fresh database (or an
 * explicitly non-public schema).
 */
export function assertVerificationStorageIsolated(
  options: {
    requireEphemeralDatabase?: boolean;
  } = {},
): void {
  const schema = process.env.POSTGRES_SCHEMA?.trim();
  const schemaIsolated = Boolean(schema && schema.toLowerCase() !== "public");
  if (schemaIsolated && !options.requireEphemeralDatabase) return;
  const configured = process.env.DATABASE_URL || readEnvValue("DATABASE_URL");
  const database = configured
    ? (() => {
        try {
          const parsed = new URL(configured);
          return decodeURIComponent(parsed.pathname.replace(/^\//, ""));
        } catch {
          return "";
        }
      })()
    : process.env.POSTGRES_DB || readEnvValue("POSTGRES_DB") || "";
  const normalized = String(database).trim().toLowerCase();
  if (
    (options.requireEphemeralDatabase && schemaIsolated) ||
    !EPHEMERAL_DATABASE_PREFIXES.some((prefix) => normalized.startsWith(prefix))
  ) {
    throw new Error(
      options.requireEphemeralDatabase
        ? "verification spec requires an ephemeral PostgreSQL database (Story/TRPG rows have no generic cleanup); refusing normal or persistent-schema writes"
        : "verification spec requires an ephemeral PostgreSQL database or non-public POSTGRES_SCHEMA; refusing normal runtime writes",
    );
  }
}

export type VerificationCleanupOptions = {
  /**
   * Permit a run with no ledger selector only when an outer harness has
   * guaranteed ephemeral storage (for example Story/TRPG rows that predate
   * generic verification cleanup). Endpoint/HTTP failures remain fatal.
   */
  allowMissingSelector?: boolean;
};

/**
 * Delete one explicitly attributable verification run through the admin
 * maintenance API.  No title/status/age fallback is permitted.  The
 * endpoint/method can be overridden for a staged deployment, but a missing
 * endpoint is an error by default so live verification cannot silently leak
 * rows.  Set ``AOITALK_VERIFICATION_CLEANUP_OPTIONAL=1`` only for a server
 * that is intentionally running a read-only/non-persistent workflow.
 */
export async function cleanupVerificationRun(
  page: Page,
  run: VerificationRun,
  options: VerificationCleanupOptions = {},
): Promise<{ status: number; body: unknown }> {
  const basePath =
    process.env.AOITALK_VERIFICATION_CLEANUP_BASE_PATH?.trim() ||
    "/api/admin/verification-data";
  // The Next BFF exposes GET and POST on the base path.  A direct FastAPI
  // deployment may still expose the historical ``/preview`` and ``/cleanup``
  // suffixes; callers can opt into those explicitly via the env overrides.
  const previewPath =
    process.env.AOITALK_VERIFICATION_PREVIEW_PATH?.trim() || basePath;
  const path =
    process.env.AOITALK_VERIFICATION_CLEANUP_PATH?.trim() || basePath;
  const requestHeaders = {
    ...verificationRunHeaders(run),
    ...(process.env.AOITALK_VERIFICATION_CLEANUP_TOKEN?.trim()
      ? {
          Authorization: `Bearer ${process.env.AOITALK_VERIFICATION_CLEANUP_TOKEN.trim()}`,
        }
      : {}),
  };
  const listingResponse = await page.request.get(previewPath, {
    headers: requestHeaders,
    failOnStatusCode: false,
  });
  const listingBody = await listingResponse
    .json()
    .catch(() => listingResponse.text().catch(() => ""));
  if (
    listingResponse.status() === 404 &&
    process.env.AOITALK_VERIFICATION_CLEANUP_OPTIONAL?.trim() === "1"
  ) {
    return { status: listingResponse.status(), body: listingBody };
  }
  if (!listingResponse.ok()) {
    throw new Error(
      `verification preview failed (${listingResponse.status()}): ${JSON.stringify(listingBody).slice(0, 500)}`,
    );
  }
  if (
    listingBody === null ||
    typeof listingBody !== "object" ||
    Array.isArray(listingBody)
  ) {
    throw new Error("verification preview returned a non-object payload");
  }
  const listing = listingBody as {
    selectors?: Array<{ type?: string; id?: string }>;
    preview_digest?: string;
    digest?: string;
  };
  const selector = (listing.selectors || []).find(
    (item) => item.type === "verification_run" && item.id === run.runId,
  );
  // A missing selector is only considered clean in an explicitly
  // non-persistent/read-only workflow.  Live tests that created entities must
  // fail rather than silently treating an unregistered run as teardown.
  if (!selector) {
    if (
      options.allowMissingSelector ||
      process.env.AOITALK_VERIFICATION_CLEANUP_OPTIONAL?.trim() === "1"
    ) {
      return { status: 200, body: { status: "already_clean" } };
    }
    throw new Error(
      `verification run ${run.runId} is not registered in the server provenance ledger`,
    );
  }
  const previewDigest = listing.preview_digest || listing.digest;
  if (!previewDigest) {
    throw new Error("verification preview did not return preview_digest");
  }
  const response = await page.request.post(path, {
    headers: requestHeaders,
    data: {
      selectors: [{ type: "verification_run", id: run.runId }],
      preview_digest: previewDigest,
      confirmation: "DELETE VERIFIED TEST DATA",
    },
    failOnStatusCode: false,
  });
  const body = await response
    .json()
    .catch(() => response.text().catch(() => ""));
  if (!response.ok() && response.status() !== 404) {
    throw new Error(
      `verification cleanup failed (${response.status()}): ${JSON.stringify(body).slice(0, 500)}`,
    );
  }
  if (
    response.status() === 404 &&
    process.env.AOITALK_VERIFICATION_CLEANUP_OPTIONAL?.trim() !== "1"
  ) {
    throw new Error(
      `verification cleanup endpoint is unavailable (POST ${path}); refusing to leave a live run uncleaned`,
    );
  }
  return { status: response.status(), body };
}

function resolveTestUserId(userId?: string) {
  // Older E2E specs passed the non-UUID placeholder `user-1`. Keep those
  // callers compatible while ensuring proxy's UUID-backed DB lookup is valid.
  return !userId || userId === "user-1" ? E2E_USER_ID : userId;
}

function getDatabaseUrl() {
  const configured = process.env.DATABASE_URL || readEnvValue("DATABASE_URL");
  if (configured) return configured;
  const user =
    process.env.POSTGRES_USER || readEnvValue("POSTGRES_USER") || "aoitalk";
  const password =
    process.env.POSTGRES_PASSWORD || readEnvValue("POSTGRES_PASSWORD") || "";
  const host =
    process.env.POSTGRES_HOST || readEnvValue("POSTGRES_HOST") || "127.0.0.1";
  const port =
    process.env.POSTGRES_PORT || readEnvValue("POSTGRES_PORT") || "5432";
  const database =
    process.env.POSTGRES_DB || readEnvValue("POSTGRES_DB") || "aoitalk_memory";
  return `postgresql://${encodeURIComponent(user)}:${encodeURIComponent(password)}@${host}:${port}/${encodeURIComponent(database)}`;
}

function getDatabaseOptions() {
  const schema = (
    process.env.POSTGRES_SCHEMA || readEnvValue("POSTGRES_SCHEMA") || ""
  ).trim();
  if (!schema || schema.toLowerCase() === "public") {
    return { max: 1 };
  }
  // The schema is sent as a libpq startup option, not interpolated into SQL.
  // Validate it as one simple identifier before constructing the option so a
  // schema-only verification run cannot escape into another namespace.
  if (!POSTGRES_SCHEMA_RE.test(schema)) {
    throw new Error(
      "POSTGRES_SCHEMA must be a simple PostgreSQL identifier for verification",
    );
  }
  return {
    max: 1,
    connection: { options: `-c search_path=${schema}` },
  };
}

export async function ensureE2EUser() {
  // Global setup writes the reserved fixture user before any spec runs.  Do
  // not authorize that write against a developer's ordinary runtime DB; the
  // canonical runner supplies a fresh prefixed database (or an explicit
  // non-public schema) through the environment.
  assertVerificationStorageIsolated();
  const verificationRun = createVerificationRun(E2E_VERIFICATION_SOURCE);
  const verificationRunId = verificationRun.runId;
  const verificationSource = verificationRun.source;
  const verificationHarness =
    process.env.AOITALK_VERIFICATION_HARNESS?.trim() || verificationSource;
  if (
    !verificationHarness ||
    verificationHarness.length > 255 ||
    /[\u0000-\u001f]/.test(verificationHarness)
  ) {
    throw new Error(
      "AOITALK_VERIFICATION_HARNESS must be a non-empty value of at most 255 characters without control characters",
    );
  }
  const verificationMetadata = JSON.stringify({
    verification_provenance: {
      schema_version: 1,
      disposable: true,
      run_id: verificationRunId,
      source: verificationSource,
      harness: verificationHarness,
    },
  });
  const sql = postgres(getDatabaseUrl(), getDatabaseOptions());
  try {
    const existing = await sql<{ username: string }[]>`
      select username from users where id = ${E2E_USER_ID}::uuid
    `;
    if (existing[0] && existing[0].username !== E2E_USERNAME) {
      throw new Error(
        `E2E user id ${E2E_USER_ID} is already owned by ${existing[0].username}`,
      );
    }
    await sql`
      insert into users (
        id, username, password_hash, display_name, role, is_active,
        is_password_reset_required, session_version, user_settings,
        created_at, updated_at
      ) values (
        ${E2E_USER_ID}::uuid, ${E2E_USERNAME}, ${"e2e-not-a-login-password"},
        ${"Playwright E2E"}, ${"admin"}, true, false, 1,
        ${verificationMetadata}::json, now(), now()
      )
      on conflict (id) do update set
        role = excluded.role,
        is_active = true,
        is_password_reset_required = false,
        session_version = 1,
        user_settings = excluded.user_settings,
        updated_at = now()
    `;
  } finally {
    await sql.end();
  }
}

export async function deactivateE2EUser() {
  // Teardown is also a DB mutation.  Refuse to touch normal runtime storage
  // even when setup/test execution aborted before a page-level cleanup path.
  assertVerificationStorageIsolated();
  const sql = postgres(getDatabaseUrl(), getDatabaseOptions());
  try {
    // E2E-created rows can legitimately retain created_by/updated_by foreign
    // keys to this reserved fixture user. Keep the identity reusable instead
    // of deleting referenced data, but make its session unusable between runs.
    await sql`
      update users
      set is_active = false, updated_at = now()
      where id = ${E2E_USER_ID}::uuid and username = ${E2E_USERNAME}
    `;
  } finally {
    await sql.end();
  }
}

export async function createSessionToken(userId = E2E_USER_ID) {
  const resolvedUserId = resolveTestUserId(userId);
  return await new SignJWT({
    sub: resolvedUserId,
    username: E2E_USERNAME,
    role: "admin",
    session_version: 1,
    password_reset_required: false,
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

export async function addAuthCookie(page: Page, userId = E2E_USER_ID) {
  const host = process.env.PLAYWRIGHT_HOST ?? "127.0.0.1";
  const port = process.env.PLAYWRIGHT_PORT ?? "3002";
  await page.context().addCookies([
    {
      name: "aoitalk_session",
      value: await createSessionToken(resolveTestUserId(userId)),
      url: `http://${host}:${port}`,
    },
  ]);
}

export async function mockAuthenticatedApis(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: E2E_USER_ID, username: E2E_USERNAME, role: "admin" },
        },
      });
      return;
    }

    if (url.pathname === "/api/projects") {
      await route.fulfill({ json: { projects: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/users/list") {
      await route.fulfill({ json: [] });
      return;
    }

    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/notifications") {
      await route.fulfill({ json: [] });
      return;
    }

    if (url.pathname === "/api/tasks") {
      await route.fulfill({ json: [] });
      return;
    }

    if (url.pathname === "/api/time-entries/active") {
      await route.fulfill({ json: null });
      return;
    }

    if (url.pathname === "/api/docs") {
      await route.fulfill({
        json: {
          workspace: {
            id: "workspace-1",
            name: "Personal Docs",
            description: "",
            owner_user_id: "user-1",
            settings: {},
            created_at: "2026-06-30T00:00:00",
            updated_at: "2026-06-30T00:00:00",
          },
          nodes: [],
          supertags: [],
          node_supertags: [],
          fields: [],
          field_values: [],
          views: [],
          ai_suggestions: [],
          import_jobs: [],
          import_items: [],
          attachments: [],
          edges: [],
          projects: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/docs/bootstrap") {
      await route.fulfill({
        json: {
          nodes: [],
          supertags: [],
          node_supertags: [],
          supertag_fields: [],
          placements: [],
          fields: [],
          field_values: [],
          attachments: [],
          views: [],
          ai_suggestions: [],
          projects: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/conversations" && method === "POST") {
      await route.fulfill({
        json: {
          session: {
            id: "session-e2e",
            user_id: "user-1",
            title: "E2E conversation",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
        },
      });
      return;
    }

    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/conversations/session-e2e/messages") {
      if (method === "POST") {
        const body = route.request().postDataJSON() as {
          role?: string;
          content?: string;
        };
        await route.fulfill({
          json: {
            success: true,
            message: {
              id: "message-e2e",
              session_id: "session-e2e",
              role: body.role ?? "user",
              content: body.content ?? "",
              metadata: {},
              branch_index: 0,
              is_active_branch: true,
            },
          },
        });
        return;
      }
      await route.fulfill({ json: { messages: [] } });
      return;
    }

    if (url.pathname === "/api/conversations/session-e2e/resume") {
      await route.fulfill({
        json: {
          session: {
            id: "session-e2e",
            user_id: "user-1",
            title: "E2E conversation",
            character_name: "aoi",
            message_count: 0,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: [],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ status: 503, json: { ok: false } });
      return;
    }

    if (url.pathname === "/api/python-proxy/characters") {
      await route.fulfill({ json: { characters: [], current: "" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/engine") {
      await route.fulfill({ json: { available: [], provider: "", model: "" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/models") {
      await route.fulfill({
        json: {
          current: { provider: "mock", model: "mock-model" },
          providers: [{ id: "mock", label: "Mock", models: [] }],
        },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/mode") {
      await route.fulfill({ json: { mode: "", available_modes: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/runtime/features") {
      await route.fulfill({ json: { features: {} } });
      return;
    }

    if (url.pathname === "/api/python-proxy/mobile/commands") {
      await route.fulfill({ json: { enabled: false, commands: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/crawler/status") {
      await route.fulfill({ json: { crawlers: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/settings") {
      await route.fulfill({ json: {} });
      return;
    }

    if (
      url.pathname === "/api/python-proxy/conversations/session-e2e/dispatch"
    ) {
      await route.fulfill({
        json: { success: true, queued: false, session_id: "session-e2e" },
      });
      return;
    }

    if (url.pathname.includes("/scenarios/logs/by-conversation/")) {
      await route.fulfill({ json: null });
      return;
    }

    if (url.pathname.endsWith("/explorer/list")) {
      await route.fulfill({
        json: {
          success: true,
          current_path: "",
          parent_path: null,
          can_go_up: false,
          directories: [],
          files: [],
          total_items: 0,
        },
      });
      return;
    }

    await route.fulfill({ json: {} });
  });
}
