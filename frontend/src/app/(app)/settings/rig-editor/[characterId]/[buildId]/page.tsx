'use client';

import { useState } from 'react';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import { Button } from '@/components/ui/button';
import { rigRequest, rigUrl } from '@/lib/seethrough-rig/api';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type EditorInfo = { core_ref: string; status: string };

export default function RigEditorPage() {
  const params = useParams<{ characterId: string; buildId: string }>();
  const valid = UUID.test(params.characterId) && UUID.test(params.buildId);
  const root = rigUrl(params.characterId);
  const { data, error, isLoading } = useSWR<EditorInfo>(
    valid ? `${root}/builds/${params.buildId}/editor-info` : null,
    (url: string) => rigRequest<EditorInfo>(url),
    { revalidateOnFocus: false },
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function createCandidate() {
    setBusy(true);
    setMessage(null);
    try {
      await rigRequest(`${root}/builds`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: params.buildId }),
      });
      setMessage('新しい候補の制作を依頼しました。キャラクター設定で確認してください。使用中モデルは変更していません。');
    } catch (failure) {
      setMessage(failure instanceof Error ? failure.message : '制作を依頼できませんでした。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">アバターを再編集</h1>
          <p className="text-sm text-muted-foreground">編集画面で設定を保存してから、新しい候補を制作してください。使用中のモデルへ直接上書きはしません。</p>
        </div>
        <Button disabled={!data || busy} onClick={() => void createCandidate()}>
          {busy ? '依頼中…' : '保存した調整から新しい候補を制作'}
        </Button>
      </div>
      {message && <p role="status" className="text-sm">{message}</p>}
      {!valid && <p role="alert">モデルの指定が不正です。</p>}
      {isLoading && <p role="status">制作プロジェクトを確認しています…</p>}
      {error && <p role="alert">{error instanceof Error ? error.message : '制作プロジェクトを開けません。'}</p>}
      {data && (
        <iframe
          title="See-through Rig 制作プロジェクト"
          src={`${root}/builds/${params.buildId}/editor/editor.html?model=models/project&coreRef=${encodeURIComponent(data.core_ref)}`}
          className="min-h-[600px] w-full flex-1 rounded-lg border"
          style={{ height: 'calc(100dvh - 12rem)' }}
          sandbox="allow-scripts allow-same-origin allow-downloads"
          allow="microphone"
          referrerPolicy="no-referrer"
        />
      )}
    </section>
  );
}
