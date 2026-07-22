/**
 * Huggingface マルチアカウント解決
 *
 * サーバーサイド専用。トークンはすべて環境変数 HF_TOKEN_{username}
 * から取得する。クライアントにトークンを露出させない。
 *
 * Windows の process.env は環境変数名を大文字化して Node 側に渡してくる
 * ため、HF_TOKEN_ プレフィックスを走査するだけでは HF のケース sensitive な
 * ユーザー名を復元できない（ExampleOrg が EXAMPLEORG として見える）。
 * 対策として、`HF_ACCOUNTS` に正しいケースのユーザー名を CSV で指定する。
 * 指定があればそちらを優先し、無ければフォールバックで env 走査する。
 *
 * 追加の汎用トークン HUGGINGFACE_API_KEY も読み込むが、公開リポジトリ
 * 閲覧用のフォールバックとして扱う（アカウント一覧には出さない）。
 */

export interface HfAccount {
  id: string; // "env:username"
  username: string; // HF 上のユーザー名
  label: string; // UI 表示名（username と同じ）
  source: "env";
}

const HF_TOKEN_PREFIX = "HF_TOKEN_";

/** process.env をケース非依存で検索する（Windows でキーが大文字化される件の回避） */
function findEnvValueCaseInsensitive(targetKey: string): string | undefined {
  const direct = process.env[targetKey];
  if (direct) return direct;
  const upper = targetKey.toUpperCase();
  const lower = targetKey.toLowerCase();
  for (const key of Object.keys(process.env)) {
    if (key === upper || key === lower || key.toUpperCase() === upper) {
      const v = process.env[key];
      if (v) return v;
    }
  }
  return undefined;
}

/** 登録済みアカウント一覧（トークンは含めない） */
export function listAccounts(): HfAccount[] {
  const accounts: HfAccount[] = [];
  const seen = new Set<string>();

  // 1) HF_ACCOUNTS 優先（正しいケースを保持）
  const csv = process.env.HF_ACCOUNTS;
  if (csv) {
    for (const raw of csv.split(",")) {
      const username = raw.trim();
      if (!username) continue;
      const token = findEnvValueCaseInsensitive(`${HF_TOKEN_PREFIX}${username}`);
      if (!token) continue;
      const dedupKey = username.toLowerCase();
      if (seen.has(dedupKey)) continue;
      seen.add(dedupKey);
      accounts.push({
        id: `env:${username}`,
        username,
        label: username,
        source: "env",
      });
    }
  }

  // 2) フォールバック：env 走査（HF_ACCOUNTS に漏れたアカウントを拾う）
  for (const key of Object.keys(process.env)) {
    if (!key.startsWith(HF_TOKEN_PREFIX) && !key.toUpperCase().startsWith(HF_TOKEN_PREFIX)) {
      continue;
    }
    const prefixLen = HF_TOKEN_PREFIX.length;
    const username = key.slice(prefixLen);
    if (!username) continue;
    const value = process.env[key];
    if (!value) continue;
    const dedupKey = username.toLowerCase();
    if (seen.has(dedupKey)) continue;
    seen.add(dedupKey);
    accounts.push({
      id: `env:${username}`,
      username,
      label: username,
      source: "env",
    });
  }

  accounts.sort((a, b) => a.username.localeCompare(b.username));
  return accounts;
}

/** accountId からトークンを取得。未指定時は先頭のアカウントを返す */
export function resolveToken(
  accountId?: string | null,
): { accountId: string; username: string; token: string } | null {
  const accounts = listAccounts();
  if (accounts.length === 0) return null;

  let picked: HfAccount | undefined;
  if (accountId) {
    // ケース非依存で比較（大文字化された id が来ても復元できるように）
    picked = accounts.find((a) => a.id === accountId)
      || accounts.find((a) => a.id.toLowerCase() === accountId.toLowerCase());
    if (!picked) return null;
  }
  if (!picked) picked = accounts[0];

  const token = findEnvValueCaseInsensitive(`${HF_TOKEN_PREFIX}${picked.username}`);
  if (!token) return null;

  return { accountId: picked.id, username: picked.username, token };
}

/** 登録済みアカウントとtokenの組。サーバー側のHF照会でのみ使用する。 */
export function listResolvedTokens(): Array<{
  accountId: string;
  username: string;
  token: string;
}> {
  return listAccounts()
    .map((account) => resolveToken(account.id))
    .filter((item): item is NonNullable<typeof item> => item !== null);
}

/** 公開リポジトリ閲覧用のフォールバックトークン（匿名閲覧も可） */
export function getFallbackToken(): string | undefined {
  return process.env.HUGGINGFACE_API_KEY || undefined;
}
