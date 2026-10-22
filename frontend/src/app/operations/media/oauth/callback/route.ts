import { randomUUID } from "node:crypto";
import { withBasePath } from "@/lib/base-path";

export const dynamic = "force-dynamic";

/** No React shell, external assets, analytics, or server-side query projection. */
export async function GET() {
  const nonce = randomUUID();
  const proxy = JSON.stringify(withBasePath("/api/python-proxy/operations/media/oauth/callback")).replace(/</g, "\\u003c");
  const home = JSON.stringify(withBasePath("/operations?tab=studio")).replace(/</g, "\\u003c");
  const html = `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>本人接続の確認</title></head><body><main><h1>本人接続の確認</h1><p id="status" role="status">接続結果を確認中…</p><a id="return">人格のworkspaceへ戻る</a></main><script nonce="${nonce}">
(() => {
  const query = new URLSearchParams(location.search);
  const state = query.get('state'); const code = query.get('code'); const denied = query.get('error');
  history.replaceState(null, '', location.pathname);
  const status = document.getElementById('status'); const link = document.getElementById('return');
  let expectedAccount = null; link.href = ${home};
  try {
    const stored = JSON.parse(sessionStorage.getItem('aoitalk-media-oauth-return-v1') || 'null');
    sessionStorage.removeItem('aoitalk-media-oauth-return-v1');
    if (stored && typeof stored.path === 'string' && typeof stored.accountId === 'string') {
      const target = new URL(stored.path, location.origin);
      if (target.origin === location.origin && target.pathname === new URL(${home}, location.origin).pathname && target.searchParams.get('tab') === 'studio') { link.href = target.href; expectedAccount = stored.accountId; }
    }
  } catch {}
  if (!state || state.length > 4096 || (code && code.length > 16384) || (!code && !denied)) { status.textContent = '接続結果を照合できません。workspaceで接続状態を確認してください。'; return; }
  const body = denied ? {state, error: denied.slice(0, 256)} : {state, code};
  fetch(${proxy}, {method:'POST', credentials:'include', cache:'no-store', referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(async response => {
      if (!response.ok) { status.textContent = response.status === 401 ? 'ログイン状態を確認してください。接続操作は自動再送しません。' : response.status === 409 ? 'この接続結果は消費済み、または接続先の版が変わりました。workspaceで保存状態を確認してください。' : '接続結果を保存できませんでした。workspaceで保存状態を確認してください。'; return; }
      const result = await response.json();
      if (!expectedAccount || result.platform_account_id !== expectedAccount) { status.textContent = '接続先の保存記録をworkspaceで照合してください。'; return; }
      status.textContent = result.status === 'connected' ? '本人接続を保存しました。workspaceで公開前の準備状態を更新してください。' : result.status === 'consent_denied' ? '本人接続の許可は取り消されました。' : '本人接続の確認に失敗しました。workspaceで接続設定を確認してください。';
    }).catch(() => { status.textContent = '接続結果の通信が不明です。自動再送せずworkspaceで保存状態を確認してください。'; });
})();</script></body></html>`;
  return new Response(html, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store, max-age=0", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`,
  } });
}
