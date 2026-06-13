import type { Config } from 'drizzle-kit';

/**
 * Drizzle-kit config. Generates SQL migrations from src/db/schema.ts
 * into src/db/migrations/ for use with expo-sqlite.
 */
export default {
  schema: './src/db/schema.ts',
  out: './src/db/migrations',
  dialect: 'sqlite',
  driver: 'expo',
} satisfies Config;
