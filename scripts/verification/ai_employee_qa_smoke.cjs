// UI repair smoke; phone mode also creates a draft test route.
// Password login is real; no reports/actions/revisions or provider commands.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const root = process.argv[2];
const requireFrontend = createRequire(path.join(root, 'frontend', 'package.json'));
const { chromium, expect: baseExpect } = requireFrontend('@playwright/test');
const expect = baseExpect.configure({ timeout: 30000 });
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
const journey = JSON.parse(fs.readFileSync(path.join(root, 'browser-journey.json'), 'utf8'));
const phoneMode = process.env.AI_EMPLOYEE_QA_SMOKE_MODE === 'phone';
const historyMode = process.env.AI_EMPLOYEE_QA_SMOKE_MODE === 'history';
const evidencePath = path.join(root, phoneMode ? 'phone-ui-evidence.json' : historyMode ? 'history-ui-evidence.json' : 'ui-smoke-evidence.json');
const providerPath = path.join(root, 'provider-evidence.json');
const before = JSON.parse(fs.readFileSync(providerPath, 'utf8'));
const evidence = { no_order_smoke: true, agent_id: phoneMode ? manifest.fixtures.phone_agent_id : journey.agent_id, console_errors: [], page_errors: [], server_errors: [], off_scope_requests: [], checks: [] };
let stage = 'launch';
let browser;
(async () => {
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.setDefaultNavigationTimeout(60000);
  const allowed = new Set([manifest.frontend_url, manifest.backend_url]);
  await context.route(/^https?:\/\//u, async route => {
    const url = new URL(route.request().url());
    if (allowed.has(url.origin)) await route.continue();
    else { evidence.off_scope_requests.push({ kind: 'http', origin: url.origin, path: url.pathname }); await route.abort(); }
  });
  await page.routeWebSocket('**/*', socket => {
    const url = new URL(socket.url()); url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
    if (allowed.has(url.origin)) socket.connectToServer();
    else { evidence.off_scope_requests.push({ kind: 'websocket', origin: url.origin, path: url.pathname }); socket.close(); }
  });
  page.on('console', message => { if (message.type() === 'error') {
    const text = message.text();
    evidence.console_errors.push(text.includes('RSC payload') ? 'rsc_fetch_error' : text.includes('WebSocket') ? 'websocket_error' : text.includes('same key') ? 'duplicate_key' : /hydrat/i.test(text) ? 'hydration_error' : 'console.error');
  } });
  page.on('pageerror', error => evidence.page_errors.push(error.name));
  page.on('response', response => { if (response.status() >= 500) evidence.server_errors.push({ status: response.status(), path: new URL(response.url()).pathname }); });
  stage = 'login';
  await page.goto(manifest.frontend_url + '/login');
  await page.getByLabel('ユーザー名', { exact: true }).fill(process.env.AI_EMPLOYEE_QA_EMAIL);
  await page.getByLabel('パスワード', { exact: true }).fill(process.env.AI_EMPLOYEE_QA_PASSWORD);
  await page.getByRole('button', { name: 'ログイン', exact: true }).click();
  await expect(page).not.toHaveURL(/\/login(?:\?|$)/);
  if (phoneMode) {
    stage = 'phone-catalog';
    await page.goto(`${manifest.frontend_url}/operations?tab=agents&agent=${manifest.fixtures.phone_agent_id}&panel=phone`);
    const catalogResponse = await page.request.get(`${manifest.frontend_url}/api/python-proxy/telephony/catalog`);
    expect(catalogResponse.status()).toBe(200);
    const catalog = await catalogResponse.json();
    expect(catalog.provider_status).toBe('unverified');
    expect(catalog.route_keys.find(item => item.key === 'main').connection_id).toBe(manifest.fixtures.phone_connection_id);
    await expect(page.getByText('実際の公衆電話網（PSTN）との接続は未検証です。設定保存だけでは着信受付は開始しません。')).toBeVisible();
    const name = '電話受付 UI TEST fixture';
    await page.getByLabel('受付の表示名', { exact: true }).fill(name);
    await page.getByRole('combobox', { name: '登録済み回線', exact: true }).click();
    await page.getByRole('listbox').getByRole('option', { name: /Main phone TEST/ }).click();
    await page.getByRole('combobox', { name: '電話で使用する役割リビジョン', exact: true }).click();
    await page.getByRole('listbox').getByRole('option', { name: /realtime/ }).click();
    await page.screenshot({ path: path.join(root, 'screenshots', '08-phone-config-test-fixture.png'), fullPage: true });
    stage = 'phone-route-save';
    const savedResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/python-proxy/telephony/routes');
    await page.getByRole('button', { name: '下書きとして保存', exact: true }).click();
    const saved = await savedResponse;
    expect(saved.status()).toBe(201);
    const route = (await saved.json()).route;
    expect(route.state).toBe('draft');
    expect(route.agent_revision_id).toBe(manifest.fixtures.phone_revision_id);
    evidence.phone_route_id = route.id;
    stage = 'phone-readiness';
    const readinessResponse = await page.request.get(`${manifest.frontend_url}/api/python-proxy/telephony/routes/${route.id}/readiness`);
    expect(readinessResponse.status()).toBe(200);
    const readiness = await readinessResponse.json();
    expect(readiness.ready).toBe(false);
    expect(readiness.reason_codes).toContain('telephony_route_inactive');
    expect(readiness.provider_status).toBe('unverified');
    expect(readiness.external_setup).toBe('unverified');
    await expect(page.getByRole('article', { name })).toBeVisible();
    await expect(page.getByText(/受付準備が完了していません/)).toBeVisible();
    await page.screenshot({ path: path.join(root, 'screenshots', '09-phone-readiness-unverified.png'), fullPage: true });
    const phoneEvidence = JSON.parse(fs.readFileSync(path.join(root, 'phone-provider-evidence.json'), 'utf8'));
    expect(phoneEvidence.command_count).toBe(0);
    evidence.phone_provider_commands = 0;
    evidence.readiness = readiness;
    evidence.checks.push('real-ui-route-create-pinned-realtime-revision', 'masked-directory-test-config', 'draft-readiness-and-live-provider-unverified');
  } else {
  stage = 'prefilled-role';
  await page.goto(`${manifest.frontend_url}/operations?tab=agents&agent=${journey.agent_id}&panel=role`);
  const mission = page.getByLabel('ミッション', { exact: true });
  await expect(mission).toBeVisible();
  const existing = await mission.inputValue();
  expect(existing.length).toBeGreaterThan(0);
  await page.reload();
  await expect(page.getByLabel('ミッション', { exact: true })).toHaveValue(existing);
  evidence.checks.push('exact-prefilled-label-after-reload');
  await page.screenshot({ path: path.join(root, 'screenshots', '06-role-exact-label-smoke.png'), fullPage: true });
  stage = 'activity';
  await page.getByRole('button', { name: '活動・要確認', exact: true }).click();
  await page.getByRole('button', { name: '活動状態を更新', exact: true }).click();
  const response = await page.request.get(`${manifest.frontend_url}/api/python-proxy/operations/command-center?agent_id=${journey.agent_id}&scope=agent`);
  expect(response.status()).toBe(200);
  const snapshot = await response.json();
  const activity = snapshot.activity;
  expect(Array.isArray(activity)).toBe(true);
  const sameAttempt = activity.filter(item => item.id === journey.attempt_ids[0]);
  expect(new Set(sameAttempt.map(item => item.kind)).size).toBeGreaterThanOrEqual(2);
  for (const item of sameAttempt) await expect(page.getByText(item.kind, { exact: true }).first()).toBeVisible();
  evidence.checks.push('authorization-and-attempt-same-id-both-visible');
  await page.screenshot({ path: path.join(root, 'screenshots', '07-activity-composite-key-smoke.png'), fullPage: true });
  if (historyMode) {
    stage = 'history-back';
    await page.reload();
    await expect(page).toHaveURL(/panel=activity/);
    await expect(page.getByRole('button', { name: '活動状態を更新', exact: true })).toBeEnabled();
    evidence.checks.push('hydrated-activity-before-history-navigation');
    await page.goBack();
    await expect(page).toHaveURL(/panel=role/);
  } else {
    await page.getByRole('button', { name: '職務・モデル', exact: true }).click();
  }
  await expect(page.getByLabel('ミッション', { exact: true })).toHaveValue(existing);
  evidence.checks.push('prefilled-role-after-activity-tab');
  }
  expect(evidence.console_errors).toEqual([]);
  expect(evidence.page_errors).toEqual([]);
  expect(evidence.server_errors).toEqual([]);
  expect(evidence.off_scope_requests).toEqual([]);
  const after = JSON.parse(fs.readFileSync(providerPath, 'utf8'));
  expect(after.submission_count).toBe(before.submission_count);
  expect(after.duplicate_attempts).toBe(before.duplicate_attempts);
  evidence.provider_submission_count = after.submission_count;
  evidence.passed = true;
})().catch(error => {
  evidence.passed = false;
  evidence.failure = { stage, type: error.name };
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2));
  process.stdout.write(JSON.stringify({ stage, passed: evidence.passed, output: evidencePath }) + '\n');
});
