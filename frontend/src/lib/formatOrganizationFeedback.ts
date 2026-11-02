// MUSE_ORG_FEEDBACK_20261101_0940_AC23
// formatOrganizationFeedback.ts

export type OrganizationFeedbackTone = 'neutral' | 'success' | 'warning' | 'error';
export type OrganizationFeedbackAction =
  | 'none'
  | 'retry_read'
  | 'check_status'
  | 'review_changes';

export interface OrganizationFeedback {
  tone: OrganizationFeedbackTone;
  message: string;
  action: OrganizationFeedbackAction;
}

const make = (
  tone: OrganizationFeedbackTone,
  message: string,
  action: OrganizationFeedbackAction,
): OrganizationFeedback => ({ tone, message, action });

const fallback = (): OrganizationFeedback =>
  make('error', '状態を確認できませんでした。', 'retry_read');

/**
 * Maps a verified controller status to concise, human UI feedback.
 *
 * Only the `status` field of a plain object is read, exactly once.
 * No other field is accessed or serialized, no value is coerced,
 * and success is never inferred from transport codes or message text.
 */
export function formatOrganizationFeedback(input: unknown): OrganizationFeedback {
  try {
    if (typeof input !== 'object' || input === null || Array.isArray(input)) {
      return fallback();
    }
    const proto: unknown = Object.getPrototypeOf(input);
    if (proto !== Object.prototype && proto !== null) {
      return fallback(); // arrays, class instances, and custom prototypes are not plain
    }
    if (!Object.prototype.hasOwnProperty.call(input, 'status')) {
      return fallback(); // missing or merely inherited status is not verified
    }
    const status: unknown = (input as Record<string, unknown>).status;
    if (typeof status !== 'string') {
      return fallback();
    }
    switch (status) {
      case 'loading':
        return make('neutral', '組織を読み込み中…', 'none');
      case 'saving':
        return make('neutral', '変更を保存中…', 'none');
      case 'saved':
        return make('success', '変更を保存しました。', 'none');
      case 'unchanged':
        return make('neutral', '変更はありません。', 'none');
      case 'conflict':
        return make('warning', '別の変更が保存されています。内容を確認してください。', 'review_changes');
      case 'uncertain':
        return make('warning', '保存結果を確認できません。再送せず状態を確認してください。', 'check_status');
      case 'unavailable':
        return make('error', '組織を読み込めませんでした。', 'retry_read');
      case 'denied':
        return make('error', 'この組織を編集する権限がありません。', 'none');
      case 'partial':
        return make('warning', '一部の変更を確認できません。保存状態を確認してください。', 'check_status');
      default:
        return fallback();
    }
  } catch {
    // Throwing getters / proxies / prototype tricks: classify, never propagate.
    return fallback();
  }
}
