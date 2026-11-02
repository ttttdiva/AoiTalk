export const GENERAL_SESSION_FINGERPRINT_REVISION_KEY =
  "__general_session_fingerprint_revision:v1";

const incrementRevision = `
  INSERT INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
  VALUES ('${GENERAL_SESSION_FINGERPRINT_REVISION_KEY}', NULL, NULL, '1')
  ON CONFLICT(table_name) DO UPDATE SET
    cursor = CAST(CAST(COALESCE(sync_state.cursor, '0') AS INTEGER) + 1 AS TEXT);
`;

/**
 * A durable generation for local conversation_sessions writes. General sync
 * compares it with the value saved at the last completed pull instead of
 * hashing every session row, so a no-op sync does not read the whole table.
 * Every write bumps it, including writes that keep the same values; that only
 * costs one extra sessions pull and never hides a local-only change.
 */
export const GENERAL_SESSION_FINGERPRINT_REVISION_DDL = [
  `INSERT OR IGNORE INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
   VALUES ('${GENERAL_SESSION_FINGERPRINT_REVISION_KEY}', NULL, NULL, '0');`,
  `CREATE TRIGGER IF NOT EXISTS trg_general_session_fingerprint_revision_insert
   AFTER INSERT ON conversation_sessions
   BEGIN
     ${incrementRevision}
   END;`,
  `CREATE TRIGGER IF NOT EXISTS trg_general_session_fingerprint_revision_update
   AFTER UPDATE ON conversation_sessions
   BEGIN
     ${incrementRevision}
   END;`,
  `CREATE TRIGGER IF NOT EXISTS trg_general_session_fingerprint_revision_delete
   AFTER DELETE ON conversation_sessions
   BEGIN
     ${incrementRevision}
   END;`,
];
