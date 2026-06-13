import { expect, test } from "@playwright/test";
import { SignJWT } from "jose";
import fs from "node:fs";
import path from "node:path";

const importedScenario = {
  id: "scenario-1",
  title: "F02_Unfeart-R",
  description: "Imported screenplay scenario",
  genre: "drama",
  perspective: "third_person",
  setting: "近未来",
  opening_text: "",
  tags: ["imported"],
  difficulty: "normal",
  created_at: "2026-04-28T00:00:00",
  updated_at: "2026-04-28T00:00:00",
};

const trpgScenario = {
  id: "scenario-trpg-1",
  title: "長文TRPGシナリオ",
  scenario_kind: "trpg",
  ruleset: "coc6",
  description: "TRPG document scenario",
  genre: "trpg",
  perspective: "third_person",
  setting: "閉ざされた洋館",
  opening_text: "",
  tags: ["trpg"],
  difficulty: "normal",
  created_at: "2026-04-28T00:00:00",
  updated_at: "2026-04-28T00:00:00",
};

const longTrpgSource = Array.from(
  { length: 80 },
  (_, index) => `# 部屋${index + 1}\n探索情報と描写。重要な手掛かりを含む。`,
).join("\n\n");

const trpgDocument = {
  id: "trpg-document-1",
  scenario_id: trpgScenario.id,
  ruleset: "coc6",
  source_label: "fixture.md",
  source_text: longTrpgSource,
  structure: {
    version: 1,
    nodes: [
      {
        id: "location_hall",
        type: "location",
        title: "玄関ホール",
        summary: "探索開始地点",
        body: "扉と足跡がある。",
        tags: ["探索"],
        metadata: {},
      },
    ],
    links: [],
    metadata: {},
  },
  created_at: "2026-04-28T00:00:00",
  updated_at: "2026-04-28T00:00:00",
};

const importedCharacter = {
  id: "character-1",
  scenario_id: importedScenario.id,
  name: "琴葉葵",
  role: "protagonist",
  description: "インポート済みキャラクター",
  importance: 0,
  speech_pattern: "穏やかに話す。",
  psychology: "",
  backstory: "",
  relationships: "[]",
  arc: "",
  dialogue_samples: "",
};

const importedEpisode = {
  id: "episode-1",
  scenario_id: importedScenario.id,
  title: "第1話",
  one_line_summary: "",
  paragraph_summary: "",
  full_summary: "",
  status: "draft",
  beat_sheet: "",
  sort_order: 0,
};

function readNextAuthSecret() {
  if (process.env.NEXTAUTH_SECRET) return process.env.NEXTAUTH_SECRET;

  const envPath = path.resolve(process.cwd(), ".env");
  if (fs.existsSync(envPath)) {
    const line = fs
      .readFileSync(envPath, "utf8")
      .split(/\r?\n/)
      .find((entry) => entry.startsWith("NEXTAUTH_SECRET="));
    if (line) return line.slice("NEXTAUTH_SECRET=".length);
  }

  return "fallback-secret";
}

async function createSessionCookie() {
  const secret = new TextEncoder().encode(readNextAuthSecret());
  return new SignJWT({ sub: "user-1", username: "tester", role: "admin" })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime("1h")
    .sign(secret);
}

test.describe("シナリオ詳細タブ", () => {
  test.beforeEach(async ({ page }) => {
    await page.context().addCookies([
      {
        name: "aoitalk_session",
        value: await createSessionCookie(),
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

      if (url.pathname === "/api/python-proxy/scenarios") {
        await route.fulfill({
          json: { scenarios: [importedScenario, trpgScenario] },
        });
        return;
      }

      if (url.pathname === "/api/spaces") {
        await route.fulfill({ json: { spaces: [], total: 0 } });
        return;
      }

      if (url.pathname === "/api/projects") {
        await route.fulfill({ json: { projects: [], total: 0 } });
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

      if (url.pathname === `/api/python-proxy/scenarios/${importedScenario.id}`) {
        await route.fulfill({
          json: {
            ...importedScenario,
            characters: [importedCharacter],
            scenes: [],
            episodes: [importedEpisode],
          },
        });
        return;
      }

      if (url.pathname === `/api/python-proxy/scenarios/${trpgScenario.id}`) {
        await route.fulfill({
          json: {
            ...trpgScenario,
            characters: [],
            scenes: [],
            episodes: [],
            trpg_documents: [trpgDocument],
          },
        });
        return;
      }

      if (
        url.pathname ===
        `/api/python-proxy/scenarios/${importedScenario.id}/episodes`
      ) {
        await route.fulfill({ json: { episodes: [importedEpisode] } });
        return;
      }

      if (url.pathname === "/api/conversations") {
        await route.fulfill({ json: { conversations: [] } });
        return;
      }

      if (url.pathname === "/api/notifications") {
        await route.fulfill({ json: [] });
        return;
      }

      await route.fulfill({ json: {} });
    });
  });

  test("直接返却されたインポート済みシナリオ詳細からキャラクターを表示できる", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 960, height: 720 });

    const runtimeErrors: string[] = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });

    await page.goto("/scenarios");
    await page.getByText(importedScenario.title).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const dialogBox = await dialog.boundingBox();
    expect(dialogBox?.width).toBeGreaterThan(900);
    await expect
      .poll(() =>
        dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)
      )
      .toBe(true);

    await page.getByRole("tab", { name: /キャラクター/ }).click();

    await expect(
      page.getByText(importedCharacter.name),
      runtimeErrors.join("\n"),
    ).toBeVisible();
    await expect(page.getByText("先にシナリオを保存してください")).toBeHidden();
  });

  test("キャラクター行クリックでその行の直下に詳細フォームを展開できる", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 960, height: 720 });

    const runtimeErrors: string[] = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });

    await page.goto("/scenarios");
    await page.getByText(importedScenario.title).click();
    await page.getByRole("tab", { name: /キャラクター/ }).click();

    const dialog = page.getByRole("dialog");
    const characterRow = dialog.getByRole("button", {
      name: /琴葉葵/,
    });
    await expect(characterRow).toHaveAttribute("aria-expanded", "false");

    const rowBox = await characterRow.boundingBox();
    await characterRow.click();
    await expect(characterRow).toHaveAttribute("aria-expanded", "true");

    const nameInput = dialog.getByPlaceholder("キャラクター名");
    await expect(nameInput).toBeVisible();
    await expect(nameInput).toHaveValue(importedCharacter.name);

    const inputBox = await nameInput.boundingBox();
    expect(inputBox?.y).toBeGreaterThan(rowBox?.y ?? 0);
    await expect(dialog.getByText("CoCキャラクターシート")).toBeHidden();
    expect(runtimeErrors).toEqual([]);
  });

  test("TRPG本文タブは長文エディタをスクロール領域内に表示する", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 960, height: 720 });

    const runtimeErrors: string[] = [];
    page.on("pageerror", (error) => runtimeErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") runtimeErrors.push(message.text());
    });

    await page.goto("/scenarios");
    await page.getByText(trpgScenario.title).click();
    await page.getByRole("tab", { name: /TRPG本文/ }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.locator(".cm-editor").first()).toBeVisible();
    await expect(dialog.locator(".cm-content").first()).toContainText("部屋1");

    const tabPanel = page.getByRole("tabpanel", { name: "TRPG本文" });
    await expect(tabPanel).toHaveClass(/overflow-y-auto/);
    await expect
      .poll(() =>
        dialog.locator(".cm-scroller").first().evaluate((element) => ({
          overflowY: getComputedStyle(element).overflowY,
          scrollable: element.scrollHeight > element.clientHeight,
        })),
      )
      .toEqual({ overflowY: "auto", scrollable: true });

    expect(runtimeErrors).toEqual([]);
  });
});
