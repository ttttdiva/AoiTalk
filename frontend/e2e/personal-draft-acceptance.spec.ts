import { expect, test, type Page } from "@playwright/test";
import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";
import type { components } from "../src/lib/api-types.gen";

const DRAFT_ID = "00000000-0000-4000-8000-000000000101";
const TARGET_ID = "00000000-0000-4000-8000-000000000301";
const BASE_PATH = (process.env.PLAYWRIGHT_BASE_PATH?.trim() || process.env.NEXT_PUBLIC_BASE_PATH?.trim() || "")
  .replace(/^\/+|\/+$/g, "");

// The API is entirely synthetic. The canonical runner owns authentication setup;
// importing the shared helpers does not connect this spec to a database.
test("late initial ledger state cannot overwrite the balance refreshed after draft acceptance", async ({ page }) => {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
  await page.setViewportSize({ width: 375, height: 812 });

  const draft: components["schemas"]["PersonalDraftView"] = {
    schema: 1, revision: 1, draft_ref: DRAFT_ID,
    evidence_ref: "00000000-0000-4000-8000-000000000201",
    proposal: { kind: "inventory", data: {
      subject_key: "item_a", entry_type: "consumption", quantity: 2,
      unit: "pcs", occurred_on: "2026-10-20",
    } },
    evidence: { source_text: "consumption 2 pcs 2026-10-20",
      source_sha256: "b".repeat(64), extractor_version: "owner_explicit_text_v1", field_spans: {} },
    confirmed_digest: "a".repeat(64), proposed_by: "agent",
    status: "awaiting_user_review", target_id: null,
  };
  let releaseInitial!: () => void;
  const initialGate = new Promise<void>((resolve) => { releaseInitial = resolve; });
  let accepted = false;
  let initialReads = 0;
  let initialResponses = 0;
  let freshReads = 0;
  const decisions: Array<Record<string, unknown>> = [];
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));

  await page.route("**/api/python-proxy/personal-ledger/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/state")) {
      const stale = !accepted;
      if (stale) { initialReads++; await initialGate; }
      else freshReads++;
      await route.fulfill({ json: { deadlines: [], inventory: [{
        subject_key: "item_a", balances: [{ unit: "pcs", balance: stale ? 10 : 8 }], needs_review: [],
      }] } });
      if (stale) initialResponses++;
      return;
    }
    if (path.endsWith("/entries") && route.request().method() === "GET") {
      await route.fulfill({ json: { entries: [], next_before_seq: null } });
      return;
    }
    throw new Error(`unexpected synthetic ledger request: ${route.request().method()} ${path}`);
  });
  await page.route("**/api/python-proxy/personal-drafts**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET") {
      await route.fulfill({ json: path.endsWith("/personal-drafts") ? { items: [draft], next_cursor: null } : draft });
      return;
    }
    expect(path).toMatch(new RegExp(`/${DRAFT_ID}/accept$`));
    decisions.push(route.request().postDataJSON() as Record<string, unknown>);
    accepted = true;
    draft.status = "accepted";
    draft.target_id = TARGET_ID;
    await route.fulfill({ json: { draft_id: DRAFT_ID, status: "accepted", target_kind: "inventory",
      target_id: TARGET_ID, replayed: false } });
  });

  try {
    await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
    await expect(page.getByRole("article", { name: "選択した候補" })).toBeVisible();
    await expect.poll(() => initialReads).toBeGreaterThan(0);
    await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
    await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
    await expect(page.getByTestId("draft-status")).toHaveText("受理済み・台帳反映済み");
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
    expect(freshReads).toBeGreaterThan(0);
    expect(decisions).toHaveLength(1);
    expect(Object.keys(decisions[0]).sort()).toEqual(["confirmed_digest", "idempotency_key"]);

    releaseInitial();
    await expect.poll(() => initialResponses).toBe(initialReads);
    // Allow the completed old fetch and React render to run without a timed sleep.
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
    expect(decisions).toHaveLength(1);
    expect(runtimeErrors).toEqual([]);
  } finally {
    releaseInitial();
  }
});


async function installOtherTabAcceptance(page: Page, delayInitial = false) {
  await addAuthCookie(page);
  await mockAuthenticatedApis(page);
  await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => socket.close());
  await page.setViewportSize({ width: 375, height: 812 });
  const draft: components["schemas"]["PersonalDraftView"] = {
    schema: 1, revision: 1, draft_ref: DRAFT_ID,
    evidence_ref: "00000000-0000-4000-8000-000000000201",
    proposal: { kind: "inventory", data: { subject_key: "item_a", entry_type: "consumption",
      quantity: 2, unit: "pcs", occurred_on: "2026-10-20" } },
    evidence: { source_text: "consumption 2 pcs 2026-10-20", source_sha256: "b".repeat(64),
      extractor_version: "owner_explicit_text_v1", field_spans: {} },
    confirmed_digest: "a".repeat(64), proposed_by: "agent", status: "awaiting_user_review", target_id: null,
  };
  let releaseInitial!: () => void;
  const gate = new Promise<void>((resolve) => { releaseInitial = resolve; });
  const state = { otherTabTerminal: false, staleList: false, omitPendingFromList: false, terminalStatus: "accepted" as "accepted" | "discarded", stateReads: 0, initialReads: 0, initialResponses: 0,
    listReads: 0, preflightReads: 0, posts: 0, errors: [] as string[], releaseInitial };
  page.on("pageerror", (error) => state.errors.push(error.message));
  await page.route("**/api/python-proxy/personal-ledger/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/state")) {
      state.stateReads++;
      const stale = !state.otherTabTerminal;
      if (stale) { state.initialReads++; if (delayInitial) await gate; }
      await route.fulfill({ json: { deadlines: [], inventory: [{ subject_key: "item_a",
        balances: [{ unit: "pcs", balance: stale || state.terminalStatus === "discarded" ? 10 : 8 }], needs_review: [] }] } });
      if (stale) state.initialResponses++;
      return;
    }
    if (path.endsWith("/entries") && route.request().method() === "GET") {
      await route.fulfill({ json: { entries: [], next_before_seq: null } });
      return;
    }
    throw new Error(`unexpected synthetic ledger request: ${route.request().method()} ${path}`);
  });
  await page.route("**/api/python-proxy/personal-drafts**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") {
      state.posts++;
      await route.fulfill({ status: 409, json: { detail: "unexpected duplicate action" } });
      return;
    }
    const value = state.otherTabTerminal ? { ...draft, status: state.terminalStatus,
      target_id: state.terminalStatus === "discarded" ? null : TARGET_ID } : draft;
    if (path.endsWith("/personal-drafts")) { state.listReads++; await route.fulfill({ json: { items: state.omitPendingFromList ? [] : [state.staleList ? draft : value], next_cursor: null } }); }
    else { state.preflightReads++; await route.fulfill({ json: value }); }
  });
  return state;
}

for (const discoveredBy of ["list", "preflight"] as const) {
  test(`other-tab acceptance discovered by ${discoveredBy} refreshes an already displayed balance without a POST or refresh loop`, async ({ page }) => {
    const state = await installOtherTabAcceptance(page);
    await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
    await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
    await expect(page.getByTestId("draft-status")).toHaveText("本人の受理待ち");
    const baselineReads = state.stateReads;
    state.otherTabTerminal = true;
    if (discoveredBy === "list") {
      await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
    } else {
      await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
      await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
    }
    await expect(page.getByTestId("draft-status")).toHaveText("受理済み・台帳反映済み");
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
    expect(state.posts).toBe(0);
    expect(state.stateReads).toBe(baselineReads + 1);

    // The same terminal snapshot may be read repeatedly; reconciliation is once per token.
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = page.waitForResponse((item) => new URL(item.url()).pathname.endsWith("/personal-drafts"));
      await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
      await (await response).finished();
      await expect(page.getByText("候補を取得中…", { exact: true })).toHaveCount(0);
    }
    expect(state.stateReads).toBe(baselineReads + 1);
    expect(state.posts).toBe(0);
    expect(state.errors).toEqual([]);
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
  });
}

test("GET-discovered acceptance refresh survives an older initial parent balance response", async ({ page }) => {
  const state = await installOtherTabAcceptance(page, true);
  try {
    await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
    await expect(page.getByTestId("draft-status")).toHaveText("本人の受理待ち");
    await expect.poll(() => state.initialReads).toBeGreaterThan(0);
    state.otherTabTerminal = true;
    await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
    const lateResponse = page.waitForResponse((item) => new URL(item.url()).pathname.endsWith("/personal-ledger/state"));
    state.releaseInitial();
    await (await lateResponse).finished();
    await expect.poll(() => state.initialResponses).toBe(state.initialReads);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(page.getByTestId("inventory-row")).toContainText("8 pcs");
    expect(state.posts).toBe(0);
    expect(state.errors).toEqual([]);
  } finally { state.releaseInitial(); }
});


test("an authoritative terminal preflight cannot be reverted by a stale awaiting list", async ({ page }) => {
  const state = await installOtherTabAcceptance(page);
  await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
  await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
  state.otherTabTerminal = true;
  await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
  await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
  await expect(page.getByTestId("draft-status")).toHaveText("受理済み・台帳反映済み");
  state.staleList = true;
  const response = page.waitForResponse((item) => new URL(item.url()).pathname.endsWith("/personal-drafts"));
  await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
  await (await response).finished();
  await expect(page.getByText("候補を取得中…", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("draft-status")).toHaveText("受理済み・台帳反映済み");
  await expect(page.getByRole("button", { name: "この候補を受理", exact: true })).toHaveCount(0);
  expect(state.posts).toBe(0);
  expect(state.errors).toEqual([]);
});

test("authoritative other-tab discard resolves an unknown accept and a late retry409 cannot restore its pending lock", async ({ page }) => {
  const state = await installOtherTabAcceptance(page);
  const bodies: Array<Record<string, unknown>> = [];
  let releaseRetry!: () => void;
  const retryGate = new Promise<void>((resolve) => { releaseRetry = resolve; });
  let retryReturned = false;
  await page.route(`**/api/python-proxy/personal-drafts/${DRAFT_ID}/accept`, async (route) => {
    bodies.push(route.request().postDataJSON() as Record<string, unknown>);
    if (bodies.length === 1) { await route.abort("connectionreset"); return; }
    await retryGate;
    await route.fulfill({ status: 409, json: { detail: "discarded in another tab" } });
    retryReturned = true;
  });
  try {
    await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
    await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
    await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
    await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
    const retry = page.getByRole("button", { name: "同じ操作で結果を照合", exact: true });
    await expect(retry).toBeVisible();
    await retry.click();
    await expect.poll(() => bodies.length).toBe(2);
    state.otherTabTerminal = true;
    state.terminalStatus = "discarded";
    await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
    await expect(page.getByTestId("draft-status")).toHaveText("破棄済み");
    releaseRetry();
    await expect.poll(() => retryReturned).toBe(true);
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    await expect(retry).toHaveCount(0);
    await expect(page.getByTestId("personal-drafts").getByRole("alert")).toHaveCount(0);
    await expect(page.getByTestId("draft-status")).toHaveText("破棄済み");
    await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
    expect(bodies[0]).toEqual(bodies[1]);
    expect(state.errors).toEqual([]);
  } finally { releaseRetry(); }
});


test("an unknown operation outside loaded pages can be reconciled by its exact candidate GET", async ({ page }) => {
  const state = await installOtherTabAcceptance(page);
  let posts = 0;
  await page.route(`**/api/python-proxy/personal-drafts/${DRAFT_ID}/accept`, async (route) => {
    posts++;
    await route.abort("connectionreset");
  });
  await page.goto(`${BASE_PATH ? `/${BASE_PATH}` : ""}/personal-ledger`);
  await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
  await page.getByRole("checkbox", { name: "元の文面と、この候補の内容を確認しました" }).check();
  await page.getByRole("button", { name: "この候補を受理", exact: true }).click();
  const exactRead = page.getByRole("button", { name: "保留中の候補を照合", exact: true });
  await expect(exactRead).toBeVisible();
  state.otherTabTerminal = true;
  state.terminalStatus = "discarded";
  state.omitPendingFromList = true;
  await page.getByRole("button", { name: "候補を再取得", exact: true }).click();
  await expect(page.getByText("表示できる候補はありません", { exact: true })).toBeVisible();
  await expect(exactRead).toBeVisible();
  await exactRead.click();
  await expect(exactRead).toHaveCount(0);
  await expect(page.getByTestId("personal-drafts").getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("元の受理操作は成立していません");
  await expect(page.getByTestId("inventory-row")).toContainText("10 pcs");
  expect(state.preflightReads).toBe(2);
  expect(posts).toBe(1);
  expect(state.errors).toEqual([]);
});
