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

const client = postgres(getConnectionString(), {
  types: {
    wallClockTimestamp: {
      to: 1114,
      from: [1114],
      serialize: serializeDbTimestampInput,
      parse: parseDbTimestampOutput,
    },
  },
});

export const db = drizzle(client as unknown as postgres.Sql, { schema });
