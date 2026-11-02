/** Set before leaving for the provider: where to come back and which slot was connected. */
export const OAUTH_RETURN_STORAGE_KEY = "aoitalk-media-oauth-return-v1";
/** Set by the callback relay just before it returns to the workspace. */
export const OAUTH_RESULT_STORAGE_KEY = "aoitalk-media-oauth-result-v1";

export type OAuthResultNotice = { tone: "success" | "warning" | "error"; message: string; accountRef: string; platform: string };

/** Reads and clears the relay's result once; anything malformed is dropped. */
export function takeOAuthResultNotice(storage: Pick<Storage, "getItem" | "removeItem">): OAuthResultNotice | null {
  let raw: string | null;
  try {
    raw = storage.getItem(OAUTH_RESULT_STORAGE_KEY);
    storage.removeItem(OAUTH_RESULT_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<OAuthResultNotice>;
    if (!value || !["success", "warning", "error"].includes(value.tone ?? "") || typeof value.message !== "string") return null;
    return {
      tone: value.tone as OAuthResultNotice["tone"],
      message: value.message.slice(0, 200),
      accountRef: typeof value.accountRef === "string" ? value.accountRef.slice(0, 128) : "",
      platform: value.platform === "x" || value.platform === "youtube" ? value.platform : "",
    };
  } catch {
    return null;
  }
}

export function oauthResultTitle(notice: OAuthResultNotice): string {
  const platform = notice.platform === "youtube" ? "YouTube" : notice.platform === "x" ? "X" : "";
  const target = [platform, notice.accountRef].filter(Boolean).join(" ");
  return target ? `${target}: ${notice.message}` : notice.message;
}
