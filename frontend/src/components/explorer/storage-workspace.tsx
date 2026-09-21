"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AppSelect } from "@/components/ui/app-select";
import { Checkbox } from "@/components/ui/checkbox";
import { RefreshCw, Settings } from "lucide-react";
import { storageRootsApi as api, storageStatusLabel, type RootInput, type StorageCatalog,
  type StorageEntry, type StorageListing, type StorageRootInfo, type StorageText,
} from "@/lib/storage-roots-api";

const control = "rounded border px-3 py-2 text-sm disabled:opacity-40 disabled:cursor-not-allowed";
const message = (error: unknown) => error instanceof Error ? error.message : "ストレージ操作に失敗しました";
const join = (parent: string, name: string) => parent ? `${parent}/${name}` : name;

/** Existing Personal/Project Files remain the default, including offline clients. */
type StorageWorkspaceContextValue = {
  catalog: StorageCatalog | null;
  selected: string;
  setSelected: (value: string) => void;
  manage: boolean;
  setManage: (value: boolean | ((value: boolean) => boolean)) => void;
  busy: boolean;
  setBusy: (value: boolean) => void;
  error: string;
  refresh: () => Promise<void>;
  root: StorageRootInfo | undefined;
};

const StorageWorkspaceContext = createContext<StorageWorkspaceContextValue | null>(null);

function useStorageWorkspaceContext() {
  const value = useContext(StorageWorkspaceContext);
  if (!value) throw new Error("StorageWorkspace context is missing");
  return value;
}

export function useStorageWorkspace() {
  const {selected, setSelected, catalog, root} = useStorageWorkspaceContext();
  return {
    selectedStorageId: selected,
    isDefaultStorage: selected === "default",
    catalogAvailable: catalog !== null,
    selectedStorageRoot: root,
    selectDefaultStorage: () => setSelected("default"),
  };
}

export function StorageWorkspace({children}: {children: ReactNode}) {
  const [catalog, setCatalog] = useState<StorageCatalog | null>(null);
  const [selected, setSelected] = useState("default");
  const [manage, setManage] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    try {
      const data = await api.catalog();
      if (!data || !Array.isArray(data.roots)) throw new Error("ストレージ一覧の応答形式が不正です");
      if (current !== generation.current) return;
      setCatalog(data); setError(data.configuration_error?.message || "");
      // No Promise.all gate: each root becomes available independently.
      for (const root of data.roots.filter(item => item.enabled)) {
        void api.status(root.id).then(status => {
          if (generation.current === current) setCatalog(previous => previous && ({...previous, roots: previous.roots.map(item => item.id === status.id ? status : item)}));
        }).catch(() => {
          if (generation.current === current) setCatalog(previous => previous && ({...previous, roots: previous.roots.map(item => item.id === root.id ? {...item, status: "offline", online: false} : item)}));
        });
      }
    } catch (cause) {
      // The legacy UI never disappears just because this optional API is down.
      if (current === generation.current) setError(message(cause));
    }
  }, []);
  useEffect(() => {void refresh(); return () => {generation.current += 1;};}, [refresh]);
  const root = catalog?.roots.find(item => item.id === selected);
  return <StorageWorkspaceContext.Provider value={{catalog, selected, setSelected, manage, setManage, busy, setBusy, error, refresh, root}}>
    <div className="flex h-full min-h-0 w-full flex-col overflow-hidden">{children}</div>
  </StorageWorkspaceContext.Provider>;
}

export function StorageControls() {
  const {catalog, selected, setSelected, manage, setManage, busy, refresh} = useStorageWorkspaceContext();
  if (!(catalog?.is_admin || Boolean(catalog?.roots.length) || selected !== "default")) return null;
  return <div className="flex min-w-0 shrink items-center gap-1.5" aria-label="ファイルストレージ">
    <span className="hidden shrink-0 text-[11px] text-muted-foreground lg:inline">Storage:</span>
    <AppSelect
      className="h-8 min-w-0 max-w-48 rounded border px-2 text-xs disabled:cursor-not-allowed disabled:opacity-40 sm:max-w-56"
      aria-label="ストレージ選択"
      title="表示するストレージ"
      value={selected}
      disabled={busy}
      onChange={event => setSelected(event.target.value)}
    >
      <option value="default">AoiTalk Storage</option>
      {catalog?.roots.map(item => <option key={item.id} value={item.id}>{item.name} — {storageStatusLabel(item)}{item.read_only ? "・読み取り専用" : ""}</option>)}
    </AppSelect>
    <button
      type="button"
      className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
      disabled={busy}
      onClick={() => void refresh()}
      title="接続状態を更新"
      aria-label="接続状態を更新"
    ><RefreshCw className="size-4" aria-hidden="true" /></button>
    {catalog?.is_admin && <button
      type="button"
      className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
      disabled={busy}
      onClick={() => setManage(value => !value)}
      title={manage ? "ストレージ設定を閉じる" : "ストレージ設定"}
      aria-label={manage ? "ストレージ設定を閉じる" : "ストレージ設定"}
      aria-pressed={manage}
    ><Settings className="size-4" aria-hidden="true" /></button>}
  </div>;
}

export function StorageWorkspacePanel() {
  const {catalog, selected, manage, error, refresh, root, setBusy, setManage} = useStorageWorkspaceContext();
  const showStatus = Boolean(error && catalog?.is_admin);
  const showManager = Boolean(manage && catalog?.is_admin);
  const showStorage = selected !== "default";
  if (!showStatus && !showManager && !showStorage) return null;
  return <div className={showStorage ? "flex min-h-0 flex-1 flex-col overflow-hidden" : "shrink-0"}>
    {showStatus && <p role="status" className="shrink-0 px-3 py-2 text-sm">{error}。AoiTalk Storageはそのまま利用できます。</p>}
    {showManager && catalog && <StorageRootEditor catalog={catalog} onSaved={refresh} onClose={() => setManage(false)} />}
    {showStorage && (root ? <MountedStorageBrowser key={`${root.id}:${root.configuration_revision}`} root={root} setBusy={setBusy} /> : <div role="alert" className="p-4">選択したストレージが利用できないか、アクセス権が変更されました。AoiTalk Storageへ切り替えてください。</div>)}
  </div>;
}

function StorageRootEditor({catalog, onSaved, onClose}: {catalog: StorageCatalog; onSaved: () => Promise<void>; onClose: () => void}) {
  const empty: RootInput = {id: "", name: "", root_path: "", read_only: false, enabled: true, external: true, shared: false, project_ids: [], user_ids: []};
  const [draft, setDraft] = useState<RootInput>(empty);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const current = catalog.roots.find(root => root.id === draft.id);
  const update = <K extends keyof RootInput>(key: K, value: RootInput[K]) => setDraft(old => ({...old, [key]: value}));
  async function save() {
    if (!catalog.revision) return;
    setBusy(true); setError("");
    try {await api.saveRoot(catalog.revision, draft); setEditing(true); await onSaved();}
    catch (cause) {setError(message(cause));}
    finally {setBusy(false);}
  }
  return <section className="max-h-[55vh] overflow-auto border-b p-4" aria-label="ストレージ設定">
    <div className="flex flex-wrap items-center gap-2"><h2 className="font-semibold">ストレージ設定</h2><AppSelect className={control} aria-label="編集するストレージ" disabled={busy} value={editing ? draft.id : ""} onChange={event => {
      const root = catalog.roots.find(item => item.id === event.target.value);
      setEditing(Boolean(root)); setError("");
      setDraft(root ? {id: root.id, name: root.name, root_path: root.root_path || "", enabled: root.enabled, read_only: root.read_only, external: root.external, shared: root.shared || false, project_ids: root.project_ids, user_ids: root.user_ids || []} : empty);
    }}><option value="">新しく登録</option>{catalog.roots.map(root => <option key={root.id} value={root.id}>{root.name}</option>)}</AppSelect></div>
    <p className="my-2 text-sm">OSでマウント済みのディレクトリを指定します。設定の保存だけではフォルダ作成・マウント・ファイル移動を行いません。</p>
    <div className="grid gap-2 md:grid-cols-2">
      <label>識別ID<input className={`${control} w-full`} disabled={editing || busy} value={draft.id} onChange={event => update("id", event.target.value)} placeholder="main-pc / nas" /></label>
      <label>表示名<input className={`${control} w-full`} disabled={busy} value={draft.name} onChange={event => update("name", event.target.value)} placeholder="Main PC" /></label>
      <label className="md:col-span-2">root path<input className={`${control} w-full`} disabled={busy} value={draft.root_path} onChange={event => update("root_path", event.target.value)} placeholder="/mnt/main-pc/AoiTalk" /></label>
      {([['enabled','有効'],['read_only','読み取り専用'],['external','外部マウント（識別ファイル必須）'],['shared','全ユーザーと共有']] as const).map(([key,label]) => <label key={key} className="flex items-center gap-2"><Checkbox checked={draft[key]} disabled={busy} onCheckedChange={checked => update(key, checked === true)} />{label}</label>)}
      <label>関連ProjectのUUID（カンマ区切り）<input className={`${control} w-full`} disabled={busy} value={draft.project_ids.join(", ")} onChange={event => update("project_ids",event.target.value.split(",").map(value => value.trim()))} onBlur={() => update("project_ids",draft.project_ids.filter(Boolean))} /></label>
      <label>許可するUserのUUID（カンマ区切り）<input className={`${control} w-full`} disabled={busy} value={draft.user_ids.join(", ")} onChange={event => update("user_ids",event.target.value.split(",").map(value => value.trim()))} onBlur={() => update("user_ids",draft.user_ids.filter(Boolean))} /></label>
    </div>
    <p className="my-2 text-sm">共有・User・Projectの指定がなければ管理者のみ利用できます。Project関連付けはこのroot全体への権限を付与し、既存のProject保存先は移しません。</p>
    {error && <p role="alert">{error}</p>}
    <div className="flex flex-wrap gap-2"><button className={control} disabled={busy || !catalog.revision} onClick={() => void save()}>設定を保存</button>
      {editing && current?.external && <button className={control} disabled={busy} onClick={async () => {
        if (!window.confirm(`${current.name}が目的のストレージに接続済みであることを確認しましたか？ root内に識別ファイルを1個作成します。未マウント時には実行しないでください。`)) return;
        setBusy(true);setError("");try {await api.enroll(current.id);await onSaved();} catch(cause) {setError(message(cause));} finally {setBusy(false);}
      }}>接続済みストレージを初回登録</button>}
      <button className={control} disabled={busy} onClick={onClose}>閉じる</button></div>
    {current?.external && <p className="mt-2 break-all text-xs">OS側が読み取り専用の場合は、ストレージ所有者が {current.marker_name} をroot直下に作成し、内容を {current.identity} にしてください。</p>}
  </section>;
}

function MountedStorageBrowser({root, setBusy}: {root: StorageRootInfo; setBusy: (busy: boolean) => void}) {
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<StorageListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [text, setText] = useState<StorageText | null>(null);
  const [lastTrash, setLastTrash] = useState<string | null>(null);
  const [upload, setUpload] = useState<{file: File; id: string; bytes: number; chunk: number} | null>(null);
  const seq = useRef(0);
  const mounted = useRef(true);
  const readonly = root.read_only || !root.can_write || !root.enabled;
  useEffect(() => {mounted.current = true;return () => {mounted.current = false;seq.current += 1;setBusy(false);};}, [setBusy]);
  useEffect(() => {setBusy(mutating || Boolean(text));}, [mutating, text, setBusy]);
  const reload = useCallback(async () => {
    const request = ++seq.current;setLoading(true);setError("");setListing(null);
    try {const data = await api.list(root.id,path);if(request===seq.current && mounted.current)setListing(data);}
    catch(cause){if(request===seq.current && mounted.current)setError(message(cause));}
    finally{if(request===seq.current && mounted.current)setLoading(false);}
  },[root.id,path]);
  useEffect(() => {void reload();},[reload,root.enabled]);
  async function action(operation: () => Promise<unknown>) {
    setMutating(true);setError("");
    try {await operation();if(mounted.current)await reload();}
    catch(cause){if(mounted.current)setError(message(cause));}
    finally{if(mounted.current)setMutating(false);}
  }
  async function transfer(current: NonNullable<typeof upload>) {
    const status = await api.uploadStatus(root.id,current.id);
    let received = status.received;
    if (received > current.file.size) throw new Error("アップロード状態が一致しません");
    while(received < current.file.size){
      const result = await api.chunk(root.id,current.id,received,current.file.slice(received,received+current.chunk));
      if (result.received <= received) throw new Error("アップロードが進行していません");
      received = result.received;
      if(mounted.current)setUpload({...current,bytes:received});
    }
    await api.finishUpload(root.id,current.id);
    if(mounted.current)setUpload(null);
  }
  async function open(entry: StorageEntry) {
    if(entry.is_directory){setPath(entry.path);return;}
    setError("");setMutating(true);
    try {const value = await api.text(root.id,entry.path);if(mounted.current)setText(value);}
    catch(cause){if(mounted.current)setError(message(cause));}
    finally{if(mounted.current)setMutating(false);}
  }
  if (text) return <section className="flex min-h-0 flex-1 flex-col p-3" aria-label="外部ストレージのテキスト編集">
    <h2 className="break-all font-semibold">{root.name} / {text.path}{readonly ? "（読み取り専用）" : ""}</h2>
    <textarea className="my-2 min-h-64 flex-1 rounded border bg-background p-3 font-mono" aria-label="ファイル内容" readOnly={readonly || mutating} value={text.content} onChange={event => setText({...text,content:event.target.value})} />
    {error && <p role="alert">{error}</p>}<div className="flex gap-2"><button className={control} disabled={readonly || mutating} onClick={() => void action(async () => {await api.saveText(root.id,text.path,text.content,text.etag);setText(null);})}>保存</button><button className={control} disabled={mutating} onClick={() => {if(readonly || window.confirm("編集画面を閉じます。未保存の変更は破棄されます。"))setText(null);}}>閉じる</button></div>
  </section>;
  return <section className="flex min-h-0 flex-1 flex-col gap-2 overflow-auto p-3" aria-label={`${root.name}のファイル`}>
    <h2 className="break-all font-semibold">{root.name} / {path || "（root）"} {readonly ? "— 読み取り専用" : ""}</h2>
    {root.root_path && <p className="break-all text-xs text-muted-foreground">保存先: {root.root_path}</p>}
    <div className="flex flex-wrap gap-2">
      <button className={control} disabled={!path || mutating} onClick={() => setPath(path.split("/").slice(0,-1).join("/"))}>上のフォルダ</button>
      <button className={control} disabled={mutating} onClick={() => void reload()}>再読み込み</button>
      <button className={control} disabled={readonly || mutating} onClick={() => {const name=window.prompt("新しいフォルダ名");if(name)void action(() => api.mkdir(root.id,join(path,name)));}}>新規フォルダ</button>
      <button className={control} disabled={readonly || mutating} onClick={() => {const name=window.prompt("新しいテキストファイル名");if(name)void action(() => api.saveText(root.id,join(path,name),"",null));}}>新規テキスト</button>
      <label className={control}>アップロード<input type="file" className="block max-w-56" disabled={readonly || mutating || Boolean(upload)} onChange={event => {
        const file=event.target.files?.[0];event.target.value="";if(!file)return;
        void action(async () => {const result=await api.startUpload(root.id,join(path,file.name),file.size);const current={file,id:result.upload_id,bytes:0,chunk:result.chunk_bytes};setUpload(current);await transfer(current);});
      }}/></label>
      {lastTrash && <button className={control} disabled={readonly || mutating} onClick={() => void action(async () => {await api.restore(root.id,lastTrash);setLastTrash(null);})}>直前の削除を戻す</button>}
    </div>
    {upload && <div role="status" className="flex flex-wrap items-center gap-2"><span>{upload.file.name}: {upload.bytes.toLocaleString()} / {upload.file.size.toLocaleString()} bytes</span><button className={control} disabled={mutating || readonly} onClick={() => void action(() => transfer(upload))}>状態を確認して再開</button><button className={control} disabled={mutating || readonly} onClick={() => void action(async () => {await api.cancelUpload(root.id,upload.id);setUpload(null);})}>アップロードを破棄</button></div>}
    {error && <p role="alert" className="whitespace-pre-wrap rounded border p-3">{error}</p>}
    <input className={control} placeholder="このフォルダの名前で絞り込み" aria-label="ファイル名フィルター" value={filter} onChange={event=>setFilter(event.target.value)} />
    {loading && <p role="status">読み込み中…</p>}
    {listing?.truncated && <p role="status">表示上限に達しました。フォルダを分けて参照してください。</p>}
    {listing && listing.skipped_entries > 0 && <p className="text-xs">リンク・管理用パス等 {listing.skipped_entries} 件を非表示にしています。</p>}
    <div className="overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">名前</th><th className="p-2">サイズ</th><th className="p-2">操作</th></tr></thead><tbody>
      {listing?.entries.filter(item=>item.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(entry=><tr key={entry.path} className="border-t">
        <td className="p-2"><button className="max-w-full break-all text-left underline" disabled={mutating} onClick={()=>void open(entry)}>{entry.is_directory?"📁 ":""}{entry.name}</button></td>
        <td className="whitespace-nowrap p-2">{entry.size_bytes === null?"—":`${entry.size_bytes.toLocaleString()} B`}</td>
        <td className="p-2"><div className="flex flex-wrap gap-2">{!entry.is_directory && <a className={control} href={api.downloadUrl(root.id,entry.path)} download={entry.name}>ダウンロード</a>}
          <button className={control} disabled={readonly||mutating} onClick={()=>{const destination=window.prompt("同じストレージ内の移動先（rootからの相対パス）",entry.path);if(destination&&destination!==entry.path)void action(()=>api.move(root.id,entry.path,destination));}}>名前変更／移動</button>
          <button className={control} disabled={readonly||mutating} onClick={()=>{if(window.confirm(`${entry.name}をこのストレージのごみ箱へ移しますか？`))void action(async()=>{const result=await api.trash(root.id,entry.path);setLastTrash(result.trash_id);});}}>ごみ箱へ</button>
        </div></td></tr>)}
    </tbody></table></div>
    {listing?.entries.length === 0 && <p>このフォルダは空です。</p>}
  </section>;
}
