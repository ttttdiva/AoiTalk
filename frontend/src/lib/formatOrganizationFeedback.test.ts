// MUSE_ORG_FEEDBACK_20261101_0940_AC23
// formatOrganizationFeedback.test.ts
import { describe, it, expect } from 'vitest';
import { formatOrganizationFeedback } from './formatOrganizationFeedback';

const FALLBACK = {
  tone: 'error',
  message: '状態を確認できませんでした。',
  action: 'retry_read',
} as const;

const SECRET_TOKEN = 'PRIVATE_TOKEN_XYZ';
const SECRET_MESSAGE = 'PRIVATE_MESSAGE_XYZ';

const STATUS_CASES = [
  { status: 'loading', tone: 'neutral', message: '組織を読み込み中…', action: 'none' },
  { status: 'saving', tone: 'neutral', message: '変更を保存中…', action: 'none' },
  { status: 'saved', tone: 'success', message: '変更を保存しました。', action: 'none' },
  { status: 'unchanged', tone: 'neutral', message: '変更はありません。', action: 'none' },
  { status: 'conflict', tone: 'warning', message: '別の変更が保存されています。内容を確認してください。', action: 'review_changes' },
  { status: 'uncertain', tone: 'warning', message: '保存結果を確認できません。再送せず状態を確認してください。', action: 'check_status' },
  { status: 'unavailable', tone: 'error', message: '組織を読み込めませんでした。', action: 'retry_read' },
  { status: 'denied', tone: 'error', message: 'この組織を編集する権限がありません。', action: 'none' },
  { status: 'partial', tone: 'warning', message: '一部の変更を確認できません。保存状態を確認してください。', action: 'check_status' },
] as const;

describe('formatOrganizationFeedback', () => {
  it.each(STATUS_CASES)('status "$status" maps to its exact concise feedback', (c) => {
    expect(formatOrganizationFeedback({ status: c.status })).toEqual({
      tone: c.tone, message: c.message, action: c.action,
    });
  });

  it.each([
    'ok', 'success', 'complete', 'done', 'SAVED', ' saved', 'saved ', '200', '',
    '__proto__', 'toString', 'constructor',
  ])('unknown or near-miss status %j falls back (strict strings, no synonyms)', (status) => {
    expect(formatOrganizationFeedback({ status })).toEqual(FALLBACK);
  });

  it.each([
    ['non-string status (number)', { status: 200 }],
    ['non-string status (boolean)', { status: true }],
    ['non-string status (null)', { status: null }],
    ['missing status', {}],
    ['null input', null],
    ['undefined input', undefined],
    ['string input', 'saved'],
    ['number input', 200],
    ['boolean input', true],
    ['array input', [{ status: 'saved' }]],
    ['empty array input', []],
  ])('%s falls back', (_name, input) => {
    expect(formatOrganizationFeedback(input)).toEqual(FALLBACK);
  });

  it('class instances and inherited-status objects are not plain objects', () => {
    class Controllerish { status = 'saved'; }
    expect(formatOrganizationFeedback(new Controllerish())).toEqual(FALLBACK);
    expect(formatOrganizationFeedback(Object.create({ status: 'saved' }))).toEqual(FALLBACK);
    const arr = [] as unknown[] & { status?: string };
    arr.status = 'saved';
    expect(formatOrganizationFeedback(arr)).toEqual(FALLBACK);
  });

  it('a null-prototype plain object is accepted', () => {
    const input = Object.assign(Object.create(null), { status: 'saved' });
    expect(formatOrganizationFeedback(input)).toEqual({
      tone: 'success', message: '変更を保存しました。', action: 'none',
    });
  });

  it('a throwing status getter falls back without throwing or leaking', () => {
    const input: Record<string, unknown> = {};
    Object.defineProperty(input, 'status', {
      enumerable: true,
      get() { throw new Error(SECRET_TOKEN); },
    });
    let out!: ReturnType<typeof formatOrganizationFeedback>;
    expect(() => { out = formatOrganizationFeedback(input); }).not.toThrow();
    expect(out).toEqual(FALLBACK);
    expect(JSON.stringify(out)).not.toContain(SECRET_TOKEN);
  });

  it('a mutating status getter is read once and its first value wins', () => {
    let calls = 0;
    let backing: string = 'saved';
    const input: Record<string, unknown> = {};
    Object.defineProperty(input, 'status', {
      enumerable: true,
      get() {
        calls += 1;
        const value = backing;
        backing = 'conflict';
        return value;
      },
    });
    expect(formatOrganizationFeedback(input).tone).toBe('success');
    expect(calls).toBe(1);
  });

  it('malicious toString/valueOf are never executed (no coercion)', () => {
    let calls = 0;
    const evil = {
      toString() { calls += 1; return 'saved'; },
      valueOf() { calls += 1; return 'saved'; },
    };
    // As the status value itself:
    expect(formatOrganizationFeedback({ status: evil })).toEqual(FALLBACK);
    // As an ignored extra field / an own toString on an otherwise valid input:
    expect(formatOrganizationFeedback({ status: 'saved', toString: evil.toString }).tone).toBe('success');
    expect(calls).toBe(0);
  });

  it('private extra fields are ignored and never appear in the output', () => {
    const out = formatOrganizationFeedback({
      status: 'partial',
      message: SECRET_MESSAGE,
      body: SECRET_MESSAGE,
      details: { token: SECRET_TOKEN, nested: { hash: SECRET_TOKEN } },
      code: SECRET_TOKEN,
      owner_id: SECRET_TOKEN,
      id: SECRET_TOKEN,
      hash: SECRET_TOKEN,
    });
    expect(Object.keys(out).sort()).toEqual(['action', 'message', 'tone']);
    const json = JSON.stringify(out);
    expect(json).not.toContain(SECRET_TOKEN);
    expect(json).not.toContain(SECRET_MESSAGE);
  });

  it('partial is a warning to check status, never a success', () => {
    const out = formatOrganizationFeedback({ status: 'partial' });
    expect(out.tone).toBe('warning');
    expect(out.tone).not.toBe('success');
    expect(out.action).toBe('check_status');
  });

  it('saved is never inferred from HTTP 200 or an arbitrary message', () => {
    expect(formatOrganizationFeedback({ httpStatus: 200, message: '変更を保存しました。' })).toEqual(FALLBACK);
    expect(formatOrganizationFeedback({ code: 200, statusText: 'OK' })).toEqual(FALLBACK);
    // A real uncertain status stays uncertain even alongside a 200 and a "saved" message:
    expect(formatOrganizationFeedback({ status: 'uncertain', httpStatus: 200, message: 'saved' })).toEqual({
      tone: 'warning',
      message: '保存結果を確認できません。再送せず状態を確認してください。',
      action: 'check_status',
    });
  });

  it('returns a fresh normal object each call and never mutates frozen input', () => {
    const input = Object.freeze({
      status: 'saved',
      untouched: Object.freeze({ n: 1 }),
    });
    const snapshot = JSON.parse(JSON.stringify(input)) as unknown;
    const a = formatOrganizationFeedback(input);
    const b = formatOrganizationFeedback(input);
    expect(a).toEqual(b);
    expect(a).not.toBe(b);
    expect(Object.getPrototypeOf(a)).toBe(Object.prototype);
    expect(input).toEqual(snapshot);
  });
});
