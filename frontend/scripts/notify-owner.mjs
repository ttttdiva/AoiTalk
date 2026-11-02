// Queue one in-app notification for an existing active user.
//
// The running backend's notification worker delivers pending `in_app` rows to
// the open screen (websocket) and to the user's phone (web push), so an
// external supervisor can raise an operational alert without a new endpoint or
// a server restart. `dedupe_key` is the idempotency boundary
// (notification_deliveries.dedupe_key is unique): repeating the same key is a
// no-op, which is how callers limit an alert to once per hour.
//
//   node scripts/notify-owner.mjs --user-id <uuid> --dedupe-key <key> --title <text> --message <text>
//
// Prints one JSON object, {"inserted": true|false}. Nothing else is printed.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import postgres from "postgres";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NOTIFICATION_TYPE = "system_alert";

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

function parseArgs(argv) {
  const allowed = new Set(["--user-id", "--dedupe-key", "--title", "--message"]);
  const values = {};
  if (argv.length !== 8) fail("usage");
  for (let i = 0; i < argv.length; i += 2) {
    if (!allowed.has(argv[i]) || argv[i] in values) fail("usage");
    values[argv[i]] = argv[i + 1];
  }
  const userId = values["--user-id"];
  const dedupeKey = (values["--dedupe-key"] ?? "").trim();
  const title = (values["--title"] ?? "").trim();
  const message = (values["--message"] ?? "").trim();
  if (!UUID_RE.test(userId ?? "")) fail("invalid_user_id");
  if (!dedupeKey || dedupeKey.length > 255) fail("invalid_dedupe_key");
  if (!title || title.length > 255) fail("invalid_title");
  if (!message || message.length > 4000) fail("invalid_message");
  return { userId: userId.toLowerCase(), dedupeKey, title, message };
}

async function main() {
  const { userId, dedupeKey, title, message } = parseArgs(process.argv.slice(2));
  const dryRun = process.env.NOTIFY_OWNER_DRY_RUN === "1";
  const sql = postgres(databaseUrl(), { max: 1 });
  const rollback = Symbol("dry-run rollback");
  try {
    let inserted = false;
    try {
      await sql.begin(async (tx) => {
        const [user] = await tx`
          select is_active from users where id = ${userId}::uuid limit 1
        `;
        if (!user) fail("user_not_found");
        if (!user.is_active) fail("user_inactive");
        // notification_deliveries.project_id is required; anchor the alert to the
        // owner's oldest live project so it lands in their own inbox.
        const [project] = await tx`
          select id from projects
          where owner_id = ${userId}::uuid and deleted_at is null
          order by created_at asc limit 1
        `;
        if (!project) fail("owner_project_not_found");
        const rows = await tx`
          insert into notification_deliveries (
            id, project_id, user_id, channel, notification_type, dedupe_key,
            title, message, scheduled_for, status, payload, created_at, updated_at
          ) values (
            ${randomUUID()}::uuid, ${project.id}, ${userId}::uuid, 'in_app',
            ${NOTIFICATION_TYPE}, ${dedupeKey}, ${title}, ${message},
            (now() at time zone 'utc'), 'pending', '{}'::json,
            (now() at time zone 'utc'), (now() at time zone 'utc')
          )
          on conflict (dedupe_key) do nothing
          returning id
        `;
        inserted = rows.length === 1;
        // A dry run proves the statements are valid, then undoes them so no
        // alert reaches the user's screen or phone.
        if (dryRun) throw rollback;
      });
    } catch (error) {
      if (error !== rollback) throw error;
    }
    process.stdout.write(JSON.stringify({ inserted, dry_run: dryRun }));
  } catch {
    fail("notify_failed");
  } finally {
    await sql.end({ timeout: 2 });
  }
}

main().catch(() => fail("notify_failed"));
