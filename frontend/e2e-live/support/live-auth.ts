import { existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { Page } from "@playwright/test";

export type LiveUserCredentials = {
  username: string;
  password: string;
  fallbackPassword?: string;
};

export type LiveAuthStatus = {
  authenticated?: boolean;
  user?: {
    id?: string;
    username?: string;
    role?: string | null;
    password_reset_required?: boolean;
  } | null;
};

function nonEmpty(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function pickUserA(payload: unknown): Record<string, unknown> | null {
  const root = asRecord(payload);
  if (!root) return null;
  const nestedUsers = asRecord(root.users);
  return (
    asRecord(root.userA) ??
    asRecord(root.user_a) ??
    asRecord(root.A) ??
    (nestedUsers
      ? (asRecord(nestedUsers.userA) ?? asRecord(nestedUsers.A))
      : null)
  );
}

function pickNamedUser(
  payload: unknown,
  name: "userA" | "admin",
): Record<string, unknown> | null {
  const root = asRecord(payload);
  if (!root) return null;
  const nestedUsers = asRecord(root.users);
  return (
    asRecord(root[name]) ?? (nestedUsers ? asRecord(nestedUsers[name]) : null)
  );
}

function flowCredsPath() {
  return (
    process.env.AOITALK_FLOW_CREDS_PATH?.trim() ||
    join(
      process.env.TEMP || process.env.TMP || tmpdir(),
      "aoitalk-flow-creds.json",
    )
  );
}

export function loadLiveUserA(): LiveUserCredentials | null {
  const envUsername =
    nonEmpty(process.env.AOITALK_LIVE_USERNAME) ??
    nonEmpty(process.env.E2E_LIVE_USERNAME);
  const envPassword =
    nonEmpty(process.env.AOITALK_LIVE_PASSWORD) ??
    nonEmpty(process.env.E2E_LIVE_PASSWORD);
  if (envUsername || envPassword) {
    if (!envUsername || !envPassword) {
      throw new Error(
        "live user credentials are incomplete: set both AOITALK_LIVE_USERNAME and AOITALK_LIVE_PASSWORD",
      );
    }
    return { username: envUsername, password: envPassword };
  }

  const credsPath = flowCredsPath();
  if (!existsSync(credsPath)) return null;
  try {
    const userA = pickUserA(JSON.parse(readFileSync(credsPath, "utf8")));
    if (!userA) {
      throw new Error(`live user A credentials are missing: ${credsPath}`);
    }
    const username = nonEmpty(userA.username);
    const password = nonEmpty(userA.password);
    const fallbackPassword = nonEmpty(userA.newPassword);
    if (!username) {
      throw new Error(`live user A credentials are incomplete: ${credsPath}`);
    }
    if (!password) {
      throw new Error(`live user A credentials are incomplete: ${credsPath}`);
    }
    if (password && fallbackPassword && fallbackPassword !== password) {
      return { username, password, fallbackPassword };
    }
    return { username, password };
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`live flow credentials are not valid JSON: ${credsPath}`);
    }
    throw error;
  }
}

export function loadLiveAdmin(): LiveUserCredentials | null {
  const envUsername =
    nonEmpty(process.env.AOITALK_LIVE_ADMIN_USERNAME) ??
    nonEmpty(process.env.E2E_LIVE_ADMIN_USERNAME);
  const envPassword =
    nonEmpty(process.env.AOITALK_LIVE_ADMIN_PASSWORD) ??
    nonEmpty(process.env.E2E_LIVE_ADMIN_PASSWORD);
  if (envUsername || envPassword) {
    if (!envUsername || !envPassword) {
      throw new Error(
        "live admin credentials are incomplete: set both AOITALK_LIVE_ADMIN_USERNAME and AOITALK_LIVE_ADMIN_PASSWORD",
      );
    }
    return { username: envUsername, password: envPassword };
  }

  const credsPath = flowCredsPath();
  if (!existsSync(credsPath)) return null;
  try {
    const admin = pickNamedUser(
      JSON.parse(readFileSync(credsPath, "utf8")),
      "admin",
    );
    if (!admin) {
      throw new Error(`live admin credentials are missing: ${credsPath}`);
    }
    const username = nonEmpty(admin.username);
    const password = nonEmpty(admin.password);
    if (!username || !password) {
      throw new Error(`live admin credentials are incomplete: ${credsPath}`);
    }
    return { username, password };
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`live flow credentials are not valid JSON: ${credsPath}`);
    }
    throw error;
  }
}

async function postLogin(page: Page, username: string, password: string) {
  return page.request.post("/api/auth/login", {
    data: { username, password },
    failOnStatusCode: false,
  });
}

async function readAuthStatus(page: Page): Promise<LiveAuthStatus> {
  const response = await page.request.get("/api/auth/status", {
    failOnStatusCode: false,
  });
  if (!response.ok()) {
    throw new Error(`auth status failed with HTTP ${response.status()}`);
  }
  try {
    return (await response.json()) as LiveAuthStatus;
  } catch (error) {
    throw new Error(
      `auth status returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

async function requireSessionCookie(page: Page, context: string) {
  const cookies = await page.context().cookies();
  if (!cookies.some((cookie) => cookie.name === "aoitalk_session")) {
    throw new Error(`${context} did not establish an aoitalk_session cookie`);
  }
}

function assertRegularUserStatus(status: LiveAuthStatus, context: string) {
  if (status.authenticated !== true) {
    throw new Error(`${context} is not authenticated`);
  }
  if (status.user?.role !== "user") {
    throw new Error(
      `${context} resolved unexpected role: ${String(status.user?.role)}`,
    );
  }
  if (status.user?.password_reset_required === true) {
    throw new Error(`${context} still requires a password reset`);
  }
}

export async function loginAsRegularLiveUser(
  page: Page,
  creds: LiveUserCredentials,
): Promise<LiveAuthStatus> {
  let response = await postLogin(page, creds.username, creds.password);
  if (
    response.status() === 401 &&
    creds.fallbackPassword &&
    creds.fallbackPassword !== creds.password
  ) {
    response = await postLogin(page, creds.username, creds.fallbackPassword);
  }
  if (!response.ok()) {
    throw new Error(
      `live user login failed for ${creds.username}: HTTP ${response.status()}`,
    );
  }

  const status = await readAuthStatus(page);
  assertRegularUserStatus(status, `live user ${creds.username}`);
  await requireSessionCookie(page, `live user ${creds.username} login`);
  return status;
}

export async function loginThroughUi(
  page: Page,
  creds: LiveUserCredentials,
  options: {
    expectedRole?: "admin" | "user";
    allowPasswordReset?: boolean;
  } = {},
): Promise<LiveAuthStatus> {
  await page.goto("/login", { waitUntil: "domcontentloaded" });
  await page.locator("#username").fill(creds.username);
  await page.locator("#password").fill(creds.password);
  await page.getByRole("button", { name: "ログイン", exact: true }).click();
  try {
    await page.waitForURL(
      (url) => !/^\/login(?:\?|$)/.test(url.pathname + url.search),
      { timeout: 20_000 },
    );
  } catch {
    throw new Error(`UI login did not leave /login for ${creds.username}`);
  }
  await page.waitForLoadState("domcontentloaded").catch(() => undefined);
  const status = await readAuthStatus(page);
  if (status.authenticated !== true) {
    throw new Error(`UI login for ${creds.username} is not authenticated`);
  }
  if (options.expectedRole && status.user?.role !== options.expectedRole) {
    throw new Error(
      `UI login for ${creds.username} resolved role ${String(status.user?.role)}, expected ${options.expectedRole}`,
    );
  }
  if (
    !options.allowPasswordReset &&
    status.user?.password_reset_required === true
  ) {
    throw new Error(
      `UI login for ${creds.username} still requires a password reset`,
    );
  }
  await requireSessionCookie(page, `UI login for ${creds.username}`);
  return status;
}

export async function logoutThroughUi(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: "ユーザーメニューを開く", exact: true })
    .click();
  await page.getByRole("menuitem", { name: /ログアウト/ }).click();
  try {
    await page.waitForURL(
      (url) => /^\/login(?:\?|$)/.test(url.pathname + url.search),
      { timeout: 20_000 },
    );
  } catch {
    throw new Error(`UI logout did not return to /login (url=${page.url()})`);
  }
  const cookies = await page.context().cookies();
  if (cookies.some((cookie) => cookie.name === "aoitalk_session")) {
    throw new Error("UI logout left an aoitalk_session cookie behind");
  }
}

export async function assertRegularSession(
  page: Page,
  username: string,
): Promise<LiveAuthStatus> {
  const status = await readAuthStatus(page);
  assertRegularUserStatus(status, `session for ${username}`);
  await requireSessionCookie(page, `session for ${username}`);
  return status;
}
