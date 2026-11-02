import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { getTransferFixtureBundleFile } from "./fixtures/transfer-fixture-paths.cjs";

import {
  decodeTransferJson,
  encodeTransferBase64,
  encodeTransferJson,
} from "../src/lib/file-transfer-protocol";
import {
  readFixtureRequestBody,
  startFixtureHttpServer,
} from "./fixtures/http-server";

const COMPATIBILITY_KEY = "aoitalk-file-transfer-compatibility-mode";
const APP_ID = "2e66ac62-6a90-4b8e-bcca-b7c50bfe10c9";
const FILENAME = "transfer-fixture.txt";
const CONTENT = Buffer.from("AoiTalk isolated transfer fixture\n", "utf8");

type TransferObservation = { operation: string; url: string };
type NativeObservation = {
  method: string;
  path: string;
  phase: "preflight" | "download";
};

let filesAppsBundle: string;

test.beforeAll(async () => {
  filesAppsBundle = await readFile(
    getTransferFixtureBundleFile("transfer-download-entry.js"),
    "utf8",
  );
});

function downloadPage(scriptPath: string): string {
  return `<!doctype html>
    <html lang="ja"><meta charset="utf-8"><title>Transfer fixture</title>
    <main>
      <p>転送互換モード: <strong data-mode-label></strong></p>
      <button type="button" data-toggle-mode>Toggle fixture mode</button>
      <button type="button" data-files-download>Files fixture download</button>
      <div data-apps-root></div>
      <p role="status" data-download-status></p>
    </main>
    <script type="module" src="${scriptPath}"></script></html>`;
}

function transferStream(): string {
  const lines = [
    {
      type: "meta",
      value: encodeTransferJson({
        filename: FILENAME,
        mimeType: "text/plain",
        size: CONTENT.byteLength,
        attachment: true,
      }),
    },
    { type: "chunk", data: encodeTransferBase64(CONTENT) },
    { type: "end", size: CONTENT.byteLength },
  ];
  return `${lines.map((line) => JSON.stringify(line)).join("\n")}\n`;
}

async function installBrowserFixture(page: Page, enabled: boolean) {
  const transferRequests: TransferObservation[] = [];
  const nativeRequests: NativeObservation[] = [];
  await page.addInitScript(
    ({ key, value }) => localStorage.setItem(key, value),
    { key: COMPATIBILITY_KEY, value: enabled ? "true" : "false" },
  );
  const server = await startFixtureHttpServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (request.method === "GET" && url.pathname === "/") {
      response.writeHead(200, {
        "Cache-Control": "no-store",
        Connection: "close",
        "Content-Type": "text/html; charset=utf-8",
      });
      response.end(downloadPage("/fixture.js"));
      return;
    }
    if (request.method === "GET" && url.pathname === "/fixture.js") {
      response.writeHead(200, {
        "Cache-Control": "no-store",
        Connection: "close",
        "Content-Type": "text/javascript; charset=utf-8",
      });
      response.end(filesAppsBundle);
      return;
    }
    if (request.method === "POST" && url.pathname === "/api/file-transfer") {
      const envelope = JSON.parse(
        (await readFixtureRequestBody(request)).toString("utf8"),
      ) as { meta?: unknown };
      if (typeof envelope.meta === "string") {
        const meta = decodeTransferJson<{ operation: string; url: string }>(
          envelope.meta,
        );
        transferRequests.push({ operation: meta.operation, url: meta.url });
      }
      const body = Buffer.from(transferStream(), "utf8");
      response.writeHead(200, {
        "Cache-Control": "no-store",
        Connection: "close",
        "Content-Length": body.byteLength,
        "Content-Type": "application/x-ndjson; charset=utf-8",
      });
      response.end(body);
      return;
    }
    if (
      request.method === "GET" &&
      (url.pathname === "/api/python-proxy/explorer/download" ||
        url.pathname === `/api/python-proxy/apps/${APP_ID}/files/download`)
    ) {
      const preflight = request.headers["x-aoitalk-download-preflight"] === "1";
      nativeRequests.push({
        method: request.method,
        path: url.pathname,
        phase: preflight ? "preflight" : "download",
      });
      response.writeHead(200, {
        "Cache-Control": "no-store",
        Connection: "close",
        "Content-Disposition": `attachment; filename="${FILENAME}"`,
        "Content-Length": CONTENT.byteLength,
        "Content-Type": "text/plain; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      });
      response.end(CONTENT);
      return;
    }
    response.writeHead(404, {
      "Cache-Control": "no-store",
      Connection: "close",
      "Content-Type": "text/plain; charset=utf-8",
    });
    response.end("fixture route not found");
  });
  await page.goto(server.origin);
  await expect(
    page.getByText(enabled ? "ON" : "OFF", { exact: true }),
  ).toBeVisible();
  return { transferRequests, nativeRequests, close: server.close };
}

async function saveAndVerify(
  download: import("@playwright/test").Download,
  outputPath: string,
) {
  expect(download.suggestedFilename()).toBe(FILENAME);
  const browserStoredPath = await download.path();
  expect(browserStoredPath).not.toBeNull();
  expect(await readFile(browserStoredPath!)).toEqual(CONTENT);
  await download.saveAs(outputPath);
  expect(await readFile(outputPath)).toEqual(CONTENT);
}

test.describe("Files/Apps転送互換の実ブラウザfixture", () => {
  for (const enabled of [true, false]) {
    test(`Files・${enabled ? "ON" : "OFF"}でfilename/contentを保持して保存する`, async ({
      page,
    }, testInfo) => {
      const observed = await installBrowserFixture(page, enabled);
      try {
        const downloaded = page.waitForEvent("download");
        await page
          .getByRole("button", { name: "Files fixture download" })
          .click();
        await expect(page.getByRole("status")).toHaveText("Files: complete");
        await saveAndVerify(
          await downloaded,
          testInfo.outputPath(`files-${enabled ? "on" : "off"}.txt`),
        );

        if (enabled) {
          expect(observed.transferRequests).toEqual([
            expect.objectContaining({
              operation: "download",
              url: expect.stringContaining("/explorer/download"),
            }),
          ]);
          expect(observed.nativeRequests).toEqual([]);
        } else {
          expect(observed.transferRequests).toEqual([]);
          expect(observed.nativeRequests.map((item) => item.phase)).toEqual([
            "preflight",
            "download",
          ]);
          expect(
            observed.nativeRequests.every((item) => item.method === "GET"),
          ).toBe(true);
        }
      } finally {
        await observed.close();
      }
    });

    test(`Apps・${enabled ? "ON" : "OFF"}でfilename/contentを保持して保存する`, async ({
      page,
    }, testInfo) => {
      const observed = await installBrowserFixture(page, enabled);
      try {
        const downloaded = page.waitForEvent("download");
        const appDownloadLink = page.locator('[data-app-download-link="true"]');
        await expect(appDownloadLink).toBeVisible();
        if (!enabled) {
          expect(await appDownloadLink.getAttribute("download")).toBe(FILENAME);
        }
        await appDownloadLink.click();
        await saveAndVerify(
          await downloaded,
          testInfo.outputPath(`apps-${enabled ? "on" : "off"}.txt`),
        );

        if (enabled) {
          expect(observed.transferRequests).toEqual([
            expect.objectContaining({
              operation: "download",
              url: expect.stringContaining(`/apps/${APP_ID}/files/download`),
            }),
          ]);
          expect(observed.nativeRequests).toEqual([]);
        } else {
          expect(observed.transferRequests).toEqual([]);
          expect(observed.nativeRequests).toEqual([
            expect.objectContaining({ method: "GET", phase: "download" }),
          ]);
        }
      } finally {
        await observed.close();
      }
    });
  }
});
