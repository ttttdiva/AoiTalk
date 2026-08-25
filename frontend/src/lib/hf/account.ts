/**
 * User-scoped Hugging Face credential resolution.
 *
 * Credentials used to be discovered from process.env (HF_ACCOUNTS,
 * HF_TOKEN_* and HUGGINGFACE_API_KEY).  That API is intentionally gone: an
 * authenticated principal must be supplied for every credential operation,
 * and the adapter below reads only that principal's encrypted DB row.
 */

import {
  listUserHfAccounts,
  listUserHfTokens,
  resolveUserHfToken,
  saveUserHfToken,
  type UserHfAccount,
} from "./user-store";

export type { UserHfAccount } from "./user-store";

/** List only integrations owned by the authenticated AoiTalk user. */
export async function listAccountsForUser(
  userId: string,
): Promise<UserHfAccount[]> {
  return listUserHfAccounts(userId);
}

/** Resolve an account after checking both principal and opaque account id. */
export async function resolveTokenForUser(
  userId: string,
  accountId?: string | null,
): Promise<{ accountId: string; username: string; token: string } | null> {
  return resolveUserHfToken(userId, accountId);
}

export async function listResolvedTokensForUser(
  userId: string,
): Promise<Array<{ accountId: string; username: string; token: string }>> {
  return listUserHfTokens(userId);
}

export { saveUserHfToken };
