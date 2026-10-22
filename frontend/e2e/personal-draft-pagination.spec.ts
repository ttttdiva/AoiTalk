import { expect, test, type Page, type Route } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import type { components } from "../src/lib/api-types.gen";

type Draft = components["schemas"]["PersonalDraftView"];
const BASE_PATH = (process.env.PLAYWRIGHT_BASE_PATH?.trim() || process.env.NEXT_PUBLIC_BASE_PATH?.trim() || "")
  .replace(/^\/+|\/+$/g, "");
const uid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const draft = (n: number, status = "awaiting_user_review"): Draft => ({
  schema: 1, revision: 1, draft_ref: uid(n), evidence_ref: uid(1000 + n),
  confirmed_digest: "a".repeat(64), proposed_by: "agent", status,
  target_id: status === "accepted" ? uid(2000 + n) : null,
  proposal: { kind: "inventory", data: { subject_key: `item_${n}`, entry_type: "consumption",
    quantity: 2, unit: "pcs", occurred_on: "2026-10-20" } },
  evidence: { source_text: "consumption 2 pcs 2026-10-20", source_sha256: "b".repeat(64),
    extractor_version: "owner_explicit_text_v1", field_spans: {} },
});
const firstPage = () => Array.from({ length: 50 }, (_, index) => draft(index + 1,
  index % 2 ? "accepted" : "discarded"));
function gate() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}
async function setup(page: Page, handler: (route: Route) => Promise<void>) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
  const errors: string[] = [], consoleErrors: string[] = [], failures: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (entry) => { if (entry.type() === "error" && !entry.location().url.includes("/api/python-proxy/health")) consoleErrors.push(`${entry.text()} ${entry.location().url}`); });
  page.on("requestfailed", (request) => failures.push(`${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText}`));
  await page.route("**/api/python-proxy/personal-ledger/**", async (route) => {
    await route.fulfill({ json: new URL(route.request().url()).pathname.endsWith("/state")
      ? { deadlines: [], inventory: [] } : { entries: [], next_before_seq: null } });
  });
  await page.route("**/api/python-proxy/personal-drafts**", handler);
  return { errors, consoleErrors, failures };
}
async function open(page: Page) {
  await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
  await expect(page.getByRole("article", { name: "選択した候補" })).toBeVisible();
  await expect(page.getByRole("list", { name: "候補一覧" }).getByRole("button")).toHaveCount(50);
}
const list = (page: Page) => page.getByRole("list", { name: "候補一覧" });
const more = (page: Page) => page.getByRole("button", { name: "候補をさらに表示", exact: true });

test("older pending drafts beyond 50 mixed terminals are reachable and accepted with the reviewed digest", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const initial = firstPage(), older = draft(53);
  const decisions: unknown[] = [], cursors: Array<string | null> = [];
  const evidence = await setup(page, async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST") {
      decisions.push(route.request().postDataJSON());
      older.status = "accepted"; older.target_id = uid(2053);
      await route.fulfill({ json: { draft_id: older.draft_ref, status: "accepted", target_kind: "inventory",
        target_id: older.target_id, replayed: false } });
    } else if (url.pathname.endsWith("/personal-drafts")) {
      const cursor = url.searchParams.get("cursor"); cursors.push(cursor);
      await route.fulfill({ json: { items: cursor === uid(52) ? [draft(52), older] : cursor ? [initial[49], draft(51), draft(52)] : initial,
        next_cursor: cursor === uid(52) ? null : cursor ? uid(52) : uid(50) } });
    } else await route.fulfill({ json: older });
  });
  await open(page);
  await more(page).click();
  await expect(list(page).getByRole("button")).toHaveCount(52);
  await more(page).click();
  await expect(list(page).getByRole("button")).toHaveCount(53);
  await expect(more(page)).toHaveCount(0);
  await list(page).getByRole("button", { name: /item_53 · 在庫/ }).click();
  await expect(page.getByTestId("draft-status")).toHaveText("本人の受理待ち");
  await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
  await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0]).toEqual({ confirmed_digest: "a".repeat(64), idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/) });
  await expect(page.getByRole("status")).toContainText("受理して台帳に反映しました");
  expect(cursors.slice(0, 3)).toEqual([null, uid(50), uid(52)]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(evidence.errors).toEqual([]); expect(evidence.consoleErrors).toEqual([]); expect(evidence.failures).toEqual([]);
});

test("load-more retries the same cursor, serializes repeated clicks and preserves newer selection", async ({ page }) => {
  const initial = firstPage(), blocked = gate();
  let attempts = 0;
  const evidence = await setup(page, async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.has("cursor")) {
      attempts++;
      if (attempts === 1) { await route.fulfill({ status: 503, json: { detail: "synthetic failure" } }); return; }
      await blocked.promise;
      await route.fulfill({ json: { items: [initial[49], draft(51)], next_cursor: null } });
    } else await route.fulfill({ json: { items: initial, next_cursor: uid(50) } });
  });
  try {
    await open(page); await more(page).click();
    await expect(page.getByRole("status")).toContainText("候補の続きを取得できませんでした");
    await expect(list(page).getByRole("button")).toHaveCount(50);
    await more(page).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
    await expect.poll(() => attempts).toBe(2);
    await list(page).getByRole("button", { name: /item_2 · 在庫/ }).click();
    blocked.release();
    await expect(list(page).getByRole("button")).toHaveCount(51);
    await expect(list(page).getByRole("button", { name: /item_2 · 在庫/ })).toHaveAttribute("aria-pressed", "true");
    expect(attempts).toBe(2); expect(evidence.errors).toEqual([]);
    expect(evidence.consoleErrors.filter((message) => !message.includes("503"))).toEqual([]); expect(evidence.failures).toEqual([]);
  } finally { blocked.release(); }
});

test("a refreshed first page supersedes a delayed continuation and its cursor", async ({ page }) => {
  const blocked = gate(); let firstReads = 0, continuationArrived = false;
  const evidence = await setup(page, async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.has("cursor")) {
      continuationArrived = true; await blocked.promise;
      await route.fulfill({ json: { items: [draft(51)], next_cursor: uid(51) } });
    } else {
      firstReads++;
      await route.fulfill({ json: { items: firstReads === 1 ? firstPage() : [draft(99)],
        next_cursor: firstReads === 1 ? uid(50) : null } });
    }
  });
  try {
    await open(page); await more(page).click();
    await expect.poll(() => continuationArrived).toBe(true);
    await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
    await expect(list(page).getByRole("button")).toHaveCount(1);
    await expect(list(page).getByRole("button", { name: /item_99 · 在庫/ })).toHaveAttribute("aria-pressed", "true");
    const completed = page.waitForResponse((response) => new URL(response.url()).searchParams.has("cursor"));
    blocked.release(); await completed;
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(list(page).getByRole("button")).toHaveCount(1); await expect(more(page)).toHaveCount(0);
    expect(evidence.errors).toEqual([]); expect(evidence.consoleErrors).toEqual([]); expect(evidence.failures).toEqual([]);
  } finally { blocked.release(); }
});

test("an authoritative terminal from a continuation wins over a contradictory delayed POST", async ({ page }) => {
  const initial = [draft(1), ...firstPage().slice(1)], blocked = gate(); let postStarted = false;
  const evidence = await setup(page, async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST") {
      postStarted = true; await blocked.promise;
      await route.fulfill({ json: { draft_id: uid(1), status: "accepted", target_kind: "inventory",
        target_id: uid(2001), replayed: false } });
    } else if (!url.pathname.endsWith("/personal-drafts")) await route.fulfill({ json: initial[0] });
    else await route.fulfill({ json: { items: url.searchParams.has("cursor") ? [draft(1, "discarded"), draft(51)] : initial,
      next_cursor: url.searchParams.has("cursor") ? null : uid(50) } });
  });
  try {
    await open(page);
    await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
    await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
    await expect.poll(() => postStarted).toBe(true); await more(page).click();
    await expect(page.getByTestId("draft-status")).toHaveText("破棄済み");
    const completed = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith("/accept"));
    blocked.release(); await completed;
    await expect(page.getByRole("status")).toContainText("元の受理操作は成立していません");
    await expect(page.getByTestId("draft-status")).toHaveText("破棄済み"); await expect(page.getByTestId("personal-drafts").getByRole("alert")).toHaveCount(0);
    await list(page).getByRole("button", { name: /item_51 · 在庫/ }).click();
    await expect(page.getByRole("checkbox")).toBeEnabled();
    expect(evidence.errors).toEqual([]); expect(evidence.consoleErrors).toEqual([]); expect(evidence.failures).toEqual([]);
  } finally { blocked.release(); }
});
