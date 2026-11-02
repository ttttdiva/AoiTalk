import { describe, expect, it } from "vitest";
import { OAUTH_RESULT_STORAGE_KEY, oauthResultTitle, takeOAuthResultNotice } from "./media-oauth-result";

function memoryStorage(initial: Record<string, string>) {
  const values = new Map(Object.entries(initial));
  return { getItem: (key: string) => values.get(key) ?? null, removeItem: (key: string) => { values.delete(key); }, values };
}

describe("takeOAuthResultNotice", () => {
  it("reads the relay result once and names the connected handle", () => {
    const storage = memoryStorage({ [OAUTH_RESULT_STORAGE_KEY]: JSON.stringify({ tone: "success", message: "本人接続を保存しました。", accountRef: "@utsusen", platform: "x" }) });
    const notice = takeOAuthResultNotice(storage);
    expect(notice).toEqual({ tone: "success", message: "本人接続を保存しました。", accountRef: "@utsusen", platform: "x" });
    expect(oauthResultTitle(notice!)).toBe("X @utsusen: 本人接続を保存しました。");
    expect(takeOAuthResultNotice(storage)).toBeNull();
  });

  it("drops malformed or missing results", () => {
    expect(takeOAuthResultNotice(memoryStorage({}))).toBeNull();
    const storage = memoryStorage({ [OAUTH_RESULT_STORAGE_KEY]: JSON.stringify({ tone: "html", message: "<b>x</b>" }) });
    expect(takeOAuthResultNotice(storage)).toBeNull();
    expect(storage.values.has(OAUTH_RESULT_STORAGE_KEY)).toBe(false);
    expect(takeOAuthResultNotice(memoryStorage({ [OAUTH_RESULT_STORAGE_KEY]: "{" }))).toBeNull();
  });
});
