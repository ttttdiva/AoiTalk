"use client";
import {useEffect,useRef,useState} from 'react';
import {withBasePath} from '@/lib/base-path';
import Image from 'next/image';
import useSWR from 'swr';
import {useCurrentUserId} from '@/components/providers/swr-global-provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {HelpPopover} from '@/components/ui/help-popover';
import {AppSelect} from '@/components/ui/app-select';
import {Dialog,DialogContent,DialogDescription,DialogFooter,DialogHeader,DialogTitle} from '@/components/ui/dialog';
import {PlaybackSettingsPreview} from '@/components/aoirig/playback-settings-preview';
import {RigCanvas} from '@/components/aoirig/character-animation';
import {RigProductionSettings} from '@/components/aoirig/production-settings';
import {CharacterVoicePreview} from '@/components/aoirig/voice-preview';
import {buildFormat,buildSource,buildPreview,isProductionBuild,rigFetch,rigUrl,rigRequest,terminalBuild} from '@/lib/seethrough-rig/api';
import type {ConversationState} from '@/lib/aoirig/conversation';
import type {ReactionCue} from '@/lib/aoirig/reactions';
const labels:Record<string,string>={queued:'制作待ち',creating:'制作プロジェクトを準備中',exporting:'モデルを出力中',running:'制作中',decomposing:'画像を分解中',assembling:'モデルを組み立て中',validating:'検証中',previewing:'プレビューを準備中',ready:'プレビューで見た目を確認してください',failed:'制作に失敗しました',cancelled:'制作を中止しました',canceled:'制作を中止しました',needs_input:'制作に追加素材が必要です',unsupported:'この操作は未対応です'};
const expressionLabel:Record<string,string>={neutral:'ふつう',happy:'にっこり',worried:'困り顔',shy:'照れ笑い',surprised:'驚き',jitome:'じと目'};
const strings=(value:unknown)=>Array.isArray(value)?value.filter((item):item is string=>typeof item==='string'):[];
export function CharacterAnimationSettings({characterId,characterName}:{characterId:string;characterName:string}){
 const owner=useCurrentUserId();
 // A different owner/character must not inherit preview state or pending actions.
 return <CharacterAnimationSettingsContent key={JSON.stringify([owner,characterId])} characterId={characterId} characterName={characterName}/>;
}
function CharacterAnimationSettingsContent({characterId,characterName}:{characterId:string;characterName:string}){
 const owner=useCurrentUserId();
 const [expressionScope,setExpressionScope]=useState('');
 const url=rigUrl(characterId),{data,error:loadError,mutate}=useSWR(characterId?url:null,rigFetch,{refreshInterval:value=>value?.builds.some(b=>!terminalBuild(b.status))?2000:0,shouldRetryOnError:false,keepPreviousData:false});
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[preview,setPreview]=useState<string|null>(null),[previous,setPrevious]=useState<string|null|undefined>();
 const [deleteTarget,setDeleteTarget]=useState<string|null>(null);
 const requestPending=useRef(false),mounted=useRef(true);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
 const [expression,setExpression]=useState('neutral'),[focus,setFocus]=useState<'full'|'face'>('full'),[paused,setPaused]=useState(false),[motion,setMotion]=useState<ConversationState>('idle'),[cue,setCue]=useState<ReactionCue>();
 const [reported,setReported]=useState<{id:string;caps:Record<string,unknown>}|null>(null);
 async function submit(path:string,init:RequestInit,onSuccess?:(result:{url?:string;success?:boolean})=>void|Promise<unknown>){
  if(requestPending.current)return;
  requestPending.current=true;setBusy(true);setError('');
  try{
   const result=await rigRequest<{url?:string;success?:boolean}>(url+path,init);
   if(!mounted.current)return;
   await onSuccess?.(result);
   if(!mounted.current)return;
   await mutate();
   if(mounted.current)return result;
  }catch(e){if(mounted.current)setError(e instanceof Error?e.message:'操作に失敗しました');}
  finally{requestPending.current=false;if(mounted.current)setBusy(false);}
 }
 async function activate(revision:string|null){const old=data?.active_revision;const result=await submit('/active',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision})});if(result){setPrevious(old);setPreview(null);}}
 async function deleteBuild(){
  const target=data?.builds.find(build=>build.id===deleteTarget);
  if(!target||!terminalBuild(target.status))return;
  await submit('/builds/'+encodeURIComponent(target.id),{method:'DELETE'},async result=>{
   if(result.success!==true)throw new Error('削除を確認できませんでした');
   if((preview??data?.active_revision)===target.id){setReported(null);setCue(undefined);}
   setPreview(current=>current===target.id?null:current);
   setPrevious(current=>current===target.id?undefined:current);
   setDeleteTarget(null);
   // The chat player shares this SWR key. Update it before revalidation so a
   // deleted active model cannot continue playing if the refresh is delayed.
   await mutate(current=>{
    if(!current)return current;
    const builds=current.builds.filter(build=>build.id!==target.id),deletesActive=current.active_revision===target.id;
    return {...current,builds,active_revision:deletesActive?null:current.active_revision,active_format:deletesActive?null:current.active_format,has_model:builds.some(build=>build.status==='ready'&&buildFormat(build)==='seethrough-rig')};
   },{revalidate:false});
  });
 }
 const revision=preview??data?.active_revision,selected=data?.builds.find(b=>b.id===revision),latest=data?.builds[0],building=data?.builds.some(b=>!terminalBuild(b.status));
 const format=buildFormat(selected),playable=format==='seethrough-rig';
 const caps:Record<string,unknown>=playable&&reported&&reported.id===revision?reported.caps:{};
 const expressions=strings(caps.expressions),views=strings(caps.views);
 const canPreviewJitome=strings(caps.parameters).includes('EyeJitome')&&!expressions.includes('jitome');
 const expressionChoices=canPreviewJitome?[...expressions,'jitome']:expressions;
 const scope=JSON.stringify([owner,characterId,revision]);
 if(expressionScope!==scope){setExpressionScope(scope);setExpression('neutral');}
 const previewJitome=canPreviewJitome&&expression==='jitome';
 const previousBuild=typeof previous==='string'?data?.builds.find(b=>b.id===previous):undefined;
 const deletingBuild=data?.builds.find(build=>build.id===deleteTarget);
 const fallback=data?.has_source?<Image src={url+'/source.png'} alt={characterName} width={320} height={400} unoptimized className="mx-auto max-h-72 w-full object-contain"/>:playable&&revision?<Image src={url+'/assets/'+revision+'/preview.png'} alt={characterName} width={320} height={400} unoptimized className="mx-auto max-h-72 w-full object-contain"/>:<p className="p-4 text-sm">画像またはモデルパッケージを登録してください</p>;
 if(!characterId)return <p className="text-sm text-muted-foreground">キャラクターを保存すると設定できます。</p>;
 return <section className="min-w-0 space-y-3 rounded-lg border p-2 sm:p-4" aria-label="キャラクターアニメーション">
 <div className="flex justify-between"><h3 className="text-sm font-semibold">アニメーション</h3><span className="text-xs">{data?.active_revision?(buildFormat(data.builds.find(b=>b.id===data.active_revision))==='seethrough-rig'?'モデル使用中':'See-through Rigモデル未設定'):'静止画'}</span></div>
 <RigProductionSettings onChange={()=>void mutate()}/>
 <div className="inline-flex items-center gap-1"><Label htmlFor={'rig-package-'+characterId}>モデルパッケージを取り込む</Label><HelpPopover label="モデルパッケージを取り込む">See-through Rig の ZIPです。取込後にプレビューし、使用するモデルを選びます。</HelpPopover></div><Input id={'rig-package-'+characterId} type="file" accept=".zip,application/zip" disabled={busy} onChange={e=>{const file=e.target.files?.[0];if(file){const body=new FormData();body.append('file',file);void submit('/import',{method:'POST',body});}e.target.value='';}}/>
 <div className="min-w-0 rounded bg-muted/30 sm:p-2">{revision&&playable?<PlaybackSettingsPreview characterId={characterId} revision={revision} canvas={{modelFormat:selected?.model_format,url:url+'/assets/'+revision,character:characterName,state:motion,expression:previewJitome?'neutral':expression,previewJitome,focus,paused,cue,onCapabilities:value=>setReported({id:revision,caps:value})}}/>:revision?<RigCanvas modelFormat={selected?.model_format} url={url+'/assets/'+revision} character={characterName} fallback={fallback}/>:fallback}</div>
 {revision&&playable&&<><CharacterVoicePreview characterName={characterName} onPlaybackStart={()=>setPaused(false)}/><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="ghost" onClick={()=>setPaused(!paused)}>{paused?'動きを再開':'動きを停止'}</Button>{views.includes('face')&&<Button type="button" size="sm" variant="ghost" onClick={()=>setFocus(focus==='full'?'face':'full')}>{focus==='full'?'顔を拡大':'全身を表示'}</Button>}{expressions.length>0&&<Button type="button" size="sm" variant="outline" onClick={()=>{setPaused(false);setExpression('neutral');setMotion('idle');setCue({id:crypto.randomUUID(),kind:'reply'});}}>返答の反応を確認</Button>}</div>{expressionChoices.length>0&&<AppSelect aria-label="表情プレビュー" value={expression} onChange={e=>{setExpression(e.target.value);setCue(undefined);setPaused(false);}}>{expressionChoices.map(id=><option key={id} value={id}>{expressionLabel[id]??id}</option>)}</AppSelect>}{caps.conversation===true&&<AppSelect aria-label="会話プレビュー" value={motion} onChange={e=>setMotion(e.target.value as ConversationState)}><option value="idle">待機</option><option value="listening">聞き取り</option><option value="thinking">考え中</option></AppSelect>}</>}
 <Label htmlFor={'rig-image-'+characterId}>制作に使う画像</Label><Input id={'rig-image-'+characterId} type="file" accept="image/png,image/jpeg,image/webp" disabled={busy||Boolean(building)} onChange={e=>{const file=e.target.files?.[0];if(file){const body=new FormData();body.append('file',file);void submit('/source',{method:'POST',body});}e.target.value='';}}/>
 <div className="flex flex-wrap gap-2"><Button type="button" size="sm" disabled={busy||Boolean(building)||!data?.has_source||!data.production_enabled||!data.provider_ready} onClick={()=>void submit('/builds',{method:'POST'})}>画像からモデルを制作</Button>{selected&&isProductionBuild(selected)&&data?.production_enabled&&<Button type="button" size="sm" variant="outline" disabled={busy||Boolean(building)} onClick={()=>void submit('/builds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:selected.id})})}>この候補から再制作</Button>}{data?.active_revision&&data.has_source&&<Button type="button" size="sm" variant="outline" disabled={busy} onClick={()=>void activate(null)}>静止画に戻す</Button>}{buildFormat(previousBuild)==='seethrough-rig'&&<Button type="button" size="sm" variant="outline" disabled={busy||previous===data?.active_revision} onClick={()=>void activate(typeof previous==='string'?previous:null)}>前のモデルに戻す</Button>}</div>
 {latest&&<p role="status" className="text-sm">{labels[latest.status]??latest.status}{latest.error?'：'+latest.error:''}</p>}
 {data?.builds.map(build=>{
  const rowFormat=buildFormat(build),previewFile=buildPreview(build),canPlay=build.status==='ready'&&rowFormat==='seethrough-rig';
  const thumb=build.status==='ready'?(previewFile?url+'/assets/'+build.id+'/'+previewFile:data?.has_source?url+'/source.png':null):null;
  return <article key={build.id} className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs">{thumb&&<Image src={thumb} alt={build.display_name||'モデル '+build.revision} width={56} height={64} loading="lazy" unoptimized className="h-16 w-14 object-contain"/>}<div className="flex-1"><p>{build.display_name||characterName+' モデル'} · v{build.revision}{data.active_revision===build.id?' · 使用中':''}</p><p>{buildSource(build)} · {rowFormat} · {labels[build.status]??build.status}{rowFormat==='seethrough-rig'?'':' · 再生できません'}</p></div>{canPlay&&<><Button type="button" size="sm" variant="ghost" disabled={busy} onClick={()=>{setPreview(build.id);setExpression('neutral');setCue(undefined);}}>プレビュー</Button><Button type="button" size="sm" variant="outline" disabled={busy||preview!==build.id||data.active_revision===build.id} onClick={()=>void activate(build.id)}>確認して使用</Button></>}{!terminalBuild(build.status)&&<Button type="button" size="sm" variant="outline" disabled={busy} onClick={()=>void submit('/builds/'+build.id+'/cancel',{method:'POST'})}>制作を中止</Button>}{['failed','cancelled','canceled','needs_input'].includes(build.status)&&isProductionBuild(build)&&data.production_enabled&&<Button type="button" size="sm" variant="outline" disabled={busy} onClick={()=>void submit('/builds/'+build.id+'/resume',{method:'POST'})}>制作を再開</Button>}{isProductionBuild(build)&&data.production_enabled&&<Button type="button" size="sm" variant="ghost" disabled={busy} onClick={()=>void submit('/builds/'+build.id+'/edit',{method:'POST'}).then(result=>{if(result?.url){if(result.url.startsWith('/settings/rig-editor/'))window.open(withBasePath(result.url),'_blank','noopener,noreferrer');else setError('編集画面のURLが不正です');}})}>制作アプリで再編集</Button>}{terminalBuild(build.status)&&<Button type="button" size="sm" variant="destructive" disabled={busy} onClick={()=>{setError('');setDeleteTarget(build.id);}}>削除</Button>}</article>;
 })}
 <Dialog open={Boolean(deletingBuild)} onOpenChange={open=>{if(!open&&!requestPending.current)setDeleteTarget(null);}}>
  <DialogContent showCloseButton={false}>
   <DialogHeader><DialogTitle>モデルを削除しますか？</DialogTitle><DialogDescription>「{deletingBuild?.display_name||characterName+' モデル'} · v{deletingBuild?.revision}」を一覧から削除します。モデルファイルは保持され、キャラクターやチャット履歴は削除されません。{deleteTarget===data?.active_revision?'使用中のモデルのため、削除後は静止画表示に戻ります（画像未登録の場合は未設定表示）。':''}</DialogDescription></DialogHeader>
   {error&&<p role="alert" className="text-sm text-destructive">{error}</p>}
   <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={()=>{if(!requestPending.current)setDeleteTarget(null);}}>キャンセル</Button><Button type="button" variant="destructive" disabled={busy||!deletingBuild||!terminalBuild(deletingBuild.status)} onClick={()=>void deleteBuild()}>{busy?'削除中…':'削除する'}</Button></DialogFooter>
  </DialogContent>
 </Dialog>
 {((error&&!deletingBuild)||loadError)&&<p role="alert" className="text-sm text-destructive">{error||'設定を取得できません'}</p>}
 </section>;
}
