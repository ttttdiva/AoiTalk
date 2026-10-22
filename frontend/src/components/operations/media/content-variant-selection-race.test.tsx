// @vitest-environment jsdom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { MediaContentVariantPanel } from '@/components/operations/media/content-variant-panel';
import { mediaContentApi } from '@/lib/media-operations-content-api';
vi.mock('@/lib/media-operations-content-api',()=>({mediaContentApi:{listVariants:vi.fn(),getVariant:vi.fn(),listRevisions:vi.fn(),getReadiness:vi.fn(),createVariant:vi.fn(),appendRevision:vi.fn(),recordQa:vi.fn(),recordRights:vi.fn()}}));
function fixture(id:string,platform:string,payload:unknown){const revision={id:'r-'+id,content_variant_id:id,version:1,content_item_id:'c1',content_item_hash:'a'.repeat(64),persona_revision_id:'p1',persona_revision_hash:'b'.repeat(64),platform_account_id:'a1',platform,payload,generation_output_refs:[],source_evidence:[],content_hash:'c'.repeat(64)};return {id,content_item_id:'c1',content_item_hash:'a'.repeat(64),platform,status:'draft',current_revision:revision};}
beforeEach(() => vi.resetAllMocks());
it('retains the latest selected variant when an earlier detail response finishes later',async()=>{
const first=fixture('a','x',{type:'x_post',text:'OLD SAVED A'}),second=fixture('b','youtube',{type:'youtube_video',tags:[],title:'LATEST SAVED B',description:'Second variant',media_asset:['a'],visibility:'private'});
let release!:(x:unknown)=>void;const delayed=new Promise(r=>{release=r;});
vi.mocked(mediaContentApi.listVariants).mockResolvedValue([first,second] as never);
vi.mocked(mediaContentApi.getVariant).mockImplementation(id=>(id==='a'?delayed:Promise.resolve(second)) as never);
vi.mocked(mediaContentApi.listRevisions).mockImplementation(id=>Promise.resolve([id==='a'?first.current_revision:second.current_revision]) as never);
vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ready:false,status:'blocked',blocking_reasons:['qa_missing'],revision_id:'r1'} as never);
render(<MediaContentVariantPanel/>);
await waitFor(()=>expect(mediaContentApi.getVariant).toHaveBeenCalledWith('a'));
await userEvent.click(await screen.findByRole('button',{name:/YouTube 下書き ContentItem/u}));
await screen.findByText('LATEST SAVED B');release(first);
await new Promise(r=>setTimeout(r,50));
expect(within(screen.getByRole('region',{name:'保存済みコンテンツのプレビュー'})).queryByText('OLD SAVED A')).not.toBeInTheDocument();
expect(within(screen.getByRole('region',{name:'保存済みコンテンツのプレビュー'})).getByText('LATEST SAVED B')).toBeInTheDocument();
});

it('ignores an old detail error after switching to another saved variant', async () => {
  const first=fixture('a','x',{type:'x_post',text:'OLD A'}), second=fixture('b','youtube',{type:'youtube_video',tags:[],title:'SAVED B',description:'B body',media_asset:['b'],visibility:'private'});
  let reject!: (error: Error) => void;
  const delayed = new Promise((_resolve, fail) => { reject = fail; });
  vi.mocked(mediaContentApi.listVariants).mockResolvedValue([first,second] as never);
  vi.mocked(mediaContentApi.getVariant).mockImplementation(id => (id === 'a' ? delayed : Promise.resolve(second)) as never);
  vi.mocked(mediaContentApi.listRevisions).mockImplementation(id => Promise.resolve([id === 'a' ? first.current_revision : second.current_revision]) as never);
  vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ready:false,status:'blocked',blocking_reasons:['qa_missing'],revision_id:'r1'} as never);
  render(<MediaContentVariantPanel/>);
  await waitFor(() => expect(mediaContentApi.getVariant).toHaveBeenCalledWith('a'));
  await userEvent.click(screen.getByRole('button',{name:/YouTube 下書き ContentItem/u}));
  await screen.findByText('SAVED B');
  await act(async () => reject(new Error('OLD A ERROR')));
  expect(screen.queryByText('OLD A ERROR')).not.toBeInTheDocument();
  expect(screen.getByRole('region',{name:'保存済みコンテンツのプレビュー'})).toHaveTextContent('SAVED B');
});

it('invalidates pending detail when the refreshed list removes the selected item', async () => {
  const first=fixture('a','x',{type:'x_post',text:'REMOVED A'});
  let release!: (value: unknown) => void;
  const delayed = new Promise(resolve => { release = resolve; });
  vi.mocked(mediaContentApi.listVariants).mockResolvedValueOnce([first] as never).mockResolvedValue([]);
  vi.mocked(mediaContentApi.getVariant).mockReturnValue(delayed as never);
  vi.mocked(mediaContentApi.listRevisions).mockResolvedValue([first.current_revision] as never);
  vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ready:false,status:'blocked',blocking_reasons:['qa_missing']} as never);
  render(<MediaContentVariantPanel/>);
  await waitFor(() => expect(mediaContentApi.getVariant).toHaveBeenCalledWith('a'));
  await userEvent.click(screen.getByRole('button',{name:'更新'}));
  await screen.findByText(/まだVariantがありません/u);
  await act(async () => release(first));
  expect(screen.queryByRole('region',{name:'保存済みコンテンツのプレビュー'})).not.toBeInTheDocument();
  expect(screen.queryByText('REMOVED A')).not.toBeInTheDocument();
});

it('settles pending detail safely after the panel is unmounted', async () => {
  const first=fixture('a','x',{type:'x_post',text:'UNMOUNTED A'});
  let reject!: (error: Error) => void;
  const delayed = new Promise((_resolve, fail) => { reject = fail; });
  vi.mocked(mediaContentApi.listVariants).mockResolvedValue([first] as never);
  vi.mocked(mediaContentApi.getVariant).mockReturnValue(delayed as never);
  vi.mocked(mediaContentApi.listRevisions).mockResolvedValue([first.current_revision] as never);
  vi.mocked(mediaContentApi.getReadiness).mockResolvedValue({ready:false,status:'blocked',blocking_reasons:['qa_missing']} as never);
  const view = render(<MediaContentVariantPanel/>);
  await waitFor(() => expect(mediaContentApi.getVariant).toHaveBeenCalledWith('a'));
  view.unmount();
  await act(async () => reject(new Error('UNMOUNTED REQUEST ERROR')));
  expect(screen.queryByRole('region',{name:'保存済みコンテンツのプレビュー'})).not.toBeInTheDocument();
});
