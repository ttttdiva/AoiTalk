"use client";

import useSWR from "swr";

import type { DocsApiFetch } from "../docs-workspace-shared";
import type { DocsMentionUser } from "../outline/outline-editor";

// SWR キャッシュキー。メンション候補ユーザー一覧はワークスペース内で一意なので固定文字列を使う。
const MENTION_USERS_SWR_KEY = "docs/mention-users";

const EMPTY_MENTION_USERS: DocsMentionUser[] = [];

/**
 * メンション候補ユーザー一覧（/api/users/list）の取得を SWR で管理するフック。
 *
 * 従来は docs-workspace の useEffect + fetch + useState で、マウント時に一度だけ取得し、
 * 失敗時は空配列へフォールバックしていた。取得・重複排除・競合破棄を SWR に委譲しつつ、
 * その表示挙動（マウント時に一度取得・失敗時は空・自動再取得なし）を不変に保つ。
 */
export function useDocsMentionUsers(apiFetch: DocsApiFetch): DocsMentionUser[] {
  const { data } = useSWR<DocsMentionUser[]>(
    MENTION_USERS_SWR_KEY,
    () => apiFetch<DocsMentionUser[]>("/api/users/list"),
    {
      // 従来はマウント時に一度だけ取得していたため、mount 時の取得のみ許可し、
      // フォーカス・再接続・stale での自動再取得は全て無効化する。
      revalidateOnMount: true,
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      // 従来は catch で空配列にフォールバックし、リトライしなかった。
      shouldRetryOnError: false,
      dedupingInterval: 0,
    },
  );
  // 取得失敗時は data が undefined のまま → 従来の setMentionUsers([]) と同義。
  return data ?? EMPTY_MENTION_USERS;
}
