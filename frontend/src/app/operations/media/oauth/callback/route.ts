import { randomUUID } from "node:crypto";
import { withBasePath } from "@/lib/base-path";
import { OAUTH_RESULT_STORAGE_KEY, OAUTH_RETURN_STORAGE_KEY } from "@/lib/media-oauth-result";

export const dynamic = "force-dynamic";

/**
 * Asset-free relay: the provider's code is in this URL, so the page loads no
 * React shell, external assets or analytics. It saves the result, then returns
 * to the persona's workspace at once; the workspace shows the result as a toast.
 */
export async function GET() {
  const nonce = randomUUID();
  const json = (value: unknown) => JSON.stringify(value).replace(/</g, "\\u003c");
  const proxy = json(withBasePath("/api/python-proxy/operations/media/oauth/callback"));
  const home = json(withBasePath("/operations?tab=studio"));
  const returnKey = json(OAUTH_RETURN_STORAGE_KEY);
  const resultKey = json(OAUTH_RESULT_STORAGE_KEY);
  const html = `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>本人接続 - AoiTalk</title><style nonce="${nonce}">
:root{color-scheme:light dark;--bg:#f8fafc;--card:#fff;--fg:#0f172a;--muted:#64748b;--border:#e2e8f0;--primary:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1120;--card:#111827;--fg:#e5e7eb;--muted:#94a3b8;--border:#1f2937;--primary:#60a5fa}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif}
main{width:min(26rem,calc(100vw - 32px));padding:24px;border:1px solid var(--border);border-radius:12px;background:var(--card)}
h1{margin:0 0 8px;font-size:16px}p{margin:0 0 16px;color:var(--muted)}a{color:var(--primary)}
.spin{display:inline-block;width:12px;height:12px;margin-right:6px;border:2px solid var(--border);border-top-color:var(--primary);border-radius:50%;vertical-align:-1px;animation:s 0.8s linear infinite}@keyframes s{to{transform:rotate(360deg)}}
</style></head><body><main><h1>AoiTalk · 本人接続</h1><p id="status" role="status"><span class="spin" aria-hidden="true"></span>接続結果を保存しています…</p><a id="return">人格の画面へ戻る</a></main><script nonce="${nonce}">
(() => {
  const query = new URLSearchParams(location.search);
  const state = query.get('state'); const code = query.get('code'); const denied = query.get('error');
  history.replaceState(null, '', location.pathname);
  const status = document.getElementById('status'); const link = document.getElementById('return');
  let expectedAccount = null; let accountRef = ''; let platform = ''; link.href = ${home};
  try {
    const stored = JSON.parse(sessionStorage.getItem(${returnKey}) || 'null');
    sessionStorage.removeItem(${returnKey});
    if (stored && typeof stored.path === 'string' && typeof stored.accountId === 'string') {
      const target = new URL(stored.path, location.origin);
      if (target.origin === location.origin && target.pathname === new URL(${home}, location.origin).pathname && target.searchParams.get('tab') === 'studio') { link.href = target.href; expectedAccount = stored.accountId; }
      if (typeof stored.accountRef === 'string') accountRef = stored.accountRef.slice(0, 128);
      if (stored.platform === 'x' || stored.platform === 'youtube') platform = stored.platform;
    }
  } catch {}
  const finish = (tone, message) => {
    status.textContent = message;
    try { sessionStorage.setItem(${resultKey}, JSON.stringify({ tone, message, accountRef, platform })); } catch {}
    location.replace(link.href);
  };
  if (!state || state.length > 4096 || (code && code.length > 16384) || (!code && !denied)) { finish('error', '接続結果を照合できません。接続状態を確認してください。'); return; }
  const body = denied ? {state, error: denied.slice(0, 256)} : {state, code};
  fetch(${proxy}, {method:'POST', credentials:'include', cache:'no-store', referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(async response => {
      if (!response.ok) { finish('error', response.status === 401 ? 'ログイン状態を確認してください。接続操作は自動再送しません。' : response.status === 409 ? 'この接続結果は使用済み、または接続先の版が変わりました。接続状態を確認してください。' : '接続結果を保存できませんでした。接続状態を確認してください。'); return; }
      const result = await response.json();
      if (result && result.credential && typeof result.credential.account_ref === 'string') accountRef = result.credential.account_ref.slice(0, 128);
      if (!expectedAccount || result.platform_account_id !== expectedAccount) { finish('warning', '接続先の保存記録を照合してください。'); return; }
      if (result.status === 'connected') finish('success', '本人接続を保存しました。');
      else if (result.status === 'consent_denied') finish('warning', '本人接続の許可は取り消されました。');
      else finish('error', '本人接続の確認に失敗しました。接続設定を確認してください。');
    }).catch(() => { finish('error', '接続結果の通信が不明です。自動再送しません。接続状態を確認してください。'); });
})();</script></body></html>`;
  return new Response(html, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store, max-age=0", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`,
  } });
}
