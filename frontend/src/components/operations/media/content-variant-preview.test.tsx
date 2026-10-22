// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MediaContentVariantPanel } from '@/components/operations/media/content-variant-panel';
import { mediaContentApi } from '@/lib/media-operations-content-api';
vi.mock('@/lib/media-operations-content-api',()=>({mediaContentApi:{listVariants:vi.fn(),getVariant:vi.fn(),listRevisions:vi.fn(),getReadiness:vi.fn(),createVariant:vi.fn(),appendRevision:vi.fn(),recordQa:vi.fn(),recordRights:vi.fn()}}));
const cases = [
 ['YouTube / Long-form',{type:'youtube_video',tags:[],title:'Long form saved title',description:'Saved body',media_asset:['a'],visibility:'private'},1],
 ['YouTube / Shorts',{type:'youtube_short',tags:[],title:'Short saved title',description:'Saved body',media_asset:['a'],visibility:'unlisted'},1],
 ['Instagram / Feed image',{type:'instagram_feed',caption:'Saved caption',media:['a']},1],
 ['Instagram / Carousel',{type:'instagram_carousel',caption:'Saved caption',media:['a','b']},2],
 ['Instagram / Reel',{type:'instagram_reel',caption:'Saved caption',media:['a'],cover:['b']},1],
 ['TikTok / 動画',{type:'tiktok_video',caption:'Saved caption',media_asset:['a'],privacy:'SELF_ONLY',music_rights:'review_required',ai_disclosure:false},1],
 ['FANZA / 販売用パッケージ',{type:'fanza_release',title:'Saved product',description:'Saved body',preview_assets:['a','b'],price:100,sales:{currency:'JPY'},storefront:'commercial',submission_state:'draft',ai_usage:'partial'},2],
 ['DLsite / 販売用パッケージ',{type:'dlsite_release',title:'Saved product',description:'Saved body',preview_assets:['a'],price:100,sales:{currency:'JPY'}},1],
 ['X / 単独投稿',{type:'x_post',text:'Saved text'},0],
 ['pixiv / 作品',{type:'pixiv_work',title:'Saved title',caption:'Saved caption',media:['a']},1],
 ['Patreon / 投稿',{type:'patreon_post',title:'Saved title',body:'Saved body',audience:'paid'},0],
] as const;
function fixture(payload:unknown){const platform=String((payload as {type:string}).type).split('_')[0];const revision={id:'r1',content_variant_id:'v1',version:2,content_item_id:'c1',content_item_hash:'a'.repeat(64),persona_revision_id:'p1',persona_revision_hash:'b'.repeat(64),platform_account_id:'a1',platform,payload,generation_output_refs:[],source_evidence:[],content_hash:'c'.repeat(64)};return {id:'v1',content_item_id:'c1',content_item_hash:'a'.repeat(64),platform,status:'draft',current_revision:revision};}
async function show(payload:unknown){const v=fixture(payload);vi.mocked(mediaContentApi.listVariants).mockResolvedValue([v] as never);vi.mocked(mediaContentApi.getVariant).mockResolvedValue(v as never);vi.mocked(mediaContentApi.listRevisions).mockResolvedValue([v.current_revision] as never);render(<MediaContentVariantPanel/>);return await screen.findByRole('region',{name:'保存済みコンテンツのプレビュー'});}
beforeEach(()=>{vi.clearAllMocks();vi.stubGlobal('fetch',vi.fn(()=>Promise.reject(new Error('No network in this contract fixture'))));vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ready:false,status:'blocked',blocking_reasons:['qa_missing'],revision_id:'r1'} as never);});
describe('Saved payload format preview',()=>{
 it.each(cases)('shows %s from saved payload, not new form',async(label,payload,count)=>{const preview=await show(payload);expect(within(preview).getByText(label)).toBeInTheDocument();expect(preview).toHaveTextContent('保存済み v2');expect(preview).toHaveTextContent(`素材: ${count}件`);expect(fetch).not.toHaveBeenCalled();});
 it('shows ordered saved thread text',async()=>{const preview=await show({type:'x_thread',posts:[{text:'first saved post'},{text:'second saved post'}]});expect(within(preview).getAllByRole('listitem').map(x=>x.textContent)).toEqual(['first saved post','second saved post']);expect(preview).toHaveTextContent('X / スレッド');});
 it('distinguishes package draft and review preparation without a publication claim',async()=>{const preview=await show({type:'fanza_release',title:'Product',description:'Body',preview_assets:['a'],price:0,sales:{currency:'JPY'},storefront:'doujin',submission_state:'ready_for_review',ai_usage:'unknown'});expect(preview).toHaveTextContent('審査提出の準備');expect(preview).toHaveTextContent('同人');expect(preview).toHaveTextContent('審査・発売・販売の結果はこの下書きから確定しません');expect(preview).not.toHaveTextContent('公開済み');});
 it('renders untrusted body literally without fetching links or assets',async()=>{const body='<img src="https://untrusted.example/asset" onerror="alert(1)">';const preview=await show({type:'youtube_video',tags:[],title:'https://untrusted.example/title',description:body,media_asset:['opaque-reference'],visibility:'private'});expect(preview).toHaveTextContent(body);expect(preview.querySelector('img,video,iframe,script,a')).toBeNull();expect(fetch).not.toHaveBeenCalled();await waitFor(()=>expect(mediaContentApi.createVariant).not.toHaveBeenCalled());});
});
