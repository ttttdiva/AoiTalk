import { expect, test, type Page } from "@playwright/test";

import {
  assertRegularSession,
  loadLiveAdmin,
  loginThroughUi,
  logoutThroughUi,
  type LiveUserCredentials,
} from "./support/live-auth";
import {
  assertNoLiveObservabilityIssues,
  attachLiveObservability,
  type LiveObservability,
} from "./support/live-observability";
import {
  assertVerificationStorageIsolated,
  cleanupVerificationRun,
  createVerificationRun,
  installVerificationRun,
} from "../e2e/support/auth";

const liveAdmin = loadLiveAdmin();

async function fillDialogField(page: Page, label: string, value: string) {
  const dialog = page.getByRole("dialog");
  const field = dialog
    .locator("div.space-y-1\\.5")
    .filter({ hasText: label })
    .first();
  await expect(field, `${label} field must be present`).toBeVisible();
  await field.locator("input").first().fill(value);
}

async function openUserManagement(page: Page) {
  await page.goto("/settings");
  const navigation = page.getByRole("navigation", { name: "設定カテゴリ" });
  await expect(navigation).toBeVisible({ timeout: 20_000 });
  await navigation.getByText("管理・運用", { exact: true }).click();
  await page.getByText("ユーザー管理", { exact: true }).first().click();
  await expect(
    page.getByRole("button", { name: "ユーザー追加", exact: true }),
  ).toBeVisible({
    timeout: 20_000,
  });
}

async function createRegularUserViaUi(
  page: Page,
  username: string,
  password: string,
): Promise<string> {
  await page.getByRole("button", { name: "ユーザー追加", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await fillDialogField(page, "ユーザー名", username);
  await fillDialogField(page, "初期ログインパスワード", password);
  const roleSelect = dialog.getByRole("combobox").first();
  await expect(roleSelect).toContainText("user");
  await roleSelect.click();
  await page.getByRole("option", { name: "user", exact: true }).click();

  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/users" &&
      response.request().method() === "POST",
    { timeout: 20_000 },
  );
  await dialog.getByRole("button", { name: "追加", exact: true }).click();
  const response = await responsePromise;
  expect(
    response.status(),
    "creating the onboarding user must succeed",
  ).toBeGreaterThanOrEqual(200);
  expect(response.status()).toBeLessThan(300);
  const payload = (await response.json()) as Record<string, unknown>;
  const user =
    payload.user && typeof payload.user === "object"
      ? (payload.user as Record<string, unknown>)
      : payload;
  expect(user.role, "the UI-created onboarding account must be role=user").toBe(
    "user",
  );
  expect(
    user.password_reset_required,
    "the UI-created onboarding account must require an initial password change",
  ).toBe(true);
  const id = typeof user.id === "string" ? user.id : "";
  expect(id, "the create response must include a user id for cleanup").not.toBe(
    "",
  );
  await expect(dialog).toBeHidden({ timeout: 10_000 });
  await expect(page.getByText(`@${username}`, { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  return id;
}

async function cleanupUser(page: Page, userId: string) {
  // UserRepository's canonical lifecycle is soft-delete followed by an
  // explicit purge.  Execute both steps unconditionally for this unique
  // fixture identity; a soft-delete without the subsequent purge would leave
  // verification garbage as an inactive account.
  const softDelete = await page.request.delete(
    `/api/users/${encodeURIComponent(userId)}`,
    {
      failOnStatusCode: false,
    },
  );
  if (softDelete.status() === 404) return;
  if (!softDelete.ok()) {
    throw new Error(
      `onboarding cleanup failed: canonical soft-delete HTTP ${softDelete.status()} ${await softDelete.text()}`,
    );
  }
  const purge = await page.request.delete(
    `/api/users/${encodeURIComponent(userId)}/purge`,
    {
      failOnStatusCode: false,
    },
  );
  if (purge.ok() || purge.status() === 404) return;
  throw new Error(
    `onboarding cleanup failed: canonical purge HTTP ${purge.status()} ${await purge.text()}`,
  );
}

async function ensureAdminSession(page: Page) {
  const statusResponse = await page.request.get("/api/auth/status", {
    failOnStatusCode: false,
  });
  const status = statusResponse.ok()
    ? ((await statusResponse.json().catch(() => null)) as {
        authenticated?: boolean;
        user?: { role?: string | null } | null;
      } | null)
    : null;
  if (status?.authenticated === true && status.user?.role === "admin") return;
  await loginThroughUi(page, liveAdmin!, { expectedRole: "admin" });
}

test.describe("regular user onboarding live flow", () => {
  test("admin creates role=user, initial password changes in UI, and the new session relogins", async ({
    browser,
    page,
  }) => {
    test.skip(!liveAdmin, "live admin credentials are not available");
    test.skip(
      (process.env.AOITALK_VERIFICATION_HARNESS_KEY?.trim().length ?? 0) < 16,
      "AOITALK_VERIFICATION_HARNESS_KEY is required for provenance-tagged live writes",
    );
    // The onboarding flow deliberately exercises real user creation. Refuse
    // to run it against the ordinary application database even when a cleanup
    // endpoint is unavailable; use a prefixed ephemeral DB or non-public
    // schema configured by the harness instead.
    assertVerificationStorageIsolated();

    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const username = `e2e_onboarding_${suffix}`;
    const initialPassword = `Init_${suffix}!x`;
    const newPassword = `Next_${suffix}!y`;
    const createdCredentials: LiveUserCredentials = {
      username,
      password: initialPassword,
    };
    const verificationRun = createVerificationRun(
      "frontend/e2e-live/onboarding-regular-user.spec.ts",
    );
    await installVerificationRun(page, verificationRun);
    let userId = "";
    const adminIssues = attachLiveObservability(page);
    const userContext = await browser.newContext();
    const userPage = await userContext.newPage();
    await installVerificationRun(userPage, verificationRun);
    let userIssues: LiveObservability | null = null;

    try {
      await loginThroughUi(page, liveAdmin!, { expectedRole: "admin" });
      await openUserManagement(page);
      userId = await createRegularUserViaUi(page, username, initialPassword);
      await logoutThroughUi(page);

      const firstLogin = await loginThroughUi(userPage, createdCredentials, {
        expectedRole: "user",
        allowPasswordReset: true,
      });
      expect(firstLogin.user?.password_reset_required).toBe(true);
      await expect(userPage).toHaveURL(/\/change-password(?:\?|$)/);
      await userPage.locator("#new-password").fill(newPassword);
      await userPage.locator("#confirm-password").fill(newPassword);
      await userPage
        .getByRole("button", { name: "パスワードを設定", exact: true })
        .click();
      await userPage.waitForURL(/\/chat(?:\?|$)/, { timeout: 20_000 });
      // The reset flow replaces the session cookie while the old /chat tree
      // is still unmounting; observe only the stable authenticated phase.
      userIssues = attachLiveObservability(userPage);
      await assertRegularSession(userPage, username);
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await userPage.reload({ waitUntil: "domcontentloaded" });
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await assertRegularSession(userPage, username);

      assertNoLiveObservabilityIssues(
        userIssues,
        "regular-user onboarding initial authenticated session",
      );
      await logoutThroughUi(userPage);
      const reloginIssues = attachLiveObservability(userPage);
      const relogin = await loginThroughUi(
        userPage,
        { username, password: newPassword },
        { expectedRole: "user" },
      );
      expect(relogin.user?.password_reset_required).not.toBe(true);
      await expect(userPage).toHaveURL(/\/chat(?:\?|$)/);
      await assertRegularSession(userPage, username);
      assertNoLiveObservabilityIssues(
        reloginIssues,
        "regular-user onboarding relogin session",
      );
      await logoutThroughUi(userPage);
    } finally {
      try {
        await userContext.close();
      } finally {
        try {
          // The server-owned run cleanup is the primary teardown.  Purge the
          // unique user through its canonical soft-delete + purge lifecycle;
          // never retain an inactive soft-delete as a fallback.
          await ensureAdminSession(page);
          try {
            await cleanupVerificationRun(page, verificationRun);
          } finally {
            // The server closes a run after cleanup and rejects any subsequent
            // non-maintenance request that still carries its signed headers.
            // Clear page-level extras before the canonical user purge/logout;
            // those operations must not be interpreted as writes in a
            // terminal verification run.
            await page.setExtraHTTPHeaders({});
            if (userId) {
              try {
                await cleanupUser(page, userId);
              } finally {
                await logoutThroughUi(page);
              }
            }
          }
        } finally {
          // Even if admin re-authentication or run cleanup fails, never leave
          // a terminal-run header on the page (the next teardown/retry could
          // otherwise be rejected by verification middleware).
          await page.setExtraHTTPHeaders({});
        }
        assertNoLiveObservabilityIssues(
          adminIssues,
          "admin onboarding session",
        );
      }
    }
  });
});
