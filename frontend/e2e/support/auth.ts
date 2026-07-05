import type { Page } from "@playwright/test";
import { SignJWT } from "jose";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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

export async function createSessionToken(userId = "user-1") {
  return await new SignJWT({ sub: userId, username: "tester", role: "admin" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

export async function addAuthCookie(page: Page, userId = "user-1") {
  await page.context().addCookies([
    {
      name: "aoitalk_session",
      value: await createSessionToken(userId),
      domain: "127.0.0.1",
      path: "/",
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
          user: { id: "user-1", username: "tester", role: "admin" },
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
