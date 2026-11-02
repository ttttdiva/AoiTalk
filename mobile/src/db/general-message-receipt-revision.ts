export const GENERAL_MESSAGE_RECEIPT_REVISION_KEY =
  "__general_message_receipt_revision:v1";

const incrementRevision = `
  INSERT INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
  VALUES ('${GENERAL_MESSAGE_RECEIPT_REVISION_KEY}', NULL, NULL, '1')
  ON CONFLICT(table_name) DO UPDATE SET
    cursor = CAST(CAST(COALESCE(sync_state.cursor, '0') AS INTEGER) + 1 AS TEXT);
`;

/**
 * A durable, table-specific generation for exact local chat-message receipts.
 * The sync_state marker is intentionally global: any message write invalidates
 * cached per-account receipts, which is conservative and survives process restarts.
 */
export const GENERAL_MESSAGE_RECEIPT_REVISION_DDL = [
  `INSERT OR IGNORE INTO sync_state(table_name, last_pulled_at, last_pushed_at, cursor)
   VALUES ('${GENERAL_MESSAGE_RECEIPT_REVISION_KEY}', NULL, NULL, '0');`,
  // Bootstrap runs on every database open. Replace the original triggers so
  // upgrades also gain no-op UPDATE filtering without a separate migration.
  "DROP TRIGGER IF EXISTS trg_general_message_receipt_revision_insert;",
  "DROP TRIGGER IF EXISTS trg_general_message_receipt_revision_update;",
  "DROP TRIGGER IF EXISTS trg_general_message_receipt_revision_delete;",
  `CREATE TRIGGER IF NOT EXISTS trg_general_message_receipt_revision_insert
   AFTER INSERT ON conversation_messages
   BEGIN
     ${incrementRevision}
   END;`,
  `CREATE TRIGGER IF NOT EXISTS trg_general_message_receipt_revision_update
   AFTER UPDATE ON conversation_messages
   WHEN OLD.id IS NOT NEW.id
     OR OLD.session_id IS NOT NEW.session_id
     OR OLD.role IS NOT NEW.role
     OR OLD.content IS NOT NEW.content
     OR OLD.message_metadata IS NOT NEW.message_metadata
     OR OLD.token_count IS NOT NEW.token_count
     OR OLD.parent_message_id IS NOT NEW.parent_message_id
     OR OLD.branch_index IS NOT NEW.branch_index
     OR OLD.is_active_branch IS NOT NEW.is_active_branch
     OR OLD.created_at IS NOT NEW.created_at
     OR OLD.updated_at IS NOT NEW.updated_at
     OR OLD.deleted_at IS NOT NEW.deleted_at
   BEGIN
     ${incrementRevision}
   END;`,
  `CREATE TRIGGER IF NOT EXISTS trg_general_message_receipt_revision_delete
   AFTER DELETE ON conversation_messages
   BEGIN
     ${incrementRevision}
   END;`,
];
