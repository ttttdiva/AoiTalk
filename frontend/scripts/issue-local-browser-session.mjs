// Issue a normal AoiTalk session cookie for an existing, active local user.
//
// Used by the Home MCP Gateway to log a broker-owned dedicated Edge profile in
// without typing a password: the same JWT claims as src/lib/auth.ts
// createSessionToken(), signed with the app's own NEXTAUTH_SECRET.  No server
// restart or new endpoint is needed.
//
// The token is written to stdout as one JSON object and only when stdout is a
// pipe, so it cannot end up on a terminal or in a shell transcript.  Callers
// must hand it straight to the browser cookie jar and never log it.
//
//   node scripts/issue-local-browser-session.mjs --user-id <uuid>
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { SignJWT } from "jose";
import postgres from "postgres";

const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7; // keep in sync with src/lib/auth.ts
const DEFAULT_COOKIE_NAME = "aoitalk_session"; // keep in sync with src/lib/session-cookie.ts
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const COOKIE_NAME_RE = /^[A-Za-z0-9_-]+$/u;

function fail(code) {
  process.stderr.write(`${code}\n`);
  process.exit(2);
}

function envValue(key) {
  if (process.env[key]) return process.env[key];
  try {
    const line = readFileSync(resolve(process.cwd(), ".env"), "utf8")
      .split(/\r?\n/)
      .find((item) => item.startsWith(`${key}=`));
    return line?.slice(key.length + 1).trim() || undefined;
  } catch {
    return undefined;
  }
}

function parseUserId(argv) {
  if (argv.length !== 2 || argv[0] !== "--user-id" || !UUID_RE.test(argv[1])) {
    fail("usage: --user-id <uuid>");
  }
  return argv[1].toLowerCase();
}

function databaseUrl() {
  const configured = envValue("DATABASE_URL");
  if (configured) return configured;
  const user = envValue("POSTGRES_USER") || "aoitalk";
  const password = envValue("POSTGRES_PASSWORD") || "";
  const host = envValue("POSTGRES_HOST") || "127.0.0.1";
  const port = envValue("POSTGRES_PORT") || "5432";
  const database = envValue("POSTGRES_DB") || "aoitalk_memory";
  return `postgresql://${encodeURIComponent(user)}:${encodeURIComponent(password)}@${host}:${port}/${encodeURIComponent(database)}`;
}

function cookiePath() {
  const raw = (envValue("NEXT_PUBLIC_BASE_PATH") || "").trim();
  if (!raw || raw === "/") return "/";
  const normalized = `/${raw.replace(/^\/+|\/+$/gu, "")}`;
  if (!/^\/[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/u.test(normalized)) {
    fail("invalid_base_path");
  }
  return normalized;
}

async function main() {
  if (process.stdout.isTTY) fail("stdout_must_be_a_pipe");
  const userId = parseUserId(process.argv.slice(2));
  const secretText = envValue("NEXTAUTH_SECRET")?.trim();
  if (!secretText) fail("session_secret_unavailable");
  const cookieName =
    (envValue("AOITALK_SESSION_COOKIE_NAME") || "").trim() || DEFAULT_COOKIE_NAME;
  if (!COOKIE_NAME_RE.test(cookieName)) fail("invalid_cookie_name");

  const sql = postgres(databaseUrl(), { max: 1 });
  let user;
  try {
    [user] = await sql`
      select is_active, is_password_reset_required, session_version
      from users where id = ${userId}::uuid limit 1
    `;
  } catch {
    fail("user_lookup_failed");
  } finally {
    await sql.end({ timeout: 2 });
  }
  if (!user) fail("user_not_found");
  if (!user.is_active) fail("user_inactive");
  if (user.is_password_reset_required) fail("user_password_reset_required");

  const now = Math.floor(Date.now() / 1000);
  const expiresAt = now + SESSION_MAX_AGE_SECONDS;
  const token = await new SignJWT({
    sub: userId,
    password_reset_required: false,
    session_version: Math.max(1, Number(user.session_version) || 1),
  })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setJti(randomUUID())
    .setExpirationTime(expiresAt)
    .sign(new TextEncoder().encode(secretText));

  process.stdout.write(
    JSON.stringify({
      name: cookieName,
      value: token,
      path: cookiePath(),
      expires: expiresAt,
    }),
  );
}

main().catch(() => fail("issue_failed"));
