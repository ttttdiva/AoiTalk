"use client";
import { useRef, useState } from "react";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { SettingsDisclosure } from "@/components/settings/settings-disclosure";
import { DEFAULT_PREFERENCES, MOTIONS, type PetMotion } from "@/lib/pets/codex-pet";
import { usePet } from "./pet-provider";
import { PetSprite } from "./pet-sprite";
import { useReducedPetMotion } from "./pet-overlay";

const buttonClass = "rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50";
export function PetSettings() {
  const context = usePet();
  const input = useRef<HTMLInputElement>(null);
  const [motion, setMotion] = useState<PetMotion>("idle");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const systemReduced = useReducedPetMotion();
  if (!context?.available) return null;
  const { pet, registered, legacyPet, ready, connected, busy, error, warning, notice, importFiles, migrateLegacy, updatePreferences, remove, reload } = context;
  const disabled = !ready || !connected || busy;
  return <section aria-label="ペットの設定" className="space-y-3 rounded-md border bg-card p-4 text-card-foreground"
    onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
    onDrop={(event) => { if (!event.dataTransfer.types.includes("Files")) return; event.preventDefault(); event.stopPropagation(); if (!disabled) void importFiles(Array.from(event.dataTransfer.files)); }}>
    <h3 className="text-sm font-semibold">画面のペット（Codex形式・サーバー共通）</h3>
    <p role="status" data-testid="pet-registration-status" className="rounded-md border p-3 text-sm font-medium">
      {!ready ? "サーバーの登録状況を確認中…" : !connected ? "サーバーの登録状況を確認できません。再読み込みしてください。"
        : registered ? `このサーバーにペットが登録済み：${registered.manifest.displayName}` : "ペット未登録：このサーバーにはペットが登録されていません。"}
    </p>
    <p className="text-sm text-muted-foreground">一度登録すると、このAoiTalkサーバーに接続する他のブラウザ・端末でも利用できます。登録・置換・削除はサーバー全体に適用されます。</p>
    <p className="text-sm text-muted-foreground">Codex用のZIPをそのまま選択・ドロップできます。展開済みの場合は pet.json とスプライト画像を一緒に選択してください。Codex v2（1536×2288 px）のPNG / WebPに対応します。</p>
    <input ref={input} type="file" multiple accept=".zip,.json,.png,.webp" aria-label="Codexペットのファイル" className="sr-only" disabled={disabled}
      onChange={(event) => { const files = Array.from(event.currentTarget.files ?? []); event.currentTarget.value = ""; if (files.length) void importFiles(files); }} />
    <div className="flex flex-wrap gap-2">
      <button type="button" className={buttonClass} disabled={disabled} onClick={() => input.current?.click()}>{busy ? "サーバーに保存中…" : !ready ? "読み込み中…" : registered ? "サーバーのペットを置き換える" : "サーバーにペットを登録"}</button>
      <button type="button" className={buttonClass} disabled={busy} onClick={reload}>サーバーの登録を再読み込み</button>
    </div>
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {warning && <p role="status" className="text-xs text-muted-foreground">{warning}</p>}
    {ready && connected && !registered && legacyPet && <div className="space-y-2 rounded-md border p-3">
      <p className="text-sm">このブラウザにだけ保存されていた「{legacyPet.manifest.displayName}」があります。まだサーバーには登録されていません。</p>
      <button type="button" className={buttonClass} disabled={disabled} onClick={() => void migrateLegacy()}>以前のペットをサーバーへ移行</button>
    </div>}
    {registered && !pet && <p className="text-sm text-muted-foreground">登録済みペットの画像を取得しています。表示されない場合はサーバーの登録を再読み込みしてください。</p>}
    {pet && <>
      <div className="flex flex-wrap items-center gap-4">
        <PetSprite image={pet.image} label={`${pet.manifest.displayName}のプレビュー`} motion={motion} size={128} reduced={systemReduced || pet.preferences.reduceMotion} />
        <div className="space-y-2">
          <p className="font-medium">{pet.manifest.displayName}</p>
          <p className="max-w-lg break-words text-xs text-muted-foreground">{pet.manifest.description}</p>
          <label className="flex items-center gap-2 text-sm">モーション確認
            <AppSelect aria-label="プレビューモーション" value={motion} className="rounded border bg-background p-1" onValueChange={(value) => setMotion(value as PetMotion)}>
              {(Object.keys(MOTIONS) as PetMotion[]).map((key) => <option key={key} value={key}>{MOTIONS[key].label}</option>)}
            </AppSelect>
          </label>
        </div>
      </div>
      <fieldset disabled={disabled} className="space-y-3">
        <legend className="sr-only">ペット表示オプション</legend>
        <p className="text-xs text-muted-foreground">以下はこのブラウザのログインユーザー用表示設定です。他の端末の表示は変更しません。</p>
        <label className="flex items-center gap-2 text-sm"><Checkbox disabled={disabled} checked={pet.preferences.enabled} onCheckedChange={(checked) => void updatePreferences({ enabled: checked === true })} />ペットを表示する</label>
        <label className="flex items-center gap-2 text-sm">表示サイズ
          <AppSelect aria-label="ペットの表示サイズ" value={pet.preferences.size} className="rounded border bg-background p-1" disabled={disabled} onValueChange={(value) => void updatePreferences({ size: Number(value) })}>
            {[64, 96, 128, 160, 192].map((size) => <option key={size} value={size}>{size} px</option>)}
          </AppSelect>
        </label>
        <label className="flex items-center gap-2 text-sm"><Checkbox disabled={disabled} checked={pet.preferences.followPointer} onCheckedChange={(checked) => void updatePreferences({ followPointer: checked === true })} />マウスの方向を見る</label>
        <label className="flex items-center gap-2 text-sm"><Checkbox disabled={disabled} checked={pet.preferences.reduceMotion} onCheckedChange={(checked) => void updatePreferences({ reduceMotion: checked === true })} />アニメーションを抑える</label>
        {systemReduced && <p className="text-xs text-muted-foreground">OS / ブラウザの「動きを減らす」設定により、静止表示しています。</p>}
        <button type="button" className={buttonClass} onClick={() => void updatePreferences({ x: DEFAULT_PREFERENCES.x, y: DEFAULT_PREFERENCES.y })}>表示位置をリセット</button>
      </fieldset>
      <p className="text-xs text-muted-foreground">ドラッグで移動、クリックで手振り。会話の返答待ち・生成中・完了・失敗に反応します。ペットにフォーカスして矢印キーでも移動できます。</p>
    </>}
    {registered && (confirmRemove ? <div role="group" aria-label="ペット削除の確認" className="flex flex-wrap items-center gap-2 text-sm">
      <span>このサーバーのペット登録を削除しますか？ 他のブラウザ・端末からも利用できなくなります。</span>
      <button type="button" className={buttonClass} disabled={disabled} onClick={() => void remove().then((ok) => { if (ok) setConfirmRemove(false); })}>サーバーから削除する</button>
      <button type="button" className={buttonClass} onClick={() => setConfirmRemove(false)}>キャンセル</button>
    </div> : <button type="button" className={buttonClass} disabled={disabled} onClick={() => setConfirmRemove(true)}>サーバーのペット登録を削除</button>)}
    <p className="text-xs text-muted-foreground">ペット本体はサーバーに保存され、ブラウザのサイトデータ削除やAoiTalkの再起動でも登録は保持されます。他端末の変更は表示中の画面で約15秒ごと、または画面へ戻った時に確認します。元のCodexペットは変更しません。</p>
  </section>;
}

/** Settings overview owns only this compact entry; server state stays in PetProvider. */
export function PetSettingsEntry() {
  const context = usePet();
  if (!context?.available) return null;
  const summary = !context.ready ? "サーバーを確認中" : !context.connected ? "登録状況を確認できません"
    : context.registered ? `サーバー登録済み：${context.registered.manifest.displayName}` : "サーバー未登録";
  return <SettingsDisclosure title="画面のペット" id="pet" targetId="pet"
    defaultOpen={typeof window !== "undefined" && window.location.hash === "#pet"}
    summary={<span className="text-xs text-muted-foreground">{summary}</span>}>
    <PetSettings />
  </SettingsDisclosure>;
}
