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
  process.env.NEXTAUTH_SECRET || readEnvValue("NEXTAUTH_SECRET") || "fallback-secret",
);

async function createSessionToken(userId: string) {
  return await new SignJWT({ sub: userId, username: "tester", role: "admin" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime("7d")
    .sign(SECRET);
}

const participants = [
  {
    id: "participant-1",
    user_id: "user-1",
    display_name: "Tester",
    role: "player",
    participant_kind: "human",
    avatar_url: "",
    color: "#2563eb",
    seat_index: 0,
    pc_state: { hp: 12, max_hp: 12, mp: 8, max_mp: 8, sanity: 55, luck: 60 },
    is_active_participant: true,
    is_connected: true,
  },
  {
    id: "participant-2",
    user_id: "user-2",
    display_name: "GM",
    role: "gm",
    participant_kind: "human",
    avatar_url: "",
    color: "#16a34a",
    seat_index: 1,
    pc_state: {},
    is_active_participant: true,
    is_connected: true,
  },
];

const logs = Array.from({ length: 90 }, (_, index) => ({
  id: `log-${index + 1}`,
  play_session_id: "room-1",
  participant_id: index % 3 === 0 ? "participant-1" : null,
  log_type: index % 3 === 0 ? "speech" : "narration",
  content: `Long log line ${String(index + 1).padStart(3, "0")}`,
  metadata: {},
  created_at: "2026-05-04T00:00:00.000Z",
}));

const room = {
  id: "room-1",
  room_code: "ABC123",
  room_title: "Scroll Test Room",
  status: "playing",
  max_players: 4,
  gm_mode: "human",
  is_multiplayer: true,
  is_public: false,
  turn_order: ["participant-1", "participant-2"],
  current_turn_participant_id: "participant-1",
  shared_state: {},
  scenario: {
    id: "scenario-1",
    title: "TRPG Scenario",
    description: "",
    opening_text: "",
    scenario_kind: "trpg",
    ruleset: "coc6",
    genre: "trpg",
    tags: [],
  },
  current_scene: {
    id: "scene-1",
    title: "Scene 1",
    description: "",
    image_prompt: "",
  },
  participants,
  logs,
};

test("TRPG room keeps long logs inside the dedicated scroll area", async ({ page }) => {
  const runtimeErrors: string[] = [];
  const apiRequests: string[] = [];
  let leaveRequests = 0;
  page.on("pageerror", (error) => runtimeErrors.push(error.stack || error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    runtimeErrors.push(`${request.method()} ${request.url()} ${request.failure()?.errorText}`);
  });

  await page.context().addCookies([
    {
      name: "aoitalk_session",
      value: await createSessionToken("user-1"),
      domain: "127.0.0.1",
      path: "/",
    },
  ]);

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    apiRequests.push(url.pathname + url.search);

    if (url.pathname === "/api/auth/status") {
      await route.fulfill({
        json: {
          authenticated: true,
          user: { id: "user-1", username: "tester", role: "admin" },
        },
      });
      return;
    }

    if (
      url.pathname === "/api/python-proxy/api/trpg/rooms/room-1" ||
      url.pathname === "/api/python-proxy/api/trpg/rooms/room-1/"
    ) {
      await route.fulfill({ json: room });
      return;
    }

    if (url.pathname === "/api/python-proxy/api/trpg/rooms/room-1/leave/participant-1") {
      leaveRequests += 1;
      await route.fulfill({ json: { ok: true } });
      return;
    }

    if (url.pathname === "/api/python-proxy/api/trpg/rooms/room-1/disclosures") {
      await route.fulfill({ json: { disclosures: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/api/trpg/rooms/room-1/private-messages") {
      await route.fulfill({ json: { messages: [] } });
      return;
    }

    if (url.pathname === "/api/python-proxy/api/trpg/rooms/room-1/player-sheets") {
      await route.fulfill({ json: { sheets: [] } });
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

    if (url.pathname === "/api/notifications") {
      await route.fulfill({ json: [] });
      return;
    }

    if (url.pathname === "/api/python-proxy/runtime/features") {
      await route.fulfill({
        json: {
          features: {
            local_mic: false,
            local_speaker: false,
            tts: false,
            discord_bot: false,
            discord_text: false,
            discord_vc_input: false,
            discord_vc_output: false,
            console_input: true,
          },
        },
      });
      return;
    }

    await route.fulfill({ json: {} });
  });

  await page.addInitScript(() => {
    localStorage.setItem("trpg-participant-room-1", "participant-1");
  });

  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/trpg/rooms/room-1");
  await page.waitForTimeout(1000);

  const bodyText = await page.locator("body").innerText();
  if (bodyText.includes("couldn")) {
    throw new Error(
      `TRPG room failed to render:\n${runtimeErrors.join("\n")}\n\nAPI requests:\n${apiRequests.join("\n")}\n\n${bodyText}`,
    );
  }

  await expect(page.getByText("参加者 (2)")).toBeVisible();
  const logScroll = page.getByTestId("trpg-log-scroll");
  await expect(logScroll).toBeVisible();

  const initialMetrics = await logScroll.evaluate((element) => {
    const main = document.querySelector("main");
    return {
      logClientHeight: element.clientHeight,
      logScrollHeight: element.scrollHeight,
      logOverflowY: getComputedStyle(element).overflowY,
      mainClientHeight: main?.clientHeight ?? 0,
      mainScrollHeight: main?.scrollHeight ?? 0,
      mainScrollTop: main?.scrollTop ?? 0,
    };
  });

  expect(initialMetrics.logScrollHeight).toBeGreaterThan(initialMetrics.logClientHeight);
  expect(initialMetrics.logOverflowY).toBe("auto");
  expect(initialMetrics.mainScrollHeight).toBeLessThanOrEqual(
    initialMetrics.mainClientHeight + 2,
  );
  expect(initialMetrics.mainScrollTop).toBe(0);

  await logScroll.hover();
  await page.mouse.wheel(0, -2400);

  const afterWheelMetrics = await logScroll.evaluate((element) => {
    const main = document.querySelector("main");
    return {
      logScrollTop: element.scrollTop,
      mainScrollTop: main?.scrollTop ?? 0,
    };
  });

  expect(afterWheelMetrics.logScrollTop).toBeLessThan(initialMetrics.logScrollHeight);
  expect(afterWheelMetrics.mainScrollTop).toBe(0);
  await expect(page.getByText("参加者 (2)")).toBeVisible();
  await expect(page.getByText("開示情報", { exact: true })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/trpg$/);
  expect(leaveRequests).toBe(1);
});
