import { inArray, sql } from "drizzle-orm";
import type { AnyPgColumn } from "drizzle-orm/pg-core";

/** Resolved UUID scopes must stay below PostgreSQL's bind parameter limit. */
export function docsIdIn(column: AnyPgColumn, ids: readonly string[]) {
  return ids.length <= 5_000
    ? inArray(column, [...ids])
    : sql`${column} = ANY(string_to_array(${ids.join(",")}, ',')::uuid[])`;
}
