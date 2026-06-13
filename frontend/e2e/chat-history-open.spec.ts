import { expect, test } from "@playwright/test";
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

async function createSessionToken(userId: string) {
  return await new SignJWT({ sub: userId })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

test("opens a conversation from sidebar history", async ({ page }) => {
  const sessionId = "session-history";
  const token = await createSessionToken("user-1");
  await page.context().addCookies([
    {
      name: "aoitalk_session",
      value: token,
      domain: "127.0.0.1",
      path: "/",
    },
  ]);

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());

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

    if (url.pathname === "/api/spaces") {
      await route.fulfill({ json: { spaces: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/python-proxy/health") {
      await route.fulfill({ json: { status: "ok" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/characters") {
      await route.fulfill({ json: { characters: [], current: "" } });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/engine") {
      await route.fulfill({
        json: { available: [], provider: "", model: "" },
      });
      return;
    }

    if (url.pathname === "/api/python-proxy/llm/mode") {
      await route.fulfill({ json: { mode: "", available_modes: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/runtime/features") {
      await route.fulfill({
        json: {
          features: {
            local_mic: false,
            tts: false,
            discord_bot: false,
          },
        },
      });
      return;
    }

    if (url.pathname === "/api/conversations") {
      await route.fulfill({
        json: {
          conversations: [
            {
              id: sessionId,
              user_id: "user-1",
              title: "History item",
              character_name: "aoi",
              last_activity: "2026-05-04T10:00:00.000Z",
              message_count: 2,
              is_active: true,
              is_group_chat: false,
              group_character_names: [],
            },
          ],
          total: 1,
        },
      });
      return;
    }

    if (url.pathname === `/api/conversations/${sessionId}/resume`) {
      await route.fulfill({
        json: {
          session: {
            id: sessionId,
            user_id: "user-1",
            title: "History item",
            character_name: "aoi",
            message_count: 2,
            is_active: true,
            is_group_chat: false,
            group_character_names: [],
          },
          messages: [
            {
              id: "msg-user",
              session_id: sessionId,
              role: "user",
              content: "Previous question",
              metadata: {},
              branch_index: 0,
              is_active_branch: true,
            },
            {
              id: "msg-assistant",
              session_id: sessionId,
              role: "assistant",
              content: "Previous answer",
              metadata: {},
              branch_index: 0,
              is_active_branch: true,
            },
          ],
        },
      });
      return;
    }

    if (
      url.pathname ===
      `/api/python-proxy/scenarios/logs/by-conversation/${sessionId}`
    ) {
      await route.fulfill({ json: null });
      return;
    }

    await route.fulfill({ json: {} });
  });

  await page.goto("/chat");
  await page.getByText("History item").click();

  await expect(page).toHaveURL(new RegExp(`/chat\\?s=${sessionId}$`));
  await expect(page.getByText("Previous question")).toBeVisible();
  await expect(page.getByText("Previous answer")).toBeVisible();
});
