import {
  expect,
  test,
  type Locator,
  type Page,
  type WebSocketRoute,
} from "@playwright/test";

import { addAuthCookie, mockAuthenticatedApis } from "./support/auth";

const SESSION_ID = "session-e2e";
const REQUEST_ID = "external-review-layout";
const originalPayload = Array.from(
  { length: 32 },
  (_, index) => `原文 ${index + 1}: qa-person@example.invalid の確認用メッセージ`,
).join("\n");
const candidatePayload = originalPayload.replaceAll(
  "qa-person@example.invalid",
  "[EMAIL_1]",
);

async function expectInsideViewport(locator: Locator, page: Page) {
  await expect(locator).toBeInViewport({ ratio: 1 });
  const bounds = await locator.boundingBox();
  const viewport = page.viewportSize();
  expect(bounds).not.toBeNull();
  expect(viewport).not.toBeNull();
  if (!bounds || !viewport) throw new Error("Viewport bounds unavailable");
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height);
}

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 390, height: 640 },
]) {
  test.describe(`外部送信確認 ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("長文の確認内容をスクロールしても見出しとキャンセル操作を維持する", async ({
      page,
    }) => {
      await addAuthCookie(page);
      await mockAuthenticatedApis(page);
      await page.route("**/api/python-proxy/health", (route) =>
        route.fulfill({ json: { status: "ok" } }),
      );
      await page.route("**/api/python-proxy/characters", (route) =>
        route.fulfill({ json: { characters: ["aoi"], current: "aoi" } }),
      );
      await page.route("**/api/python-proxy/llm/session-settings*", (route) =>
        route.fulfill({
          json: {
            settings: {
              main_route: { provider: "mock", model: "mock-model" },
              special_routing: {},
            },
            effective_main: { provider: "mock", model: "mock-model" },
          },
        }),
      );
      await page.route(
        `**/api/python-proxy/conversations/${SESSION_ID}/generation/status`,
        (route) =>
          route.fulfill({
            json: { session_id: SESSION_ID, running: false, status: "idle" },
          }),
      );

      const sockets: WebSocketRoute[] = [];
      const decisions: unknown[] = [];
      await page.routeWebSocket(/\/ws(?:\?|$)/, (socket) => {
        // Do not connect to a backend: only this test supplies server packets.
        sockets.push(socket);
        socket.onMessage((message) => {
          const packet = JSON.parse(message.toString());
          if (packet.type === "external_model_prompt_response") {
            decisions.push(packet);
          }
        });
      });

      await page.goto(`/chat?s=${SESSION_ID}`);
      await expect(
        page.getByText("この会話にはまだメッセージがありません。", { exact: true }),
      ).toBeVisible();
      await expect.poll(() => sockets.length).toBeGreaterThan(0);
      const socket = sockets.at(-1);
      if (!socket) throw new Error("Chat WebSocket did not connect");
      socket.send(
        JSON.stringify({
          type: "external_model_prompt_request",
          data: {
            session_id: SESSION_ID,
            request_id: REQUEST_ID,
            contract_version: 2,
            review_nonce: "layout-review-nonce",
            binding_digest: "layout-binding-digest",
            action: "jev_completion_verification",
            transport: "http",
            destination: `https://review.example.invalid/${"long-destination-".repeat(12)}`,
            provider: "jev",
            tool: "completion_verification",
            model: "test-review-model",
            original_payload: originalPayload,
            candidate_payload: candidatePayload,
            masking_status: "MASKING_APPLIED",
            description: "外部へ送信する原文、秘匿候補、最終送信内容を確認してください。",
            redaction_findings: [{ category: "email", placeholder: "[EMAIL_1]" }],
            source_kind: "completion_verification",
            risk_level: "high",
            semantic_status: "completed",
            warning: "確認用の長文です。内容を確認し、送信せずキャンセルしてください。",
            notify: false,
          },
        }),
      );

      const dialog = page.getByRole("dialog", { name: "外部モデル送信の確認" });
      const header = dialog.locator('[data-slot="dialog-header"]');
      const footer = dialog.locator('[data-slot="dialog-footer"]');
      const cancel = dialog.getByRole("button", { name: "キャンセル", exact: true });
      const send = dialog.getByRole("button", { name: "送信", exact: true });
      const original = dialog.getByTestId("external-model-original-payload");
      const candidate = dialog.getByTestId("external-model-candidate-payload");
      const draft = dialog.getByRole("textbox", { name: "最終送信内容", exact: true });

      await expect(dialog).toBeVisible();
      for (const control of [dialog, header, footer, cancel, send]) {
        await expectInsideViewport(control, page);
      }
      await expect(draft).toHaveValue(candidatePayload);
      await expect(original).toHaveText(originalPayload);
      await expect(candidate).toHaveText(candidatePayload);
      for (const evidence of [original, candidate]) {
        expect(
          await evidence.evaluate(
            (element) => element.tagName === "PRE" && !(element as HTMLElement).isContentEditable,
          ),
        ).toBe(true);
      }

      await original.scrollIntoViewIfNeeded();
      await expectInsideViewport(header, page);
      await expectInsideViewport(footer, page);
      await draft.scrollIntoViewIfNeeded();
      await expectInsideViewport(draft, page);
      expect(
        await draft.evaluate((element) => {
          for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
            if (ancestor.scrollHeight > ancestor.clientHeight && ancestor.scrollTop > 0) return true;
          }
          return false;
        }),
      ).toBe(true);
      await draft.fill(`${candidatePayload}\n確認済み・送信しない`);
      await expect(original).toHaveText(originalPayload);
      await expect(candidate).toHaveText(candidatePayload);
      for (const control of [header, footer, cancel, send]) {
        await expectInsideViewport(control, page);
      }

      await cancel.click();
      await expect(dialog).toBeHidden();
      await expect.poll(() => decisions).toEqual([
        {
          type: "external_model_prompt_response",
          data: {
            contract_version: 2,
            request_id: REQUEST_ID,
            review_nonce: "layout-review-nonce",
            binding_digest: "layout-binding-digest",
            approved: false,
            final_payload: "",
            session_id: SESSION_ID,
          },
        },
      ]);
    });
  });
}
