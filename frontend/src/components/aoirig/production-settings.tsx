'use client';

import { useState } from 'react';
import useSWR from 'swr';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { productionUrl, rigRequest, type ProductionSettings } from '@/lib/seethrough-rig/api';

export function RigProductionSettings({ onChange }: { onChange?: () => void }) {
  const { data, error, mutate } = useSWR(
    productionUrl + '/settings',
    (url: string) => rigRequest<ProductionSettings>(url),
    { shouldRetryOnError: false, refreshInterval: value => value?.status.startsWith('running:') ? 2000 : 0 },
  );
  const [path, setPath] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');

  async function act(action: string, body?: { enabled: boolean; path: string | null }) {
    setBusy(true);
    setFailure('');
    try {
      const updated = await rigRequest<ProductionSettings>(productionUrl + action, {
        method: action === '/settings' ? 'PUT' : 'POST',
        ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
      });
      // Turning on or changing a path only diagnoses an existing installation.
      // Missing installations always require their own explicit button press.
      if (action === '/settings' && body?.enabled && updated.installed) {
        await rigRequest(productionUrl + '/doctor', { method: 'POST' });
      }
      await mutate();
      onChange?.();
    } catch (cause) {
      setFailure(cause instanceof Error ? cause.message : '操作に失敗しました');
      await mutate();
    } finally {
      setBusy(false);
    }
  }

  const state = !data ? '' : data.status.startsWith('running:') ? '環境を準備しています'
    : data.status.startsWith('interrupted:') ? '前回の処理が中断されました。再実行できます'
    : data.status.startsWith('failed:') ? '環境の準備に失敗しました。問題を確認して再実行してください'
    : data.ready ? '制作を利用できます' : data.installed ? '制作環境の確認が必要です' : '制作環境は未導入です';

  return (
    <details className="rounded border p-3 text-sm">
      <summary className="cursor-pointer">See-through Rigでアバターを制作する {data?.enabled ? 'ON' : 'OFF'}</summary>
      <div className="mt-3 space-y-3">
        <p className="text-xs text-muted-foreground">制作連携は任意です。OFFでも完成パッケージの取込とモデル再生を利用できます。</p>
        {data && <>
          <p role="status">{state}</p>
          {(data.problems ?? []).map((problem, index) => <p key={index} className="text-xs">{problem}</p>)}
          {data.can_manage ? <>
            <div className="flex items-center gap-2">
              <Checkbox id="rig-production-enabled" checked={data.enabled} disabled={busy}
                onCheckedChange={value => void act('/settings', { enabled: value === true, path: data.path ?? null })} />
              <Label htmlFor="rig-production-enabled">制作連携を使う</Label>
            </div>
            <div className="space-y-1">
              <Label htmlFor="rig-production-path">制作を実行するサーバー上の場所</Label>
              <div className="flex gap-2">
                <Input id="rig-production-path" value={path ?? data.path ?? ''} placeholder="自動検出" disabled={busy}
                  onChange={event => setPath(event.target.value)} />
                <Button type="button" size="sm" disabled={busy}
                  onClick={() => void act('/settings', { enabled: data.enabled, path: (path ?? data.path ?? '').trim() || null })}>保存</Button>
              </div>
              {data.effective_path && <p className="break-all text-xs text-muted-foreground">使用する場所: {data.effective_path}</p>}
            </div>
            <div className="flex flex-wrap gap-2">
              {data.installed && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void act('/doctor')}>再確認</Button>}
              {!data.installed && <Button type="button" size="sm" disabled={busy} onClick={() => void act('/install')}>この場所に制作アプリを導入</Button>}
              {data.installed && !data.ready && <Button type="button" size="sm" disabled={busy} onClick={() => void act('/prepare')}>制作環境を準備</Button>}
              {data.installed && data.managed && <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void act('/update')}>制作アプリを更新</Button>}
            </div>
            {(!data.installed || !data.ready) && <p className="text-xs text-muted-foreground">導入・準備はこの操作で明示的に開始します。既存環境を再利用し、推論モデルの重みは自動ダウンロードしません。</p>}
          </> : <p>連携設定は管理者が変更します。</p>}
        </>}
        {(failure || error) && <p role="alert" className="text-sm">{failure || '連携設定を取得できません'}</p>}
      </div>
    </details>
  );
}
