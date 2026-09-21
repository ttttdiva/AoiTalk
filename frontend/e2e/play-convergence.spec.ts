import { expect, test } from "@playwright/test";
import {
  addAuthCookie,
  assertVerificationStorageIsolated,
  cleanupVerificationRun,
  createVerificationRun,
  installVerificationRun,
} from "./support/auth";

test.describe("Scenario / Roleplay / TRPG 収束確認", () => {
  test.beforeEach(async ({ page }) => {
    await addAuthCookie(page);
  });

  test("Chat / Scenario / TRPG Play が認証付きで開ける", async ({ page }) => {
    await page.goto("/chat");
    await expect(page).not.toHaveURL(/\/login(?:\?|$)/);

    await page.goto("/scenarios");
    await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
    await expect(page.locator("body")).toBeVisible();

    await page.goto("/trpg/play");
    await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
  });

  test("TRPG卓を作成しGM共有パネルを表示する", async ({ page }) => {
    test.skip(
      process.env.E2E_LIVE_PLAY !== "1",
      "live FastAPI が必要な実機確認",
    );
    test.skip(
      (process.env.AOITALK_VERIFICATION_HARNESS_KEY?.trim().length ?? 0) < 16,
      "AOITALK_VERIFICATION_HARNESS_KEY is required for provenance-tagged live writes",
    );
    // Story/TRPG rows are not yet covered by the generic verification
    // cleanup coordinator. Keep this opt-in writer on a fresh *database*
    // rather than risking a normal runtime work/session. A non-public schema
    // alone is insufficient because these rows may not be selected by the
    // server-owned verification cleanup endpoint.
    assertVerificationStorageIsolated({ requireEphemeralDatabase: true });
    const verificationRun = createVerificationRun(
      "frontend/e2e/play-convergence.spec.ts",
    );
    await installVerificationRun(page, verificationRun);
    try {
      await page.goto("/trpg/play");
      // Never reuse a pre-existing work: it may belong to another operator or
      // verification run and cannot be safely attributed to this teardown.
      const createdWork = await page.request.post(
        "/api/python-proxy/story/works",
        {
          data: { title: "E2E収束TRPG", kind: "trpg" },
        },
      );
      expect(
        createdWork.ok(),
        `create work ${createdWork.status()} ${await createdWork.text()}`,
      ).toBeTruthy();
      const workId = ((await createdWork.json()) as { id: string }).id;

      const created = await page.request.post(
        "/api/python-proxy/trpg/sessions",
        {
          data: { work_id: workId, gm_mode: "human", title: "E2E収束卓" },
        },
      );
      expect(
        created.ok(),
        `create session ${created.status()} ${await created.text()}`,
      ).toBeTruthy();
      const session = (await created.json()) as { id: string };

      await page.goto(`/trpg/play/${session.id}`);
      await expect(
        page.getByRole("heading", { name: "E2E収束卓" }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "GM共有の非公開状態" }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "非公開状態", exact: true }),
      ).toBeVisible();
      await expect(page.getByText("観戦モードです")).toHaveCount(0);

      const patched = await page.request.patch(
        `/api/python-proxy/trpg/sessions/${session.id}/private-state`,
        {
          data: {
            state: {
              entries: {
                secret: { value: "X", shared_with_gm: true },
                hidden: { value: "NO", shared_with_gm: false },
              },
            },
          },
        },
      );
      expect(
        patched.ok(),
        `patch private ${patched.status()} ${await patched.text()}`,
      ).toBeTruthy();
      const gmList = await page.request.get(
        `/api/python-proxy/trpg/sessions/${session.id}/private-states`,
      );
      expect(gmList.ok(), `gm list ${gmList.status()}`).toBeTruthy();
      const gmJson = (await gmList.json()) as {
        private_states?: Array<{
          state?: { entries?: Record<string, unknown> };
        }>;
      };
      const entries = gmJson.private_states?.[0]?.state?.entries ?? {};
      expect(entries).toHaveProperty("secret");
      expect(entries).not.toHaveProperty("hidden");
    } finally {
      // Work/session creation is deliberately attributed to a server-owned
      // run.  Cleanup is idempotent and uses explicit provenance rather than
      // the human-readable E2E収束TRPG title.
      await cleanupVerificationRun(page, verificationRun, {
        // The fresh ephemeral DB is the isolation boundary for Story/TRPG
        // rows, which predate the generic verification selector. If the
        // server has no ledger row yet, there is therefore nothing to delete.
        allowMissingSelector: true,
      });
    }
  });
});
