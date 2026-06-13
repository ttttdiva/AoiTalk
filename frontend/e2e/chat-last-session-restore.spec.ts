import { expect, test } from "@playwright/test";
import { SignJWT } from "jose";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const LAST_SESSION_KEY = "aoitalk_last_session_id";

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
  process.env.NEXTAUTH_SECRET || readEnvValue("NEXTAUTH_SECRET") || "fallback-secret",
);

async function createSessionToken(userId: string) {
  return await new SignJWT({ sub: userId })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

async function mockCommonApis(page: import("@playwright/test").Page) {
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

    if (url.pathname === "/api/conversations") {
      await route.fulfill({ json: { conversations: [], total: 0 } });
      return;
    }

    if (url.pathname === "/api/conversations/session-scenario/resume") {
      await route.fulfill({
        json: {
          session: {
            id: "session-scenario",
            user_id: "user-1",
            title: "[シナリオ] F02_Unfeart-R",
            character_name: "scenario_F02_Unfeart-R",
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

    if (url.pathname === "/api/conversations/session-normal/resume") {
      await route.fulfill({
        json: {
          session: {
            id: "session-normal",
            user_id: "user-1",
            title: "通常チャット",
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

    await route.fulfill({ json: {} });
  });
}

test.describe("チャットの最終セッション復元", () => {
  test.beforeEach(async ({ page }) => {
    const token = await createSessionToken("user-1");
    await page.context().addCookies([
      {
        name: "aoitalk_session",
        value: token,
        domain: "127.0.0.1",
        path: "/",
      },
    ]);
    await mockCommonApis(page);
  });

  test("保存済み最終セッションがシナリオの場合は通常チャット入口で復元しない", async ({
    page,
  }) => {
    await page.addInitScript((key) => {
      localStorage.setItem(key, "session-scenario");
    }, LAST_SESSION_KEY);

    await page.goto("/chat");
    await expect(page).toHaveURL(/\/chat$/);
    await expect(page.getByText("Scenario")).toHaveCount(0);
    await expect
      .poll(() => page.evaluate((key) => localStorage.getItem(key), LAST_SESSION_KEY))
      .toBeNull();
  });

  test("保存済み最終セッションが通常チャットの場合は復元する", async ({
    page,
  }) => {
    await page.addInitScript((key) => {
      localStorage.setItem(key, "session-normal");
    }, LAST_SESSION_KEY);

    await page.goto("/chat");
    await expect(page).toHaveURL(/\/chat\?s=session-normal$/);
  });
});
