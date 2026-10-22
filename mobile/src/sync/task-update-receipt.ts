/** A stale CAS is a receipt only when every requested update is already present.
 * Unknown wire fields and partial/mismatched entities fail closed. Never rebase
 * a different server value or treat a missing task as an acknowledgement.
 */
export const TASK_CAS_STALE_REASON_CODE = 'task_cas_stale';

/** Only the explicit backend CAS classification may enter the Task receipt path. */
export function isTaskCasStaleReason(reasonCode: unknown): boolean {
  return reasonCode === TASK_CAS_STALE_REASON_CODE;
}

export function matchesTaskUpdateReceipt(
  entityId: string,
  payload: Record<string, unknown>,
  entity: Record<string, unknown> | undefined,
): boolean {
  if (!entity || entity.id !== entityId || entity.deleted_at != null
    || typeof entity.project_id !== 'string' || !entity.project_id
    || typeof entity.updated_at !== 'string' || !entity.updated_at) return false;
  const aliases: Record<string, string> = { task_metadata: 'metadata' };
  const fields = new Set(['title', 'description', 'status', 'priority', 'project_id',
    'start_at', 'end_at', 'all_day', 'auto_close_on_due', 'reminder_offsets',
    'notifications_enabled', 'estimated_hours', 'parent_task_id', 'sort_order',
    'tag_ids', 'assignee_ids', 'task_metadata']);
  const keys = Object.keys(payload);
  return keys.length > 0 && keys.every((key) => {
    if (!fields.has(key)) return false;
    const serverKey = Object.prototype.hasOwnProperty.call(entity, key) ? key : aliases[key] ?? key;
    return Object.prototype.hasOwnProperty.call(entity, serverKey)
      && JSON.stringify(entity[serverKey]) === JSON.stringify(payload[key]);
  });
}
