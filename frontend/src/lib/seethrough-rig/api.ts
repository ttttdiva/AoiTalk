import type { components } from '@/lib/api-types.gen';
import { withBasePath } from '@/lib/base-path';
export type BuildView = components['schemas']['BuildView'];
export type RigView = components['schemas']['RigView'];
export type ProductionSettings = components['schemas']['ProductionSettingsView'];
export const productionUrl = withBasePath('/api/python-proxy/rig-production');
export const rigUrl = (character:string) => withBasePath('/api/python-proxy/characters/manage/'+encodeURIComponent(character)+'/rig');
export async function rigRequest<T>(url:string, init:RequestInit = {}):Promise<T> {
 const response=await fetch(url,{credentials:'include',cache:'no-store',...init});
 if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(typeof body.detail==='string'?body.detail:'操作に失敗しました（'+response.status+'）');}
 return response.json();
}
export const rigFetch=(url:string)=>rigRequest<RigView>(url);
export const terminalBuild=(status:string)=>['ready','failed','cancelled','canceled','needs_input','unsupported'].includes(status);
export const buildFormat=(build?:{model_format?:string|null}|null)=>{const format=build?.model_format;return typeof format==='string'&&format.trim()?format:'legacy-unavailable';};
export const isProductionBuild=(build:BuildView)=>build.has_production_project===true;
export const buildSource=(build:BuildView)=>{const format=buildFormat(build);return format==='aoirig'||format==='legacy-unavailable'?'再生できないモデル':isProductionBuild(build)?'制作プロジェクト':'取込パッケージ';};
export const buildPreview=(build:BuildView)=>buildFormat(build)==='seethrough-rig'?'preview.png':null;
