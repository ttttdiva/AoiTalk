import { expect, test } from '@playwright/test';
import { addAuthCookie, mockAuthenticatedApis } from './support/auth';

// API fixtures exercise the real chat page and its attachment/evidence UI.
// They do not claim native encoder or database-backed search coverage.
for (const width of [1280, 375]) {
  test(`空queryの添付GETと更新済み根拠の取得を再試行できる (${width}px)`, async ({ page }) => {
    const sessionId = '11111111-1111-4111-8111-111111111111';
    const messageId = '22222222-2222-4222-8222-222222222222';
    const nodeId = '33333333-3333-4333-8333-333333333333';
    const oldHash = 'a'.repeat(64), newHash = 'b'.repeat(64);
    let attachmentGets = 0, searches = 0, evidenceReads = 0;
    const readerVersions: unknown[] = [];
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(String(error)));
    await page.setViewportSize({ width, height: 812 });
    await addAuthCookie(page);
    await mockAuthenticatedApis(page);
    const session = { id: sessionId, user_id: 'user-1', title: '保存済み添付の会話',
      character_name: 'aoi', message_count: 1, is_active: true, is_group_chat: false };
    const message = { id: messageId, session_id: sessionId, role: 'user', content: '検索前の保存済み添付',
      metadata: { attachments: [{ name: 'clip.mp4', path: '_users/user_user-1/clip.mp4', mime_type: 'video/mp4', size: 10 }] },
      created_at: '2026-10-07T00:00:00Z', parent_message_id: null, branch_index: 0, is_active_branch: true };
    await page.route(`**/api/conversations/${sessionId}/resume*`, (route) => route.fulfill({ json: { session, messages: [message] } }));
    await page.route(`**/api/conversations/${sessionId}/messages*`, (route) => route.fulfill({ json: { messages: [message] } }));
    await page.route(`**/api/python-proxy/conversations/${sessionId}/generation/status*`, (route) => route.fulfill({
      json: { session_id: sessionId, running: false, status: 'idle', updated_at: '2026-10-07T00:00:00Z' },
    }));
    await page.route('**/api/python-proxy/search/media**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith('/attachments')) {
        attachmentGets += 1;
        await route.fulfill(attachmentGets === 1 ? { status: 503, json: { detail: 'fixture temporary failure' } }
          : { json: [{ index: 0, filename: 'clip.mp4', kind: 'video', size_bytes: 10 }] });
      } else if (path.endsWith('/evidence')) {
        evidenceReads += 1;
        const body = route.request().postDataJSON() as { citation: { body_sha256: string } };
        readerVersions.push(body.citation.body_sha256);
        await route.fulfill(evidenceReads === 1 ? { status: 409, json: { detail: 'evidence_changed' } }
          : { json: { text: '更新後の根拠', citation: body.citation, complete: true, warnings: [], original_url: `/docs?node_id=${nodeId}` } });
      } else {
        searches += 1;
        const body = route.request().postDataJSON() as { plan: { instruction: string }; use_current_attachments: boolean; attachment_indices: number[] };
        expect(body.plan.instruction).toBe('');
        expect(body.use_current_attachments).toBe(true);
        expect(body.attachment_indices).toEqual([0]);
        await route.fulfill({ json: { version: 1, results: [{ target_kind: 'docs', title: 'Docsの根拠',
          locator: { kind: 'docs_node', id: nodeId }, text: '検索の候補', score: 0, modality: 'text', matched_query_sources: [],
          citation: { derived_text_start: 0, derived_text_end: 3, body_sha256: searches === 1 ? oldHash : newHash } }],
          query_sources: [], lane_status: ['docs', 'knowledge', 'conversation', 'media'].map((target_kind) => ({ target_kind, status: 'complete' })),
          complete: true, warnings: [] } });
      }
    });
    await page.goto(`/chat?s=${sessionId}`);
    await expect(page.getByTestId('chat-message-list').getByText(message.content, { exact: true })).toBeVisible();
    await page.keyboard.press('Control+f');
    await page.getByRole('tab', { name: '添付・統合検索' }).click();
    await expect(page.getByRole('alert')).toContainText('保存済み添付を取得できません');
    expect(searches).toBe(0);
    await page.getByRole('button', { name: '添付一覧を再取得' }).click();
    await page.getByRole('checkbox', { name: /0: clip.mp4/ }).click();
    await expect(page.getByText('Docsの根拠', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '資料を確認' }).click();
    await expect(page.getByRole('alert')).toContainText('再検索');
    await expect(page.getByRole('link', { name: '原本を開く' })).toHaveCount(0);
    await page.getByRole('button', { name: '再試行', exact: true }).click();
    await expect.poll(() => searches).toBe(2);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.getByRole('button', { name: '資料を確認' }).click();
    await expect(page.getByText('更新後の根拠', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '原本を開く' })).toBeVisible();
    expect(attachmentGets).toBe(2);
    expect(readerVersions).toEqual([oldHash, newHash]);
    expect(pageErrors).toEqual([]);
  });
}
