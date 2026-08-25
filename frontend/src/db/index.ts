import { drizzle } from "drizzle-orm/postgres-js";
import {
  parseDbTimestampOutput,
  serializeDbTimestampInput,
} from "@/lib/server/db-time";
import postgres from "postgres";
import * as schema from "./schema";

function getConnectionString(): string {
  if (process.env.DATABASE_URL) return process.env.DATABASE_URL;

  // ルート.envのPOSTGRES_*変数から組み立て
  const user = process.env.POSTGRES_USER || "aoitalk";
  const password = process.env.POSTGRES_PASSWORD || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  const dbName = process.env.POSTGRES_DB || "aoitalk_memory";
  return `postgres://${user}:${password}@${host}:${port}/${dbName}`;
}

function createClient() {
  return postgres(getConnectionString(), {
    // 1プロセスあたりの上限を明示する（postgres の既定も10だが、
    // PostgreSQL 側の max_connections を食い潰さないよう意図を残す）。
    max: 10,
    types: {
      wallClockTimestamp: {
        to: 1114,
        from: [1114],
        serialize: serializeDbTimestampInput,
        parse: parseDbTimestampOutput,
      },
    },
  });
}

// dev の HMR ではサーバー側モジュールが再評価されるたびに新しい接続プールが作られ、
// 古いプールが閉じられないまま残るため、接続数が際限なく増えて PostgreSQL の
// max_connections を使い切ってしまう（"残りの接続枠はSUPERUSER..." で全 API が失敗する）。
// globalThis に載せてプロセス内で1つだけ使い回す。
const globalForDb = globalThis as typeof globalThis & {
  __aoitalkPostgresClient?: ReturnType<typeof createClient>;
};

const client = globalForDb.__aoitalkPostgresClient ?? createClient();
if (process.env.NODE_ENV !== "production") {
  globalForDb.__aoitalkPostgresClient = client;
}

export const db = drizzle(client as unknown as postgres.Sql, { schema });
