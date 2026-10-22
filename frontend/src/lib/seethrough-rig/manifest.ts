import type { RuntimeProfile } from '@aoitalk/seethrough-rig-runtime';
export type ImportedManifest = {
 schema_version:number; source_sha256?:string;project_id?:string;model_format:string; model_format_version:number;
 runtime:{abi:number;version:string;core_ref?:string;requires?:string[]};provenance?:{commit?:string;core_ref?:string};model:string;settings:string|null;preview:string;
 profile:RuntimeProfile;capabilities:Record<string,unknown>;
 files:{path:string;sha256:string;size:number}[];
};
export function parseManifest(value:unknown):ImportedManifest {
 const m=value as ImportedManifest;
 if(!m||m.schema_version!==1||m.model_format!=='seethrough-rig'||m.model_format_version!==1||m.runtime?.abi!==1||!['0.1.0','0.2.0','0.3.0','0.4.0'].includes(m.runtime.version))throw new Error('このモデルのランタイム形式は未対応です');
 const requirements=m.runtime.requires===undefined?[]:m.runtime.requires;
 const expected=m.runtime.version==='0.4.0'?'continuous-eyes-v3':m.runtime.version==='0.3.0'?'continuous-eyes-v2':m.runtime.version==='0.2.0'?'continuous-eyes-v1':undefined;
 if(!Array.isArray(requirements)||requirements.length!==(expected?1:0)||(expected&&requirements[0]!==expected))throw new Error('このモデルのランタイム機能は未対応です');
 if(requirements.length&&(m.runtime.core_ref||m.provenance?.core_ref||m.provenance?.commit==='46c0037779fb5e59715030925d66abdc6c09ca12'))throw new Error('目の連続変形は現行の再生エンジンでのみ使えます');
 if(!Array.isArray(m.files)||!m.profile||typeof m.profile!=='object')throw new Error('モデルの情報が不正です');
 const names=new Set(m.files.map(f=>f.path));
 for(const name of [m.model,m.preview,...(m.settings?[m.settings]:[])])if(typeof name!=='string'||!/^[-a-zA-Z0-9_.]+$/.test(name)||name.includes('..')||!names.has(name))throw new Error('モデルのファイル参照が不正です');
 return m;
}
export async function fetchManifest(base:string, signal:AbortSignal):Promise<ImportedManifest>{
 const response=await fetch(base+'/manifest.json',{credentials:'include',cache:'no-store',signal});
 if(!response.ok)throw new Error('モデルの情報を取得できません');
 return parseManifest(await response.json());
}
