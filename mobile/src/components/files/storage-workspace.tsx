import React, { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, FlatList, Modal, Pressable, ScrollView, StyleSheet, View } from "react-native";
import { ActivityIndicator, Button, IconButton, List, Text, TextInput, useTheme, Switch } from "react-native-paper";
import * as DocumentPicker from "expo-document-picker";
import * as FileSystem from "expo-file-system/legacy";
import { useAuth } from "../../contexts/AuthContext";
import { FullScreenModalShell } from "../full-screen-modal-shell";
import { FilesWorkspaceLayout } from "./files-workspace-layout";
import { createStorageRootsClient, storageStatusLabel, type RootInput, type StorageCatalog, type StorageListing,
  type StorageRootInfo, type StorageRootsClient, type StorageText, type StorageEntry,
} from "../../lib/storage-roots-api";

const errorText = (error: unknown) => error instanceof Error ? error.message : "ストレージ操作に失敗しました";
const join = (parent: string, name: string) => parent ? `${parent}/${name}` : name;

function StorageHeaderButton({
  children,
  disabled,
  onPress,
}: {
  children: ReactNode;
  disabled?: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={[styles.storageHeaderButton, disabled && styles.storageDisabled]}
    >
      <Text style={styles.storageHeaderButtonText}>{children}</Text>
    </Pressable>
  );
}

/** `controls` に関数を渡すと、ストレージ設定の歯車を操作行内の好きな位置へ置ける。 */
export type StorageWorkspaceControls = ReactNode | ((storageGear: ReactNode) => ReactNode);

export function NativeStorageWorkspace({children,controls}: {children: ReactNode;controls?: StorageWorkspaceControls}) {
  const {user, isAuthenticated} = useAuth();
  const scope = isAuthenticated ? user?.user_id : "anonymous";
  return <StorageWorkspaceSession key={scope} authenticated={isAuthenticated} controls={controls}>{children}</StorageWorkspaceSession>;
}
function StorageWorkspaceSession({children, controls, authenticated}: {children: ReactNode;controls?: StorageWorkspaceControls;authenticated: boolean}) {
  const [client,setClient]=useState<StorageRootsClient|null>(null);
  const [catalog,setCatalog]=useState<StorageCatalog|null>(null);
  const [selected,setSelected]=useState("default");
  const [manage,setManage]=useState(false);
  const [busy,setBusy]=useState(false);
  const [refreshing,setRefreshing]=useState(false);
  const [error,setError]=useState("");
  const sequence=useRef(0);
  const refreshInFlight=useRef(false);
  const refresh=useCallback(async()=>{
    if(refreshInFlight.current)return;
    refreshInFlight.current=true;
    setRefreshing(true);
    const current=++sequence.current;
    try {
      const api=await createStorageRootsClient();
      const data=await api.catalog();
      if(sequence.current!==current)return;
      setClient(api);setCatalog(data);setError(data.configuration_error?.message||"");
      for(const root of data.roots.filter(root=>root.enabled)) void api.status(root.id).then(status=>{
        if(sequence.current===current)setCatalog(previous=>previous&&({...previous,roots:previous.roots.map(root=>root.id===status.id?status:root)}));
      }).catch(()=>{if(sequence.current===current)setCatalog(previous=>previous&&({...previous,roots:previous.roots.map(item=>item.id===root.id?{...item,status:"offline",online:false}:item)}));});
    }catch(cause){if(sequence.current===current)setError(errorText(cause));}
    finally{
      if(sequence.current===current)setRefreshing(false);
      refreshInFlight.current=false;
    }
  },[]);
  useEffect(()=>{if(authenticated)void refresh();return()=>{sequence.current++;};},[authenticated,refresh]);
  const root=catalog?.roots.find(root=>root.id===selected);
  // 通常はAoiTalk本体のストレージだけを使うため、専用の選択行は置かない。
  // 接続ストレージの選択と管理は歯車の設定画面だけで行う。
  const showGear=Boolean(catalog&&(catalog.is_admin||catalog.roots.length>0||selected!=="default"));
  const gear = showGear ? <IconButton
    icon="cog-outline"
    iconColor="#cdd6f4"
    size={20}
    style={styles.storageGear}
    hitSlop={4}
    disabled={busy}
    onPress={()=>setManage(true)}
    accessibilityLabel="ストレージ設定"
    testID="storage-settings"
  /> : null;
  const storageNotice = error&&catalog?.is_admin?<Text accessibilityRole="alert" style={{padding:8}}>{error}。既存Filesは引き続き利用できます。</Text>:null;
  const workspace = selected==="default"
    ? <FilesWorkspaceLayout controls={<>
        {storageNotice}
        {typeof controls==="function"
          ? controls(gear)
          : <>{gear?<View style={styles.storageGearRow}>{gear}</View>:null}{controls}</>}
      </>}>{children}</FilesWorkspaceLayout>
    : root&&client
      ? <MountedFiles key={`${client.fingerprint}:${root.id}:${root.configuration_revision}`} root={root} api={client} setBusy={setBusy} gear={gear} storageNotice={storageNotice}/>
      : <FilesWorkspaceLayout controls={<>{storageNotice}<View style={styles.storageGearRow}>{gear}</View></>}><View style={{flex:1,minHeight:0,padding:8}}><Text>ストレージが利用できません。AoiTalk Storageへ切り替えてください。</Text></View></FilesWorkspaceLayout>;
  return <View style={{flex:1}}>
    {workspace}
    {client&&catalog&&<StorageSettings visible={manage} catalog={catalog} api={client} selected={selected} refreshing={refreshing} onSelect={id=>{setSelected(id);setManage(false);}} onRefresh={()=>void refresh()} onClose={()=>setManage(false)} onSaved={refresh}/>}
  </View>;
}

type PendingUpload={uri:string;name:string;size:number;id:string;chunk:number;received:number};
function MountedFiles({root,api,setBusy,gear,storageNotice}:{root:StorageRootInfo;api:StorageRootsClient;setBusy:(value:boolean)=>void;gear:ReactNode;storageNotice:ReactNode}){
  const [path,setPath]=useState("");const [listing,setListing]=useState<StorageListing|null>(null);
  const [loading,setLoading]=useState(false);const [busy,setWorking]=useState(false);const [error,setError]=useState("");
  const [text,setText]=useState<StorageText|null>(null);const [trashId,setTrashId]=useState<string|null>(null);
  const [upload,setUpload]=useState<PendingUpload|null>(null);const [filter,setFilter]=useState("");
  const [dialog,setDialog]=useState<{kind:"folder"|"file"|"move";value:string;source?:string}|null>(null);
  const sequence=useRef(0);const mounted=useRef(true);const theme=useTheme();
  const readonly=root.read_only||!root.can_write||!root.enabled;
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;sequence.current++;setBusy(false);};},[setBusy]);
  useEffect(()=>{setBusy(busy||Boolean(text));},[busy,text,setBusy]);
  const reload=useCallback(async()=>{const current=++sequence.current;setLoading(true);setListing(null);setError("");try{const result=await api.list(root.id,path);if(mounted.current&&current===sequence.current)setListing(result);}catch(cause){if(mounted.current&&current===sequence.current)setError(errorText(cause));}finally{if(mounted.current&&current===sequence.current)setLoading(false);}},[api,root.id,path]);
  useEffect(()=>{void reload();},[reload,root.enabled]);
  async function action(operation:()=>Promise<unknown>){setWorking(true);setError("");try{await operation();if(mounted.current)await reload();}catch(cause){if(mounted.current)setError(errorText(cause));}finally{if(mounted.current)setWorking(false);}}
  async function transfer(current:PendingUpload){
    let received=(await api.uploadStatus(root.id,current.id)).received;
    if(received>current.size)throw new Error("アップロード状態が一致しません");
    while(received<current.size){
      const result=await api.chunk(root.id,current.id,received,current.uri,Math.min(current.chunk,current.size-received));
      if(result.received<=received)throw new Error("アップロードが進行していません");
      received=result.received;if(mounted.current)setUpload({...current,received});
    }
    await api.finishUpload(root.id,current.id);if(mounted.current)setUpload(null);
    await FileSystem.deleteAsync(current.uri,{idempotent:true}).catch(()=>undefined);
  }
  async function pickUpload(){
    const picked=await DocumentPicker.getDocumentAsync({copyToCacheDirectory:true,multiple:false});
    if(picked.canceled)return;const file=picked.assets[0];
    await action(async()=>{
      const info=await FileSystem.getInfoAsync(file.uri);
      if(!info.exists||info.isDirectory)throw new Error("選択したファイルを読み込めません");
      const size=file.size??info.size;
      const session=await api.startUpload(root.id,join(path,file.name),size);
      const current={uri:file.uri,name:file.name,size,id:session.upload_id,chunk:session.chunk_bytes,received:0};
      setUpload(current);await transfer(current);
    });
  }
  async function open(entry:StorageEntry){if(entry.is_directory){setPath(entry.path);return;}setWorking(true);setError("");try{const value=await api.text(root.id,entry.path);if(mounted.current)setText(value);}catch(cause){if(mounted.current)setError(errorText(cause));}finally{if(mounted.current)setWorking(false);}}
  const editorControls = <>
    {storageNotice}
    <View style={styles.mountedTitleRow}><Text variant="titleSmall" numberOfLines={1} style={styles.mountedTitle}>{root.name} / {text?.path||""}{readonly?"（読み取り専用）":""}</Text>{gear}</View>
    {error?<Text accessibilityRole="alert">{error}</Text>:null}
    <View style={styles.storageActionsRow}>
      <StorageHeaderButton disabled={readonly||busy} onPress={()=>void action(async()=>{if(!text)return;await api.saveText(root.id,text.path,text.content,text.etag);setText(null);})}>保存</StorageHeaderButton>
      <StorageHeaderButton disabled={busy} onPress={()=>Alert.alert("編集を閉じる","未保存の変更は破棄されます",[{text:"戻る",style:"cancel"},{text:"閉じる",onPress:()=>setText(null)}])}>閉じる</StorageHeaderButton>
    </View>
  </>;
  if(text)return <FilesWorkspaceLayout testID="mounted-files-editor-layout" controls={editorControls}><ScrollView style={styles.editorBody} contentContainerStyle={styles.editorBodyContent} keyboardShouldPersistTaps="handled"><TextInput accessibilityLabel="ファイル内容" multiline style={styles.editorInput} contentStyle={styles.editorInputContent} value={text.content} editable={!readonly&&!busy} onChangeText={content=>setText({...text,content})}/></ScrollView></FilesWorkspaceLayout>;
  const mountedControls = <>
    {storageNotice}
    <View style={styles.mountedTitleRow}><Text variant="titleSmall" numberOfLines={1} style={styles.mountedTitle}>{root.name} / {path||"root"}{readonly?" — 読み取り専用":""}</Text>{gear}</View>
    <View style={styles.storageActionsRow}>
      <StorageHeaderButton disabled={!path||busy} onPress={()=>setPath(path.split("/").slice(0,-1).join("/"))}>上へ</StorageHeaderButton>
      <StorageHeaderButton disabled={busy} onPress={()=>void reload()}>再読み込み</StorageHeaderButton>
      <StorageHeaderButton disabled={readonly||busy} onPress={()=>setDialog({kind:"folder",value:""})}>新規フォルダ</StorageHeaderButton>
      <StorageHeaderButton disabled={readonly||busy} onPress={()=>setDialog({kind:"file",value:""})}>新規テキスト</StorageHeaderButton>
      <StorageHeaderButton disabled={readonly||busy||Boolean(upload)} onPress={()=>void pickUpload().catch(cause=>setError(errorText(cause)))}>アップロード</StorageHeaderButton>
      {trashId&&<StorageHeaderButton disabled={readonly||busy} onPress={()=>void action(async()=>{await api.restore(root.id,trashId);setTrashId(null);})}>削除を戻す</StorageHeaderButton>}
    </View>
    {upload&&<View><Text>{upload.name}: {upload.received.toLocaleString()} / {upload.size.toLocaleString()} bytes</Text><View style={styles.storageActionsRow}><StorageHeaderButton disabled={busy||readonly} onPress={()=>void action(()=>transfer(upload))}>確認して再開</StorageHeaderButton><StorageHeaderButton disabled={busy||readonly} onPress={()=>void action(async()=>{await api.cancelUpload(root.id,upload.id);await FileSystem.deleteAsync(upload.uri,{idempotent:true});setUpload(null);})}>破棄</StorageHeaderButton></View></View>}
    {error?<Text accessibilityRole="alert">{error}</Text>:null}
    <TextInput label="ファイル名で絞り込み" value={filter} onChangeText={setFilter}/>
    {loading?<ActivityIndicator/>:null}
    {listing?.truncated?<Text>表示上限に達しました。フォルダを分けて参照してください。</Text>:null}
  </>;
  return <FilesWorkspaceLayout testID="mounted-files-layout" controls={mountedControls}>
    <View style={styles.mountedBody}>
      <FlatList testID="mounted-files-list" style={styles.mountedList} data={listing?.entries.filter(entry=>entry.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()))||[]} keyExtractor={entry=>entry.path} renderItem={({item})=><View>
      <List.Item title={item.name} titleNumberOfLines={3} description={item.is_directory?"フォルダ":`${(item.size_bytes||0).toLocaleString()} bytes`} left={props=><List.Icon {...props} icon={item.is_directory?"folder":"file"}/>} onPress={()=>{if(!busy)void open(item);}}/>
      <View style={{flexDirection:"row",flexWrap:"wrap"}}>{!item.is_directory&&<Button disabled={busy} onPress={()=>void action(()=>api.download(root.id,item))}>端末に保存</Button>}<Button disabled={readonly||busy} onPress={()=>setDialog({kind:"move",source:item.path,value:item.path})}>名前変更／移動</Button><Button disabled={readonly||busy} onPress={()=>Alert.alert("ごみ箱へ移動",item.name,[{text:"戻る",style:"cancel"},{text:"移動",onPress:()=>void action(async()=>{const result=await api.trash(root.id,item.path);setTrashId(result.trash_id);})}])}>ごみ箱へ</Button></View>
      </View>}/>
    <Modal visible={Boolean(dialog)} transparent animationType="slide" onRequestClose={()=>{if(!busy)setDialog(null);}}><View style={{flex:1,justifyContent:"center",padding:24,backgroundColor:theme.colors.backdrop}}><View style={{padding:16,gap:8,backgroundColor:theme.colors.surface}}><Text>{dialog?.kind==="move"?"移動先（rootからの相対パス）":"新しい名前"}</Text><TextInput value={dialog?.value||""} onChangeText={value=>setDialog(current=>current&&({...current,value}))} autoFocus/><Button disabled={busy||!dialog?.value} onPress={()=>{if(!dialog)return;const current=dialog;void action(async()=>{if(current.kind==="folder")await api.mkdir(root.id,join(path,current.value));else if(current.kind==="file")await api.saveText(root.id,join(path,current.value),"",null);else await api.move(root.id,current.source!,current.value);setDialog(null);});}}>実行</Button><Button disabled={busy} onPress={()=>setDialog(null)}>戻る</Button></View></View></Modal>
    </View>
  </FilesWorkspaceLayout>;
}

function StorageChoice({children,selected,onPress,testID}:{children:ReactNode;selected:boolean;onPress:()=>void;testID?:string}){
  return <Pressable accessibilityRole="radio" accessibilityState={{selected}} onPress={onPress} testID={testID} style={[styles.storageChoice,selected&&styles.storageChoiceSelected]}>
    <Text style={styles.storageChoiceText}>{children}</Text>
  </Pressable>;
}

function StorageSettings({visible,catalog,api,selected,refreshing,onSelect,onRefresh,onClose,onSaved}:{visible:boolean;catalog:StorageCatalog;api:StorageRootsClient;selected:string;refreshing:boolean;onSelect:(id:string)=>void;onRefresh:()=>void;onClose:()=>void;onSaved:()=>Promise<void>}){
  const empty:RootInput={id:"",name:"",root_path:"",read_only:false,enabled:true,external:true,shared:false,project_ids:[],user_ids:[]};
  const [draft,setDraft]=useState<RootInput>(empty);const [editing,setEditing]=useState(false);const [busy,setBusy]=useState(false);const [error,setError]=useState("");
  async function run(operation:()=>Promise<unknown>){setBusy(true);setError("");try{await operation();await onSaved();}catch(cause){setError(errorText(cause));}finally{setBusy(false);}}
  return <FullScreenModalShell visible={visible} title="ストレージ設定" onClose={onClose} closeDisabled={busy} testID="storage-settings-modal">
    <Text variant="titleSmall">表示するストレージ</Text>
    <StorageChoice selected={selected==="default"} onPress={()=>onSelect("default")} testID="storage-default">AoiTalk Storage（既定）</StorageChoice>
    {catalog.roots.map(root=><StorageChoice key={root.id} testID={`storage-root-${root.id}`} selected={selected===root.id} onPress={()=>onSelect(root.id)}>{root.name}・{storageStatusLabel(root)}{root.read_only?"・読取専用":""}</StorageChoice>)}
    <Button disabled={refreshing} icon="refresh" onPress={onRefresh} testID="storage-refresh">接続状態を更新</Button>
    {catalog.is_admin?<>
    <Text variant="titleSmall">外部ストレージの管理</Text>
    <Text>OSでマウント済みのディレクトリを指定します。PostgreSQLとQdrantの保存先は変更しません。</Text><Button disabled={busy} onPress={()=>{setDraft(empty);setEditing(false);}}>新しく登録</Button>
    {catalog.roots.map(root=><Button key={root.id} disabled={busy} onPress={()=>{setDraft({id:root.id,name:root.name,root_path:root.root_path||"",read_only:root.read_only,enabled:root.enabled,external:root.external,shared:root.shared||false,project_ids:root.project_ids,user_ids:root.user_ids||[]});setEditing(true);}}>{root.name}</Button>)}
    <TextInput label="識別ID（小文字英数）" value={draft.id} disabled={editing||busy} onChangeText={id=>setDraft({...draft,id})}/><TextInput label="表示名" value={draft.name} disabled={busy} onChangeText={name=>setDraft({...draft,name})}/><TextInput testID="storage-root-path" accessibilityLabel="ホスト上のroot path" label="ホスト上のroot path" value={draft.root_path} disabled={busy} onChangeText={root_path=>setDraft({...draft,root_path})}/>
    {([['enabled','有効'],['read_only','読み取り専用'],['external','外部マウント（識別ファイル必須）'],['shared','全ユーザーと共有']] as const).map(([key,label])=><View key={key} style={{flexDirection:"row",alignItems:"center",justifyContent:"space-between"}}><Text>{label}</Text><Switch value={draft[key]} disabled={busy} onValueChange={value=>setDraft({...draft,[key]:value})}/></View>)}
    <TextInput disabled={busy} label="Project UUID（カンマ区切り）" value={draft.project_ids.join(", ")} onChangeText={text=>setDraft({...draft,project_ids:text.split(",").map(value=>value.trim())})}/><TextInput disabled={busy} label="User UUID（カンマ区切り）" value={draft.user_ids.join(", ")} onChangeText={text=>setDraft({...draft,user_ids:text.split(",").map(value=>value.trim())})}/><Text>共有・User・Projectを指定しないrootは管理者専用です。Project関連付けはroot全体に適用されます。</Text>
    {error?<Text accessibilityRole="alert">{error}</Text>:null}<Button disabled={busy||!catalog.revision} onPress={()=>void run(async()=>{await api.saveRoot(catalog.revision!,{...draft,project_ids:draft.project_ids.filter(Boolean),user_ids:draft.user_ids.filter(Boolean)});setEditing(true);})}>設定を保存</Button>
    {editing&&draft.external&&<Button disabled={busy} onPress={()=>Alert.alert("外部ストレージの初回登録","目的のストレージがマウント済みであることを確認してください。root内に識別ファイルを1個作成します。",[{text:"戻る",style:"cancel"},{text:"確認済み・登録",onPress:()=>void run(()=>api.enroll(draft.id))}])}>接続済みストレージを初回登録</Button>}
    </>:null}
    <Button disabled={busy} onPress={onClose}>閉じる</Button></FullScreenModalShell>;
}

const styles = StyleSheet.create({
  storageGear: { width: 36, height: 36, margin: 0 },
  storageGearRow: { flexDirection: "row", justifyContent: "flex-end", backgroundColor: "#1e1e2e", paddingHorizontal: 4 },
  storageChoice: {
    minHeight: 44,
    justifyContent: "center",
    paddingHorizontal: 12,
    borderRadius: 8,
    backgroundColor: "#313244",
  },
  storageChoiceSelected: { backgroundColor: "#4c1d95" },
  storageChoiceText: { color: "#cdd6f4" },
  storageDisabled: { opacity: 0.5 },
  mountedTitle: { flex: 1, minWidth: 0 },
  mountedTitleRow: { flexDirection: "row", alignItems: "center", gap: 4, paddingLeft: 8, paddingRight: 4 },
  mountedBody: { flex: 1, minHeight: 0, padding: 8, gap: 8 },
  mountedList: { flex: 1, minHeight: 0 },
  editorBody: { flex: 1, minHeight: 0 },
  editorBodyContent: { flexGrow: 1, minHeight: 0, padding: 8 },
  editorInput: { flex: 1, minHeight: 0 },
  editorInputContent: { minHeight: 0, paddingVertical: 0, textAlignVertical: "top" },
  storageActionsRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "stretch",
    gap: 8,
  },
  storageHeaderButton: {
    maxWidth: "100%",
    minHeight: 44,
    flexShrink: 1,
    justifyContent: "center",
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderWidth: 1,
    borderColor: "#585b70",
    borderRadius: 6,
  },
  storageHeaderButtonText: {
    color: "#cdd6f4",
    flexShrink: 1,
    flexWrap: "wrap",
    textAlign: "center",
  },
});
