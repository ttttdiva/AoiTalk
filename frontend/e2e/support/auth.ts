import type { Page } from "@playwright/test";
import { SignJWT } from "jose";
import { randomUUID } from "node:crypto";
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

function resolveTestUserId(userId?: string) {
  // Older E2E specs passed the non-UUID placeholder `user-1`. Keep those
  // callers compatible while ensuring proxy's UUID-backed DB lookup is valid.
  return !userId || userId === "user-1" ? E2E_USER_ID : userId;
}

function getDatabaseUrl() {
  const configured = process.env.DATABASE_URL || readEnvValue("DATABASE_URL");
  if (configured) return configured;
  const user = process.env.POSTGRES_USER || readEnvValue("POSTGRES_USER") || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || readEnvValue("POSTGRES_PASSWORD") || "";
  const host = process.env.POSTGRES_HOST || readEnvValue("POSTGRES_HOST") || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || readEnvValue("POSTGRES_PORT") || "5432";
  const database = process.env.POSTGRES_DB || readEnvValue("POSTGRES_DB") || "aoitalk_memory";
  return `postgresql://${encodeURIComponent(user)}:${encodeURIComponent(password)}@${host}:${port}/${encodeURIComponent(database)}`;
}

export async function ensureE2EUser() {
  const sql = postgres(getDatabaseUrl(), { max: 1 });
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
        is_password_reset_required, session_version, created_at, updated_at
      ) values (
        ${E2E_USER_ID}::uuid, ${E2E_USERNAME}, ${"e2e-not-a-login-password"},
        ${"Playwright E2E"}, ${"admin"}, true, false, 1, now(), now()
      )
      on conflict (id) do update set
        role = excluded.role,
        is_active = true,
        is_password_reset_required = false,
        session_version = 1,
        updated_at = now()
    `;
  } finally {
    await sql.end();
  }
}

export async function deactivateE2EUser() {
  const sql = postgres(getDatabaseUrl(), { max: 1 });
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

    if (url.pathname === "/api/python-proxy/conversations/session-e2e/dispatch") {
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
