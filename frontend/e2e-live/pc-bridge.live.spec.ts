import { expect, test } from "@playwright/test";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { spawn, type ChildProcess } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { loadLiveAdmin, loginThroughUi } from "./support/live-auth";

const root = resolve(process.cwd(), "..");
const work = resolve(root, ".local/portable-bridge/live-ui");
const admin = loadLiveAdmin();
const base = "/api/python-proxy/pc-bridge";
test.use({ trace: "off", video: "off", screenshot: "off", viewport: { width: 1440, height: 1000 } });

test("portable exe routes Edge and Windows through the selected remote PC", async ({ page }) => {
  test.setTimeout(600_000);
  test.skip(!admin || !existsSync(resolve(root, "AoiTalk-PC-Bridge.exe")), "requires built exe, installed Edge extension and QA login");
  mkdirSync(work, { recursive: true });
  const marker = "BRIDGE-" + randomUUID().slice(0, 8);
  let saves = 0;
  const fixture = createServer((req, res) => {
    const url = new URL(req.url!, "http://localhost");
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    if (url.pathname === "/login" && req.method === "POST") {
      res.writeHead(303, { "Set-Cookie": `bridge_fixture=${marker}; HttpOnly; SameSite=Lax; Path=/`, Location: "/" }); res.end(); return;
    }
    if (url.pathname === "/logout") { res.setHeader("Set-Cookie", "bridge_fixture=; Max-Age=0; Path=/"); res.end("Signed out"); return; }
    if (!req.headers.cookie?.includes(`bridge_fixture=${marker}`)) {
      res.end('<title>Portable bridge test</title><form method="post" action="/login"><button>Sign in</button></form>'); return;
    }
    if (url.pathname === "/save" && req.method === "POST") {
      let body = ""; req.on("data", data => { body += data; }); req.on("end", () => {
        if (new URLSearchParams(body).get("note") === "Verified") { saves++; res.end(`<h1>Saved note</h1><p>Reference value: ${marker}</p>`); }
        else res.end("Incorrect note");
      }); return;
    }
    if (url.pathname === "/results") {
      const match = url.searchParams.get("q") === "Aoi" && url.searchParams.get("category") === "Books" && url.searchParams.get("stock") === "on";
      res.end('<h1>Search results</h1>' + (match ? '<a href="/details">Aoi handbook</a>' : 'No match')); return;
    }
    if (url.pathname === "/details") { res.end(`<h1>Aoi handbook</h1><p>Reference value: ${marker}</p><form method="post" action="/save"><label>Note <input name="note"></label><button>Save note</button></form>`); return; }
    res.end('<h1>Signed in catalog</h1><form action="/results"><label>Search query <input name="q"></label><label>Category <select name="category"><option>All</option><option>Books</option></select></label><label><input type="checkbox" name="stock">In stock</label><button>Search</button></form>');
  });
  await new Promise<void>(done => fixture.listen(0, "127.0.0.1", done));
  const address = fixture.address(); if (!address || typeof address === "string") throw new Error("fixture address missing");
  const url = `http://127.0.0.1:${address.port}`;
  let deviceId = "", tabId: number | null = null, previousTab: number | null = null;
  let client: ChildProcess | undefined;
  const sessions: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.name));
  await loginThroughUi(page, admin!, { expectedRole: "admin" });
  const original = (await (await page.request.get(base + "/devices")).json()).result;
  const browserSettings = (await (await page.request.get("/api/python-proxy/settings")).json()).settings.browser_agent;
  const settings = { enabled: browserSettings.enabled, jev_enabled: browserSettings.jev_enabled, max_steps: browserSettings.max_steps, timeout_seconds: browserSettings.timeout_seconds, jev_model: browserSettings.jev_model };
  async function rpc(channel: string, action: string, params: Record<string, unknown> = {}) {
    const response = await page.request.post(`${base}/devices/${deviceId}/command`, { data: { channel, action, params }, timeout: 60_000 });
    const body = await response.json();
    if (!response.ok()) throw new Error(`Bridge ${channel}/${action}: ${response.status()} ${body.detail ?? ""}`);
    return body.result;
  }
  async function chat(prompt: string, expected: string) {
    const engine = await (await page.request.get("/api/python-proxy/llm/engine?include_available=false")).json();
    const route = engine.effective_main ?? engine.execution_profile?.effective_main ?? {};
    const response = await page.request.post("/api/conversations", { data: { character_name: "project_manager", main_route: { provider: route.provider ?? engine.effective_provider ?? engine.provider, model: route.model ?? engine.effective_model ?? engine.model } } });
    expect(response.ok()).toBeTruthy(); const sessionId = (await response.json()).session.id; sessions.push(sessionId);
    await page.goto(`/chat?s=${sessionId}`);
    await page.getByRole("textbox", { name: "メッセージ入力" }).fill(prompt);
    await page.getByTitle("送信", { exact: true }).click();
    await expect(page.getByText(expected, { exact: false }).first()).toBeVisible({ timeout: 240_000 });
    return sessionId;
  }
  try {
    await page.goto("/settings#pc-bridge");
    await page.getByLabel("追加するPCの名前").fill("Portable exe live QA");
    const registeredResponse = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/pc-bridge/devices"));
    await page.getByRole("button", { name: "PCを登録", exact: true }).click();
    const registered = (await (await registeredResponse).json()).result; deviceId = registered.id;
    const configPath = resolve(work, "connection.json");
    writeFileSync(configPath, JSON.stringify({ server_url: process.env.AOITALK_BRIDGE_SERVER_URL || "http://127.0.0.1:3000", token: registered.token }));
    const log = openSync(resolve(work, "client.log"), "w");
    client = spawn(resolve(root, "AoiTalk-PC-Bridge.exe"), ["--headless", "--config", configPath], { cwd: work, stdio: ["ignore", log, log] }); closeSync(log);
    await expect.poll(async () => (await (await page.request.get(base + "/devices")).json()).result.devices.find((d: { id: string }) => d.id === deviceId)?.online, { timeout: 30_000 }).toBe(true);
    await page.getByRole("button", { name: "接続を更新", exact: true }).click();
    await page.getByLabel("操作するPC").click();
    await page.getByRole("option", { name: /Portable exe live QA/ }).click();
    await expect.poll(async () => (await (await page.request.get(base + "/devices")).json()).result.selected_device_id).toBe(deviceId);
    await page.reload(); await expect(page.getByLabel("操作するPC")).toContainText("Portable exe live QA");
    // The same executable supplies screen capture and control; no server-local desktop calls.
    await page.getByRole("button", { name: "画面を確認", exact: true }).click();
    await expect(page.getByAltText("選択したPCの画面")).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: resolve(work, "settings.png") });
    previousTab = (await rpc("browser", "status")).selected_tab_id;
    const target = await rpc("browser", "resolve_tab", { url: url + "/login" }); tabId = target.tab_id;
    const observed = await rpc("browser", "observe", { tab_id: tabId, action: "click" });
    const signIn = Object.entries(observed.candidates).find(([, value]) => (value as { label: string }).label === "Sign in")?.[0];
    expect(signIn).toBeTruthy(); await rpc("browser", "act", { tab_id: tabId, action: "click", target: signIn, value: "" });
    expect((await rpc("browser", "observe", { tab_id: tabId })).text).toContain("Signed in catalog");
    await page.request.patch("/api/python-proxy/settings", { data: { key: "browser_agent", value: { ...settings, enabled: true, jev_enabled: false, timeout_seconds: 300 } } });
    await chat("選択中PCのブラウザで今のログイン済みタブを操作してください。browser_agentを使用しURLは省略。Search queryにAoi、CategoryはBooks、In stockをチェックしSearch。Aoi handbookを開いてNoteにVerifiedを入力しSave noteを押す。画面に表示されたReference valueを答えてください。ファイルや別APIから調べないでください。", marker);
    expect(saves).toBe(1);
    await page.screenshot({ path: resolve(work, "browser-chat.png") });
    // Exercise Windows input in a new, task-owned Notepad document.
    const document = resolve(work, "portable-bridge-proof.txt"); writeFileSync(document, "");
    spawn("notepad.exe", [document], { detached: true, stdio: "ignore" }).unref();
    let windowId: number | null = null;
    await expect.poll(async () => {
      const windows = (await rpc("computer", "windows")).windows;
      windowId = windows.find((w: { title: string }) => w.title.includes("portable-bridge-proof"))?.window_id ?? null;
      return windowId;
    }, { timeout: 30_000 }).not.toBeNull();
    await rpc("computer", "focus", { window_id: windowId });
    await rpc("computer", "press", { keys: ["CTRL", "A"] });
    await rpc("computer", "type", { text: "Portable Unicode 日本語 " + marker });
    await rpc("computer", "press", { keys: ["CTRL", "S"] });
    await expect.poll(() => readFileSync(document, "utf-8")).toContain("Portable Unicode 日本語 " + marker);
    const desktop = await rpc("computer", "observe");
    expect(Object.keys(desktop.controls).length).toBeGreaterThan(0);
    writeFileSync(resolve(work, "desktop.jpg"), Buffer.from(desktop.screenshot.data, "base64"));
    await chat("computer_useで選択中PCのWindowsを操作してください。既に開いているportable-bridge-proof.txtのメモ帳に切り替えて、現在の本文を読んでください。本文はファイルAPIではなく画面から確認し、日本語と識別子をそのまま答えてください。", marker);
    await page.screenshot({ path: resolve(work, "computer-chat.png") });
    await rpc("computer", "focus", { window_id: windowId });
    await rpc("computer", "press", { keys: ["CTRL", "W"] });
    expect(errors).toEqual([]);
    writeFileSync(resolve(work, "summary.json"), JSON.stringify({ browser_saved: saves, desktop_input: true, desktop_read: true, pageErrors: errors, single_file_exe: true }));
  } finally {
    for (const id of sessions) await page.request.delete(`/api/conversations/${id}`).catch(() => undefined);
    if (tabId) {
      await rpc("browser", "navigate", { tab_id: tabId, url: url + "/logout" }).catch(() => undefined);
      await rpc("browser", "close_tab", { tab_id: tabId }).catch(() => undefined);
    }
    if (deviceId) {
      await rpc("browser", "select_tab", { tab_id: previousTab }).catch(() => undefined);
      await page.request.put(base + "/selection", { data: { device_id: original.selected_device_id } }).catch(() => undefined);
      await page.request.delete(`${base}/devices/${deviceId}`).catch(() => undefined);
    }
    await page.request.patch("/api/python-proxy/settings", { data: { key: "browser_agent", value: settings } }).catch(() => undefined);
    client?.kill(); fixture.close();
  }
});
