/** Expo SQLite 54同梱SQLiteのSQLITE_MAX_VARIABLE_NUMBER。 */
export const EXPO_SQLITE_MAX_BIND_PARAMETERS = 32_766;

/** conversation_messages multi-row insertで1行あたりbindする列数。 */
export const CONVERSATION_MESSAGE_UPSERT_BIND_COLUMNS = 12;

/**
 * 端末ごとのSQL長・prepare負荷にも余裕を残す運用上のbind予算。
 * 500 rows × 12 binds = 6000で、SQLite上限の約18%に抑える。
 */
export const CONVERSATION_MESSAGE_UPSERT_SAFE_BIND_BUDGET = 6_000;

export const CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE = Math.min(
  500,
  Math.floor(
    Math.min(
      EXPO_SQLITE_MAX_BIND_PARAMETERS,
      CONVERSATION_MESSAGE_UPSERT_SAFE_BIND_BUDGET,
    ) / CONVERSATION_MESSAGE_UPSERT_BIND_COLUMNS,
  ),
);

export function conversationMessageUpsertStatementCount(rowCount: number): number {
  return rowCount > 0
    ? Math.ceil(rowCount / CONVERSATION_MESSAGE_UPSERT_CHUNK_SIZE)
    : 0;
}
