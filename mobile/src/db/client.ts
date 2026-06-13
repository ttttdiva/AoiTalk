/**
 * SQLite + Drizzle client singleton.
 *
 * Opens the local cache DB (`aoitalk.db`) and wraps it with drizzle-orm.
 * Use `getDb()` from anywhere; the connection is lazy and reused.
 */

import { openDatabaseSync, type SQLiteDatabase } from 'expo-sqlite';
import { drizzle, type ExpoSQLiteDatabase } from 'drizzle-orm/expo-sqlite';
import * as schema from './schema';

const DB_NAME = 'aoitalk.db';

let _sqlite: SQLiteDatabase | null = null;
let _db: ExpoSQLiteDatabase<typeof schema> | null = null;

export function getSqlite(): SQLiteDatabase {
  if (!_sqlite) {
    _sqlite = openDatabaseSync(DB_NAME);
    // WAL for better concurrent reads
    _sqlite.execSync('PRAGMA journal_mode = WAL;');
    _sqlite.execSync('PRAGMA foreign_keys = ON;');
  }
  return _sqlite;
}

export function getDb(): ExpoSQLiteDatabase<typeof schema> {
  if (!_db) {
    _db = drizzle(getSqlite(), { schema });
  }
  return _db;
}

export { schema };
