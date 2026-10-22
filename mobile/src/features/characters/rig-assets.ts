import * as FileSystem from 'expo-file-system/legacy';
import {File} from 'expo-file-system';
import * as Crypto from 'expo-crypto';
import {manipulateAsync,SaveFormat} from 'expo-image-manipulator';
import type {RuntimeProfile} from '@aoitalk/seethrough-rig-runtime';
import {getBaseUrl,isApiConnectionError,getCredentialIdentityRevision} from '../../lib/api-client';
import {sendWithTransportAuth} from '../../lib/transport-auth';
import {cacheProtectedMediaResource} from '../../lib/media-cache';
import {assertRigScope,rigCacheRoot,rigPath,rigRequest,type RigScope} from './rig-api';
export type RigManifest={schema_version:number;model_format:string;model_format_version:number;runtime:{abi:number;version:string;core_ref?:string;requires?:string[]};provenance?:{commit?:string;core_ref?:string};model:string;preview:string;settings:string|null;profile:RuntimeProfile;capabilities:Record<string,unknown>;files:{path:string;size:number;sha256:string}[]};
export function validateRigManifest(value:unknown):RigManifest{
 const m=value as RigManifest;if(!m||m.schema_version!==1||m.model_format!=='seethrough-rig'||m.model_format_version!==1||m.runtime?.abi!==1||!['0.1.0','0.2.0','0.3.0','0.4.0'].includes(m.runtime.version))throw new Error('このモデルのランタイム形式は未対応です');
 const requirements=m.runtime.requires===undefined?[]:m.runtime.requires;
 const expected=m.runtime.version==='0.4.0'?'continuous-eyes-v3':m.runtime.version==='0.3.0'?'continuous-eyes-v2':m.runtime.version==='0.2.0'?'continuous-eyes-v1':undefined;
 if(!Array.isArray(requirements)||requirements.length!==(expected?1:0)||(expected&&requirements[0]!==expected))throw new Error('このモデルのランタイム機能は未対応です');
 if(requirements.length&&(m.runtime.core_ref||m.provenance?.core_ref||m.provenance?.commit==='46c0037779fb5e59715030925d66abdc6c09ca12'))throw new Error('目の連続変形は現行の再生エンジンでのみ使えます');
 if(!Array.isArray(m.files)||!m.profile||typeof m.profile!=='object'||m.files.length>128)throw new Error('モデルの情報が不正です');
 const names=new Set<string>();for(const f of m.files){if(!f||!/^[-a-zA-Z0-9_.]+$/.test(f.path)||f.path.includes('..')||names.has(f.path)||!Number.isSafeInteger(f.size)||f.size<0||f.size>64*1024*1024||!/^[a-f0-9]{64}$/i.test(f.sha256))throw new Error('モデルのファイル情報が不正です');names.add(f.path);}
 for(const name of [m.model,m.preview,...(m.settings?[m.settings]:[])])if(!names.has(name))throw new Error('モデルのファイル参照が不正です');return m;
}
/** Immutable files are scoped by configured server, authenticated owner, character and revision. */
export async function loadRigAssets(scope:RigScope,character:string,revision:string,signal:AbortSignal){
 await assertRigScope(scope,signal);const root=await rigCacheRoot(scope),directory=root+encodeURIComponent(character)+'/'+encodeURIComponent(revision)+'/',manifestPath=directory+'manifest.json',path=rigPath(character)+'/assets/'+encodeURIComponent(revision);
 await FileSystem.makeDirectoryAsync(directory,{intermediates:true});let manifest:RigManifest;
 try{manifest=validateRigManifest(await rigRequest(scope,path+'/manifest.json',{signal}));await FileSystem.writeAsStringAsync(manifestPath,JSON.stringify(manifest));}
 catch(error){if(!isApiConnectionError(error))throw error;await assertRigScope(scope,signal);manifest=validateRigManifest(JSON.parse(await FileSystem.readAsStringAsync(manifestPath)));}
 await assertRigScope(scope,signal);
 // Resolve routing only for network acquisition; cached playback also works offline.
 let remote:string|null=null;const assetBase='https://rig-assets.invalid/'+encodeURIComponent(revision);
 const urlName=(src:string)=>{const prefix=assetBase+'/';if(!src.startsWith(prefix))throw new Error('外部のモデル素材は読み込めません');const name=src.slice(prefix.length);if(!manifest.files.some(f=>f.path===name))throw new Error('未登録のモデル素材です');return name;};
 async function verified(name:string){
  await assertRigScope(scope,signal);const metadata=manifest.files.find(f=>f.path===name);if(!metadata)throw new Error('未登録のモデル素材です');const local=directory+name;
  const valid=async(uri:string)=>{const info=await FileSystem.getInfoAsync(uri);if(!info.exists||!('size' in info)||info.size!==metadata.size)return false;const digest=await Crypto.digest(Crypto.CryptoDigestAlgorithm.SHA256,await new File(uri).bytes());return Array.from(new Uint8Array(digest),n=>n.toString(16).padStart(2,'0')).join('')===metadata.sha256.toLowerCase();};
  if(await valid(local)){await assertRigScope(scope,signal);return local;}
  if(!remote){remote=await getBaseUrl();await assertRigScope(scope,signal);}
  await assertRigScope(scope,signal);const temporary=local+'.'+Crypto.randomUUID()+'.partial';const url=remote+path+'/'+name;
  try{
    await cacheProtectedMediaResource(
     url,
     temporary,
     {},
     signal,
     {
      revisionReader:getCredentialIdentityRevision,
       request:async(send,requestSignal)=>await sendWithTransportAuth(url,{primaryBaseUrl:scope.server,targetBaseUrl:remote!,signal:requestSignal??signal},send),
     },
    );
   await assertRigScope(scope,signal);if(!await valid(temporary))throw new Error('モデル素材の整合性を確認できません');await assertRigScope(scope,signal);await FileSystem.moveAsync({from:temporary,to:local});return local;
  }
  finally{await FileSystem.deleteAsync(temporary,{idempotent:true}).catch(()=>undefined);}
 }
 const localPreview=await verified(manifest.preview);
 async function nativeTexture(name:string){
  const source=await verified(name);
  // Expo GL's native texImage2D file loader is backed by stb_image, which does
  // not decode WebP. Keep the immutable package/model untouched and derive a
  // PNG only inside this owner/server/revision-scoped device cache.
  if(!/\.webp$/i.test(name))return source;
  const metadata=manifest.files.find(f=>f.path===name);if(!metadata)throw new Error('未登録のモデル素材です');
  const derived=directory+'gl-'+metadata.sha256.toLowerCase()+'.png';
  const nonempty=async(uri:string)=>{const info=await FileSystem.getInfoAsync(uri);return Boolean(info.exists&&'size' in info&&info.size>0);};
  if(await nonempty(derived)){await assertRigScope(scope,signal);return derived;}
  await FileSystem.deleteAsync(derived,{idempotent:true}).catch(()=>undefined);
  let convertedUri='',published=false;const temporary=derived+'.'+Crypto.randomUUID()+'.partial';
  try{
   const converted=await manipulateAsync(source,[],{format:SaveFormat.PNG});
   convertedUri=converted?.uri??'';
   if(!convertedUri||convertedUri===source)throw new Error('WebPをPNGへ変換できません');
   await assertRigScope(scope,signal);
   await FileSystem.copyAsync({from:convertedUri,to:temporary});
   if(!await nonempty(temporary))throw new Error('WebPをPNGへ変換できません');
   await assertRigScope(scope,signal);
   await FileSystem.moveAsync({from:temporary,to:derived});
   if(!await nonempty(derived))throw new Error('WebPをPNGへ変換できません');
   published=true;await assertRigScope(scope,signal);return derived;
  }catch(error){
   await FileSystem.deleteAsync(temporary,{idempotent:true}).catch(()=>undefined);
   if(!published)await FileSystem.deleteAsync(derived,{idempotent:true}).catch(()=>undefined);
   throw error instanceof Error?error:new Error('WebPをPNGへ変換できません');
  }finally{if(convertedUri&&convertedUri!==source)await FileSystem.deleteAsync(convertedUri,{idempotent:true}).catch(()=>undefined);}
 }
 const cachedFetch:typeof fetch=async(input,init)=>{if(init?.signal?.aborted)throw new Error('読込を中止しました');const uri=await verified(urlName(String(input)));const text=await FileSystem.readAsStringAsync(uri);await assertRigScope(scope,signal);return new Response(text,{status:200,headers:{'Content-Type':'application/json'}});};
 const loadImage=async(src:string,imageSignal?:AbortSignal)=>{if(imageSignal?.aborted)throw new Error('読込を中止しました');const localUri=await nativeTexture(urlName(src));await assertRigScope(scope,signal);return {localUri};};
 return {manifest,modelUrl:assetBase+'/'+manifest.model,settingsUrl:manifest.settings?assetBase+'/'+manifest.settings:undefined,fetch:cachedFetch,loadImage,localPreview};
}
