import type {components} from '../../types/api-types.gen';
import {fetchApiAtServerFingerprint,getBaseUrl,getConfiguredApiServerFingerprint,isApiConnectionError} from '../../lib/api-client';
import {sendWithTransportAuth} from '../../lib/transport-auth';
import {nativeMultipartUpload} from '../../lib/native-multipart';
import {getTokenSnapshot,getTokenAuthScope} from '../../lib/auth';
import * as FileSystem from 'expo-file-system/legacy';
import * as Crypto from 'expo-crypto';
export type RigBuild = components['schemas']['BuildView'];
export type CharacterRig = components['schemas']['RigView'];
export type ProductionSettings = components['schemas']['ProductionSettingsView'];
export type RigScope={server:string;authScope:string};
export const rigPath=(character:string)=>'/api/characters/manage/'+encodeURIComponent(character)+'/rig';
export const isProductionBuild=(build:RigBuild)=>build.has_production_project===true;
/** Missing format stays legacy-unavailable. Device playback is seethrough-rig only. */
export const rigModelFormat=(format?:string|null)=>format?format:'legacy-unavailable';
export const isPlayableRig=(build:{model_format?:string|null})=>build.model_format==='seethrough-rig';
export const buildSource=(build:RigBuild)=>isPlayableRig(build)?(isProductionBuild(build)?'制作プロジェクト':'取込パッケージ'):'再生できません';
export const buildPreview=(build:RigBuild)=>isPlayableRig(build)?'preview.png':null;
export function activeRigFormat(rig:{active_revision?:string|null;active_format?:string|null;builds:readonly {id:string;model_format?:string|null}[]}){
 const selected=rig.active_revision?rig.builds.find(build=>build.id===rig.active_revision):undefined;
 return rigModelFormat(selected?selected.model_format:rig.active_format);
}
export const terminalBuild=(status:string)=>['ready','failed','cancelled','canceled','needs_input','unsupported'].includes(status);
export async function captureRigScope():Promise<RigScope>{const server=await getConfiguredApiServerFingerprint(),snapshot=await getTokenSnapshot();if(!snapshot.token)throw new Error('モデルの取得にはログインが必要です');return {server,authScope:getTokenAuthScope(snapshot.token)};}
export async function assertRigScope(scope:RigScope,signal?:AbortSignal){if(signal?.aborted)throw new Error('モデルの読込を中止しました');const [server,snapshot]=await Promise.all([getConfiguredApiServerFingerprint(),getTokenSnapshot()]);if(server!==scope.server||getTokenAuthScope(snapshot.token)!==scope.authScope)throw new Error('接続先またはログインユーザーが変更されました');if(signal?.aborted)throw new Error('モデルの読込を中止しました');}
export async function rigCacheRoot(scope:RigScope){if(!FileSystem.documentDirectory)throw new Error('モデルの保存先を利用できません');const key=await Crypto.digestStringAsync(Crypto.CryptoDigestAlgorithm.SHA256,scope.server+'\n'+scope.authScope);return FileSystem.documentDirectory+'character-rigs/'+key+'/';}
export async function rigRequest<T>(scope:RigScope,path:string,init:RequestInit={},timeout=30000):Promise<T>{await assertRigScope(scope,init.signal??undefined);const value=await fetchApiAtServerFingerprint<T>(scope.server,path,init,timeout);await assertRigScope(scope,init.signal??undefined);return value;}
export async function readCharacterRig(scope:RigScope,character:string,signal:AbortSignal):Promise<CharacterRig>{
 const directory=await rigCacheRoot(scope),path=directory+encodeURIComponent(character)+'.json';await assertRigScope(scope,signal);
 try{const value=await rigRequest<CharacterRig>(scope,rigPath(character),{signal});await FileSystem.makeDirectoryAsync(directory,{intermediates:true});await FileSystem.writeAsStringAsync(path,JSON.stringify(value));await assertRigScope(scope,signal);return value;}
 catch(error){if(!isApiConnectionError(error))throw error;await assertRigScope(scope,signal);const cached=JSON.parse(await FileSystem.readAsStringAsync(path)) as CharacterRig;await assertRigScope(scope,signal);return cached;}
}
/** Multipart uses the guarded native streaming bridge (or the iOS File fallback). */
export async function uploadRigFile(scope:RigScope,character:string,kind:'import'|'source',file:{uri:string;name:string;mimeType?:string},signal:AbortSignal):Promise<RigBuild|CharacterRig>{
 await assertRigScope(scope,signal);const endpoint=await getBaseUrl();await assertRigScope(scope,signal);
 const url=endpoint+rigPath(character)+'/'+kind;const response=await sendWithTransportAuth(url,{primaryBaseUrl:scope.server,targetBaseUrl:endpoint},(headers)=>nativeMultipartUpload({url,headers,signal,file:{uri:file.uri,name:file.name,mimeType:file.mimeType??(kind==='import'?'application/zip':'image/png')}}));await assertRigScope(scope,signal);
 if(!response.ok){const failure=await response.json<Record<string, unknown>>().catch(()=>({} as Record<string, unknown>));throw new Error(typeof failure.detail==='string'?failure.detail:'ファイルの取込に失敗しました（'+response.status+'）');}
 return response.json<RigBuild|CharacterRig>();
}
