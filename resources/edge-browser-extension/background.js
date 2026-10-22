let nativePort=null, connected=false, connectionError='';
let selectedTabId=null;
let browserContextId='', browserContextEpoch='';
const CONTEXT_UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const browserContextReady=chrome.storage.local.get('mediaBrowserContextId').then(async value=>{
  const stored=value?.mediaBrowserContextId;
  browserContextId=typeof stored==='string'&&CONTEXT_UUID.test(stored)?stored:crypto.randomUUID();
  if(browserContextId!==stored)await chrome.storage.local.set({mediaBrowserContextId:browserContextId});
});
const browserContext=()=>({context_id:browserContextId,context_epoch:browserContextEpoch});
const contextValid=value=>value&&typeof value==='object'&&!Array.isArray(value)&&
  Object.keys(value).sort().join(',')==='context_epoch,context_id'&&
  ['context_id','context_epoch'].every(key=>typeof value[key]==='string'&&CONTEXT_UUID.test(value[key]));
const contextMatches=value=>contextValid(value)&&Boolean(nativePort&&connected)&&
  value.context_id===browserContextId&&value.context_epoch===browserContextEpoch;
function mediaContextChrome(guard){
  const call=async effect=>{
    if(!guard())throw Error('media_prepare_browser_context_changed');
    const result=await effect();
    if(!guard())throw Error('media_prepare_browser_context_changed');
    return result;
  };
  return {tabs:{get:id=>call(()=>chrome.tabs.get(id)),query:options=>call(()=>chrome.tabs.query(options))},
    scripting:{executeScript:request=>call(()=>chrome.scripting.executeScript(request))}};
}
function mediaContextError(command,params,unknown=false){
  const error='media_prepare_browser_context_changed',youtube=params?.platform==='youtube'||command.includes('youtube');
  if(command==='media_prepare_readiness')return {platform:youtube?'youtube':'x',account_ref:'',
    account_ref_kind:youtube?'youtube_channel_id':'x_handle',document_id:'',helper_revision:youtube?4:9,
    composer_ready:false,account_verified:false,available:false,executed:false,publication_confirmed:false,outcome_unknown:false,error};
  if(command.includes('_asset_')){
    const state=mediaAssetStates.get(params?.transfer_id),write=unknown&&command.endsWith('_finish');
    return {transfer_id:params?.transfer_id||'',scope_hash:params?.scope_hash||'',
      document_id:params?.expected_document_id||'',account_ref:params?.expected_account_ref||'',
      asset_sha256:state?.sha||params?.asset_sha256||'',offset:state?.offset||0,accepted:false,
      dispatched:write,files_verified:false,attachment_visible:false,executed:write,outcome_unknown:unknown,error};
  }
  if(youtube)return {platform:'youtube',account_ref:'',account_ref_kind:'youtube_channel_id',document_id:'',
    metadata_sha256:null,title_sha256:null,description_sha256:null,asset_sha256:null,composer_ready:false,
    final_confirmation_available:false,prepared:false,executed:unknown,outcome_unknown:unknown,error};
  return mediaError(error,unknown);
}
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const summary=tab=>({tab_id:tab.id,url:tab.url || '',title:tab.title || ''});
async function activateTab(tabId) {
  let tab = await chrome.tabs.get(tabId);
  // Edge Sleeping Tabs may report status=complete but reject/hang script
  // execution until their browser window and tab are active.
  try { await chrome.windows.update(tab.windowId, {focused: true}); } catch { /* best effort */ }
  tab = await chrome.tabs.update(tab.id, {active: true});
  await sleep(40);
  return tab;
}

async function ready(tabId) {
  const deadline=Date.now()+20000;
  while(Date.now()<deadline) {
    const tab=await chrome.tabs.get(tabId);
    if(tab.status==='complete') return tab;
    await sleep(100);
  }
  throw new Error('edge_page_load_timeout');
}
async function resolveTab({tab_id,url=''}) {
  let tab;
  if(Number.isInteger(tab_id)) {
    tab=await chrome.tabs.get(tab_id);
    if(url && tab.url!==url) tab=await chrome.tabs.update(tab.id,{url});
  } else if(url) {
    const tabs=await chrome.tabs.query({});
    tab=tabs.find(t=>(t.url || '').replace(/\/$/,'')===url.replace(/\/$/,''));
    if(!tab) tab=await chrome.tabs.create({url,active:true});
  } else if(Number.isInteger(selectedTabId)) {
    // Closing the explicitly selected tab is an error, not a change of target.
    tab=await chrome.tabs.get(selectedTabId);
  } else {
    [tab]=await chrome.tabs.query({active:true,lastFocusedWindow:true});
  }
  if(!tab) throw new Error('edge_no_tab');
  selectedTabId=tab.id;
  await chrome.storage.session.set({selectedTabId});
  return summary(await ready(tab.id));
}
const observedFrames = new Map();
const ownedMaterialGuard = typeof AOITALK_OWNED_MATERIAL_GUARD !== 'undefined' && AOITALK_OWNED_MATERIAL_GUARD === 1;
async function bounded(promise, timeoutMs, code) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(code)), timeoutMs); }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
async function observeScripts(tabId, action, allFrames) {
  const target = allFrames ? {tabId, allFrames: true} : {tabId};
  await bounded(
    chrome.scripting.executeScript({target, files: ['content.js']}),
    allFrames ? 1200 : 900,
    'edge_observation_injection_timeout',
  );
  return bounded(
    chrome.scripting.executeScript({target,
      func: (requested, identity) => globalThis.__aoitalkBrowser?.observe(requested, identity),
      args: [action, ownedMaterialGuard && !allFrames ? {tab_id: tabId} : null]}),
    allFrames ? 2600 : 2200,
    'edge_observation_script_timeout',
  );
}
async function inspectTopDocument(tabId) {
  return bounded(
    chrome.scripting.executeScript({target: {tabId}, func: () => {
      const started = performance.now();
      const candidates = {};
      let bytes = 0, eligible = 0, truncated = false;
      const links = document.querySelectorAll('a[href*="/public/jobs/"]');
      for (const e of links) {
        eligible++;
        if (eligible > 1200) { truncated = true; break; }
        const href = e.href || '';
        if (!href || !e.isConnected) continue;
        const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
        const label = clean(e.innerText || e.textContent || e.getAttribute('aria-label')).slice(0, 220);
        const container = e.closest('tr,[role="row"],li,[role="listitem"],article') || e.parentElement;
        const context = clean(container?.innerText || container?.textContent || label).slice(0, 2000);
        const item = {tag: 'a', type: '', role: e.getAttribute('role') || '', label,
          context, heading: '', stable_id: `inspect_${eligible}`, enabled: true, options: [], href};
        const encoded = JSON.stringify(item);
        const size = encoded.length * 3 + 64;
        if (Object.keys(candidates).length >= 1000 || bytes + size > 420000) { truncated = true; break; }
        candidates[`i_${eligible}`] = item;
        bytes += size;
      }
      const text = String(document.body?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 10000);
      return {url: location.href, title: document.title, text, candidates,
        document_id: `inspect_${Date.now()}`, observation_id: `inspect_${Date.now()}`, ready: Boolean(document.body),
        coverage: {scanned_nodes: links.length, candidate_count: Object.keys(candidates).length,
          omitted_count: Math.max(0, eligible - Object.keys(candidates).length), truncated,
          unscanned: eligible < links.length, shadow_scope: 'none'},
        observation_ms: Math.round(performance.now() - started), candidate_bytes: bytes};
    }}),
    2200,
    'edge_inspect_timeout',
  );
}
async function crowdworksInspect(params) {
  let tab = await chrome.tabs.get(params.tab_id);
  const url = new URL(tab.url || 'about:blank');
  if (!['crowdworks.jp', 'www.crowdworks.jp'].includes(url.hostname) || !url.pathname.startsWith('/public/jobs'))
    throw new Error('edge_crowdworks_scope_rejected');
  try { tab = await chrome.tabs.update(tab.id, {autoDiscardable:false}); } catch { /* best effort */ }
  const run = timeoutMs => bounded(chrome.scripting.executeScript({
    target: {tabId: tab.id},
    func: () => {
      const started = performance.now();
      const clean = (value, limit) => String(value || '').slice(0, limit).replace(/\s+/g, ' ').trim();
      const links = document.querySelectorAll('a[href*="/public/jobs/"]');
      const candidates = {};
      let count = 0, bytes = 0, truncated = false;
      for (const e of links) {
        if (count >= 500) { truncated = true; break; }
        const href = e.href || '';
        let parsed;
        try { parsed = new URL(href, location.href); } catch { continue; }
        if (!['crowdworks.jp', 'www.crowdworks.jp'].includes(parsed.hostname) || !/^\/public\/jobs\/\d+(?:\/|$)/.test(parsed.pathname)) continue;
        const label = clean(e.getAttribute('aria-label') || e.textContent, 1200).slice(0, 220);
        const container = e.closest('tr,[role="row"],li,[role="listitem"],article') || e.parentElement;
        const context = clean(container?.textContent || label, 5000).slice(0, 2000);
        const item = {tag:'a', type:'', role:e.getAttribute('role') || '', label, context, heading:'', href:parsed.href};
        const size = JSON.stringify(item).length + 64;
        if (bytes + size > 320000) { truncated = true; break; }
        candidates[`cw_${count++}`] = item;
        bytes += size;
      }
      return {
        url: location.href, title: document.title,
        text: clean(document.body?.textContent, 20000).slice(0, 10000),
        candidates, candidate_bytes: bytes,
        coverage: {scanned_nodes: links.length, candidate_count: count, omitted_count: Math.max(0, links.length-count), truncated, unscanned: truncated, shadow_scope:'none'},
        observation_ms: Math.round(performance.now() - started),
      };
    },
  }), timeoutMs, 'edge_crowdworks_inspect_timeout');
  let rows;
  try {
    // Normal path: never steal focus from the user's current application.
    rows = await run(1800);
  } catch {
    // Edge may suspend a background tab. Wake only as a last resort, then keep
    // the tab non-discardable so later scans normally stay on the quiet path.
    tab = await activateTab(tab.id);
    rows = await run(3500);
  }
  const result = rows?.[0]?.result;
  if (!result || typeof result !== 'object') throw new Error('edge_crowdworks_inspect_unavailable');
  return {...summary(tab), ...result};
}
async function observe(params) {
  const started = Date.now(), deadline = started + 6500;
  let tab, results;
  const action = params.action || 'read';
  // Marketplace bulk inspection only needs the top document. Avoid scanning
  // every third-party iframe on large result pages.
  const mainFrameOnly = action === 'inspect';
  while (Date.now() <= deadline) {
    tab = await chrome.tabs.get(params.tab_id);
    try {
      if (mainFrameOnly) {
        results = await inspectTopDocument(tab.id);
      } else {
        try {
          results = await observeScripts(tab.id, action, true);
        } catch {
          // A slow/uninjectable third-party frame must not wedge the whole
          // browser lane. The top document is still useful for most tasks.
          results = await observeScripts(tab.id, action, false);
        }
      }
      if (results.some(frame => frame.frameId === 0 && frame.result?.ready !== false && frame.result)) break;
      results = null;
    } catch { results = null; }
    if (Date.now() > deadline) break;
    await sleep(50);
  }
  if (!results) throw new Error('edge_observation_unavailable');
  const candidates = {}, material_states = {}, texts = [], frames = [];
  const coverage = {candidate_count: 0, omitted_count: 0, truncated: false, unscanned: false};
  let bytes = 0;
  for (const key of observedFrames.keys()) if (key.startsWith(`${tab.id}:`)) observedFrames.delete(key);
  for (const frame of results) {
    if (!frame.result) { coverage.truncated = true; continue; }
    const snapshot = frame.result;
    texts.push(snapshot.text || '');
    const binding = {document_id: frame.documentId, observation_id: snapshot.observation_id};
    observedFrames.set(`${tab.id}:${frame.frameId}`, binding);
    frames.push({frame_id: frame.frameId, ...binding, coverage: snapshot.coverage});
    coverage.truncated ||= Boolean(snapshot.coverage?.truncated);
    coverage.unscanned ||= Boolean(snapshot.coverage?.unscanned);
    coverage.omitted_count += snapshot.coverage?.omitted_count || 0;
    for (const [id, item] of Object.entries(snapshot.candidates || {})) {
      const candidate = {...item, frame_id: frame.frameId, document_id: frame.documentId || snapshot.document_id};
      const size = new TextEncoder().encode(JSON.stringify(candidate)).length + id.length + 32;
      if (coverage.candidate_count >= 4096 || bytes + size > 600000) {
        coverage.truncated = true; coverage.omitted_count++; continue;
      }
      candidates[`${frame.frameId}:${id}`] = candidate;
      if (ownedMaterialGuard && frame.frameId === 0 && snapshot.material_states?.[id])
        material_states[`0:${id}`] = {...snapshot.material_states[id], tab_id: tab.id,
          document_id: candidate.document_id};
      bytes += size; coverage.candidate_count++;
    }
  }
  return {...summary(tab), text: texts.join('\n').slice(0, 10000), candidates,
    coverage, frames, candidate_bytes: bytes, observation_ms: Date.now() - started,
    ...(ownedMaterialGuard ? {material_states} : {})};
}
async function act(params) {
  const separator = String(params.target).indexOf(':');
  const frameId = Number(String(params.target).slice(0, separator));
  const target = String(params.target).slice(separator + 1);
  const binding = observedFrames.get(`${params.tab_id}:${frameId}`);
  if (separator < 1 || !Number.isInteger(frameId) || !binding) {
    return {ok: false, error: 'edge_stale_element', executed: false, execution_status: 'blocked'};
  }
  let createdTabId = null, transitioned = false, notify;
  const effect = new Promise(resolve => { notify = resolve; });
  const onCreated = tab => {
    if (tab.openerTabId === params.tab_id) { createdTabId = tab.id; notify(); }
  };
  const onUpdated = (id, change) => {
    if (id === params.tab_id && (change.url || change.status === 'loading')) { transitioned = true; notify(); }
  };
  chrome.tabs.onCreated.addListener(onCreated);
  chrome.tabs.onUpdated.addListener(onUpdated);
  try {
    const scriptTarget = binding.document_id ? {tabId: params.tab_id, documentIds: [binding.document_id]} :
      {tabId: params.tab_id, frameIds: [frameId]};
    let results;
    try {
      results = await chrome.scripting.executeScript({target: scriptTarget,
        func: request => globalThis.__aoitalkBrowser?.act(request) ||
          {ok: false, error: 'edge_stale_element', executed: false, execution_status: 'blocked'},
        args: [{...params, target, material_identity: ownedMaterialGuard && frameId === 0 ?
          {tab_id: params.tab_id, document_id: binding.document_id} : null}]});
    } catch {
      // A destroyed document may mean navigation already happened. Never replay.
      return {ok: false, error: 'edge_action_outcome_unknown', executed: null, execution_status: 'unknown'};
    }
    const result = results[0]?.result;
    if (!result) return {ok: false, error: 'edge_action_outcome_unknown', executed: null, execution_status: 'unknown'};
    if (!result.ok) return result;
    if (params.action === 'click' && !result.effect_observed && !createdTabId && !transitioned)
      await Promise.race([effect, sleep(180)]);
    const nextId = createdTabId ?? params.tab_id;
    selectedTabId = nextId;
    await chrome.storage.session.set({selectedTabId});
    return {...summary(await chrome.tabs.get(nextId)), ...result};
  } finally {
    chrome.tabs.onCreated.removeListener(onCreated);
    chrome.tabs.onUpdated.removeListener(onUpdated);
  }
}
const mediaRuns = new Map(), mediaBusyTabs = new Set();
const mediaRunsReady = chrome.storage.session.get('mediaPrepareRuns').then(value => {
  const rows = value.mediaPrepareRuns;
  if (!Array.isArray(rows) || rows.length > 256) return;
  for (const row of rows) {
    if (!Array.isArray(row) || row.length !== 2) continue;
    const [key, run] = row;
    if (typeof key !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(key) || !run ||
        !Number.isInteger(run.tab) || typeof run.account !== 'string' || !/^@[a-z0-9_]{1,15}$/.test(run.account) ||
        typeof run.document !== 'string' || !run.document || run.document.length > 128 ||
        !Number.isFinite(run.expires) || typeof run.unknown !== 'boolean' || typeof run.fillStarted !== 'boolean') continue;
    mediaRuns.set(key, run);
  }
});
const persistMediaRuns = () => chrome.storage.session.set({mediaPrepareRuns:[...mediaRuns]});
const mediaAssetStates = new Map();
const mediaAssetsReady = chrome.storage.session.get('mediaPrepareAssetStates').then(value => {
  if (!Array.isArray(value.mediaPrepareAssetStates) || value.mediaPrepareAssetStates.length > 256) return;
  for (const row of value.mediaPrepareAssetStates) {
    if (!Array.isArray(row) || row.length !== 2 || typeof row[0] !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(row[0]) ||
        !row[1] || typeof row[1].pin !== 'string' || !Number.isInteger(row[1].offset) ||
        typeof row[1].dispatchStarted !== 'boolean' || typeof row[1].unknown !== 'boolean') continue;
    mediaAssetStates.set(row[0],row[1]);
  }
});
const persistMediaAssets = () => chrome.storage.session.set({mediaPrepareAssetStates:[...mediaAssetStates]});
const mediaYouTubeRuns = new Map();
const mediaYouTubeReady = chrome.storage.session.get('mediaPrepareYouTubeRuns').then(value => {
  if (!Array.isArray(value.mediaPrepareYouTubeRuns) || value.mediaPrepareYouTubeRuns.length > 128) return;
  for (const row of value.mediaPrepareYouTubeRuns) {
    if (!Array.isArray(row) || row.length !== 2 || typeof row[0] !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(row[0]) ||
        !row[1] || !Number.isInteger(row[1].tab) || typeof row[1].account !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(row[1].account) ||
        typeof row[1].document !== 'string' || !row[1].document || row[1].document.length > 128 ||
        typeof row[1].unknown !== 'boolean' || typeof row[1].fillStarted !== 'boolean') continue;
    mediaYouTubeRuns.set(row[0],row[1]);
  }
});
const persistMediaYouTubeRuns = () => chrome.storage.session.set({mediaPrepareYouTubeRuns:[...mediaYouTubeRuns]});
const mediaYouTubeTabAllowed = (tab, account) => {
  try {
    const parsed = new URL(tab.url || '');
    if (!/^UC[A-Za-z0-9_-]{22}$/.test(account) || parsed.protocol !== 'https:' ||
        parsed.hostname !== 'studio.youtube.com' || parsed.port || parsed.username || parsed.password || parsed.hash || parsed.search.length > 512 ||
        !new RegExp('^/channel/'+account+'(?:/(?:videos(?:/upload)?|dashboard))?$').test(parsed.pathname)) return false;
    const query = parsed.searchParams, keys = [...query.keys()];
    if (new Set(keys).size !== keys.length || keys.some(key => !['filter','sort','d','udvid'].includes(key)) ||
        keys.length && parsed.pathname !== '/channel/'+account+'/videos/upload' ||
        query.has('filter') !== query.has('sort') || query.has('d') !== query.has('udvid')) return false;
    if (query.has('filter')) {
      const filter = JSON.parse(query.get('filter')), sort = JSON.parse(query.get('sort'));
      if (!Array.isArray(filter) || filter.length || !sort || typeof sort !== 'object' || Array.isArray(sort) ||
          Object.keys(sort).sort().join(',') !== 'columnType,sortOrder' || sort.columnType !== 'date' ||
          sort.sortOrder !== 'DESCENDING') return false;
    }
    return !query.has('d') || query.get('d') === 'ud' && /^[A-Za-z0-9_-]{11}$/.test(query.get('udvid'));
  } catch { return false; }
};
const mediaError = (code, unknown = false) => ({platform:'x', account_ref:'', account_ref_kind:'x_handle',
  document_id:'', text_sha256:null, composer_ready:false, final_confirmation_available:false,
  prepared:false, executed:unknown, outcome_unknown:unknown, error:code});
const mediaTabAllowed = tab => {
  try {
    const parsed = new URL(tab.url || '');
    return parsed.protocol === 'https:' && parsed.hostname === 'x.com' && !parsed.port &&
      !parsed.username && !parsed.password && !parsed.search && !parsed.hash &&
      ['/home','/compose/post','/compose/tweet'].includes(parsed.pathname);
  } catch { return false; }
};
const mediaYouTubeOwnerTabAllowed = (tab,handle) => {
  try {
    const url = new URL(tab.url || '');
    return url.protocol === 'https:' && ['youtube.com','www.youtube.com'].includes(url.hostname) &&
      !url.port && !url.username && !url.password && !url.search && !url.hash &&
      decodeURIComponent(url.pathname) === '/'+handle;
  } catch { return false; }
};
async function mediaYouTubeIdentity(mediaBrowser,params,document) {
  let capturedToken = null, accepted = false;
  try {
    const studio = await mediaBrowser.tabs.get(params.tab_id);
    if (selectedTabId !== params.tab_id || !mediaYouTubeTabAllowed(studio,params.expected_account_ref)) return false;
    const target = {tabId:params.tab_id,documentIds:[document]};
    const inspected = await bounded(mediaBrowser.scripting.executeScript({target,
      func: account => globalThis.__aoitalkMediaPrepareYouTube?.identity?.({phase:'inspect',expected_account_ref:account}),
      args:[params.expected_account_ref]}),1800,'media_prepare_identity_timeout');
    const frame = inspected?.find(item => item.frameId === 0 && item.documentId === document), identity = frame?.result;
    if (!identity || identity.account_ref !== params.expected_account_ref) return false;
    if (identity.mode === 'visible') return selectedTabId === params.tab_id && Object.keys(identity).sort().join(',') === 'account_ref,mode';
    if (identity.mode === 'legacy') return selectedTabId === params.tab_id && Object.keys(identity).sort().join(',') === 'account_ref,mode,read_only' &&
      identity.read_only === true && [undefined,'readiness','inspect','verify'].includes(params.phase);
    if (identity.mode !== 'captured' || Object.keys(identity).sort().join(',') !==
        'account_ref,capture_token,display_name,handle,mode,owner_document_id,owner_tab_id' ||
        typeof identity.handle !== 'string' || !/^@[\p{L}\p{N}\p{M}_.-]{1,100}$/u.test(identity.handle) ||
        typeof identity.display_name !== 'string' || !identity.display_name || identity.display_name.length > 200 ||
        typeof identity.capture_token !== 'string' || !CONTEXT_UUID.test(identity.capture_token) ||
        !Number.isInteger(studio.windowId)) return false;
    capturedToken = identity.capture_token;
    const candidates = (await bounded(mediaBrowser.tabs.query({windowId:studio.windowId}),1800,'media_prepare_identity_timeout'))
      .filter(tab => tab.id !== params.tab_id && tab.windowId === studio.windowId && mediaYouTubeOwnerTabAllowed(tab,identity.handle));
    // No navigation, tab creation, account switch, or fallback to an unrelated page.
    if (candidates.length !== 1) return false;
    const owner = candidates[0];
    if (identity.owner_tab_id !== null && identity.owner_tab_id !== owner.id ||
        identity.owner_document_id !== null && (typeof identity.owner_document_id !== 'string' || !identity.owner_document_id)) return false;
    const ownerTarget = identity.owner_document_id ? {tabId:owner.id,documentIds:[identity.owner_document_id]}
      : {tabId:owner.id,frameIds:[0]};
    const owners = await bounded(mediaBrowser.scripting.executeScript({target:ownerTarget,func: expected => {
      const visible = node => Boolean(node?.isConnected && node.getClientRects().length &&
        getComputedStyle(node).visibility !== 'hidden' && (!node.checkVisibility ||
        node.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})));
      const one = (root,selector,predicate = visible) => {
        const nodes = [...root.querySelectorAll(selector)].filter(predicate);
        return nodes.length === 1 ? nodes[0] : null;
      };
      const url = new URL(location.href);
      if (window !== window.top || url.protocol !== 'https:' || !['youtube.com','www.youtube.com'].includes(url.hostname) ||
          url.port || url.username || url.password || url.search || url.hash || decodeURIComponent(url.pathname) !== '/'+expected.handle) return false;
      const header = one(document,'yt-page-header-renderer');
      if (!header) return false;
      const name = one(header,'h1.dynamicTextViewModelH1 > span.ytAttributedStringHost');
      const handle = one(header,'yt-content-metadata-view-model .ytContentMetadataViewModelMetadataText > span.ytAttributedStringLinkInheritColor',
        node => visible(node) && node.innerText.trim().startsWith('@'));
      if (!name || !handle || name.innerText.trim() !== expected.display_name || handle.innerText.trim() !== expected.handle) return false;
      const links = [];
      for (const [labels,path] of [[['動画を管理','Manage videos'],'content'],[['チャンネルをカスタマイズ','Customize channel'],'editing']]) {
        const link = one(header,'yt-flexible-actions-view-model button-view-model > a[href]',node =>
          visible(node) && labels.includes(node.getAttribute('aria-label')));
        if (!link) return false;
        const target = new URL(link.href);
        if (target.protocol !== 'https:' || target.hostname !== 'studio.youtube.com' || target.port || target.username ||
            target.password || target.search || target.hash || target.pathname !== '/channel/'+expected.account_ref+'/'+path) return false;
        links.push(link);
      }
      const current = {token:expected.capture_token,url:location.href,account:expected.account_ref,
        displayName:expected.display_name,ownerHandle:expected.handle,header,name,handle,
        manage:links[0],customize:links[1],manageHref:links[0].href,customizeHref:links[1].href};
      const saved = globalThis.__aoitalkYouTubeOwnerIdentity;
      if (saved?.token === current.token && Object.keys(current).some(key => current[key] !== saved[key])) return false;
      globalThis.__aoitalkYouTubeOwnerIdentity = current;
      return true;
    },args:[identity]}),1800,'media_prepare_identity_timeout');
    const proven = owners?.find(item => item.frameId === 0 && item.result === true);
    if (!proven?.documentId || identity.owner_document_id && identity.owner_document_id !== proven.documentId ||
        selectedTabId !== params.tab_id || !mediaYouTubeOwnerTabAllowed(await mediaBrowser.tabs.get(owner.id),identity.handle) ||
        (await mediaBrowser.tabs.get(params.tab_id)).url !== studio.url) return false;
    const bound = await bounded(mediaBrowser.scripting.executeScript({target,
      func: request => globalThis.__aoitalkMediaPrepareYouTube?.identity?.(request),args:[{phase:'bind',
        expected_account_ref:params.expected_account_ref,capture_token:identity.capture_token,
        owner_document_id:proven.documentId,owner_tab_id:owner.id}]}),1800,'media_prepare_identity_timeout');
    const proof = bound?.find(item => item.frameId === 0 && item.documentId === document)?.result;
    const currentOwner = await mediaBrowser.tabs.get(owner.id), currentStudio = await mediaBrowser.tabs.get(params.tab_id);
    accepted = selectedTabId === params.tab_id && currentOwner.windowId === studio.windowId &&
      mediaYouTubeOwnerTabAllowed(currentOwner,identity.handle) && currentStudio.url === studio.url &&
      proof?.mode === 'bound' && proof.account_ref === params.expected_account_ref && proof.capture_token === identity.capture_token &&
      Object.keys(proof).sort().join(',') === 'account_ref,capture_token,mode';
    return accepted;
  } catch { return false; } finally {
    // A failed owner check cannot revive an old token after its DOM is restored.
    // Only a freshly visible Studio Account menu can capture another proof.
    if (capturedToken && !accepted) {
      try { await bounded(mediaBrowser.scripting.executeScript({target:{tabId:params.tab_id,documentIds:[document]},
        func: request => globalThis.__aoitalkMediaPrepareYouTube?.identity?.(request),args:[{phase:'invalidate',
          expected_account_ref:params.expected_account_ref,capture_token:capturedToken}]}),1800,'media_prepare_identity_timeout'); } catch {}
    }
  }
}
async function mediaPrepareReadiness(params,guard) {
  const mediaBrowser=mediaContextChrome(guard);
  const youtube = params?.platform === 'youtube', platform = youtube ? 'youtube' : 'x';
  const reply = (error, document = '') => ({platform,account_ref:'',account_ref_kind:youtube ? 'youtube_channel_id' : 'x_handle',
    document_id:document,helper_revision:youtube ? 4 : 9,composer_ready:false,account_verified:false,available:false,
    executed:false,publication_confirmed:false,outcome_unknown:false,error});
  const keys = new Set(['platform','tab_id','expected_account_ref']);
  if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).length !== 3 ||
      Object.keys(params).some(key => !keys.has(key)) || !['x','youtube'].includes(params.platform) ||
      !Number.isInteger(params.tab_id) || params.tab_id < 0 || typeof params.expected_account_ref !== 'string' ||
      !(youtube ? /^UC[A-Za-z0-9_-]{22}$/ : /^@[A-Za-z0-9_]{1,15}$/).test(params.expected_account_ref))
    return reply('media_prepare_params_invalid');
  if (selectedTabId !== params.tab_id) return reply('media_prepare_target_mismatch');
  const scope = tab => youtube ? mediaYouTubeTabAllowed(tab,params.expected_account_ref) : mediaTabAllowed(tab);
  let document = '';
  try {
    const tab = await mediaBrowser.tabs.get(params.tab_id);
    if (!scope(tab)) return reply('media_prepare_scope_rejected');
    const initialUrl = tab.url;
    const inspected = await bounded(mediaBrowser.scripting.executeScript({target:{tabId:params.tab_id,frameIds:[0]},
      func: youtube => youtube ? {present:Boolean(globalThis.__aoitalkMediaPrepareYouTube),
        revision:globalThis.__aoitalkMediaPrepareYouTube?.version || 0,
        readiness:globalThis.__aoitalkMediaPrepareYouTube?.readiness_revision || 0} :
        {present:Boolean(globalThis.__aoitalkBrowser),revision:globalThis.__aoitalkBrowser?.version || 0,
          readiness:typeof globalThis.__aoitalkBrowser?.mediaPrepareReadiness === 'function'},args:[youtube]}),
      1800,'media_prepare_readiness_inspection_timeout');
    const existing = inspected?.find(item => item.frameId === 0);
    document = existing?.documentId;
    if (typeof document !== 'string' || !document || document.length > 128) return reply('media_prepare_document_changed');
    if (existing.result?.present && (youtube ? existing.result.revision !== 4 || existing.result.readiness !== 4 :
      existing.result.revision !== 9 || !existing.result.readiness))
      return reply('media_prepare_new_document_required',document);
    const target = {tabId:params.tab_id,documentIds:[document]};
    const injected = await bounded(mediaBrowser.scripting.executeScript({target,files:[youtube ? 'media-prepare-youtube.js' : 'content.js']}),
      1800,'media_prepare_readiness_injection_timeout');
    if (!injected?.some(item => item.frameId === 0 && item.documentId === document)) return reply('media_prepare_document_changed',document);
    if (selectedTabId !== params.tab_id) return reply('media_prepare_target_mismatch',document);
    const freshTab = await mediaBrowser.tabs.get(params.tab_id);
    if (!scope(freshTab) || freshTab.url !== initialUrl) return reply('media_prepare_scope_rejected',document);
    if (youtube && !await mediaYouTubeIdentity(mediaBrowser,params,document)) return reply('media_prepare_account_unverified',document);
    const rows = await bounded(mediaBrowser.scripting.executeScript({target,func: async request => {
      if (location.href !== request.url) return;
      const observed = request.params.platform === 'youtube' ?
        await globalThis.__aoitalkMediaPrepareYouTube?.({...request.params,phase:'readiness',run_id:'readiness_probe'}) :
        await globalThis.__aoitalkBrowser?.mediaPrepareReadiness(request.params);
      return location.href === request.url ? observed : undefined;
    },args:[{params,url:initialUrl}]}),3500,'media_prepare_readiness_timeout');
    const frame = rows?.find(item => item.frameId === 0), result = frame?.result;
    const responseKeys = new Set(['platform','account_ref','account_ref_kind','document_id','helper_revision','composer_ready',
      'account_verified','available','executed','publication_confirmed','outcome_unknown','error']);
    const valid = result && typeof result === 'object' && !Array.isArray(result) && Object.keys(result).every(key => responseKeys.has(key)) &&
      result.platform === platform && result.account_ref_kind === (youtube ? 'youtube_channel_id' : 'x_handle') &&
      typeof result.account_ref === 'string' && typeof result.document_id === 'string' && result.helper_revision === (youtube ? 4 : 9) &&
      frame.documentId === document && ['composer_ready','account_verified','available'].every(key => typeof result[key] === 'boolean') &&
      result.executed === false && result.publication_confirmed === false && result.outcome_unknown === false &&
      (result.error === undefined || typeof result.error === 'string' && /^media_prepare_[a-z_]+$/.test(result.error)) &&
      (!result.account_verified || result.account_ref === (youtube ? params.expected_account_ref : params.expected_account_ref.toLowerCase())) &&
      (!result.available || !result.error && result.composer_ready && result.account_verified &&
        result.account_ref === (youtube ? params.expected_account_ref : params.expected_account_ref.toLowerCase()));
    if (!valid) return reply('media_prepare_readiness_unverified',document);
    if (selectedTabId !== params.tab_id) return reply('media_prepare_target_mismatch',document);
    if (youtube && !await mediaYouTubeIdentity(mediaBrowser,params,document)) return reply('media_prepare_account_unverified',document);
    return {...result,document_id:document};
  } catch {return reply('media_prepare_readiness_unverified',typeof document === 'string' ? document : '');}
}
async function mediaPrepareText(params,guard) {
  const mediaBrowser=mediaContextChrome(guard);
  await mediaRunsReady;
  const keys = new Set(['platform','phase','expected_account_ref','text','tab_id','run_id','expected_document_id']);
  if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).some(key => !keys.has(key)) ||
      params.platform !== 'x' || !['inspect','fill','verify'].includes(params.phase) ||
      typeof params.expected_account_ref !== 'string' || !/^@[a-zA-Z0-9_]{1,15}$/.test(params.expected_account_ref) ||
      !Number.isInteger(params.tab_id) || params.tab_id < 0 || typeof params.run_id !== 'string' ||
      !/^[a-zA-Z0-9_-]{1,128}$/.test(params.run_id) ||
      (params.phase === 'inspect' ? params.text !== undefined : typeof params.text !== 'string' || !params.text ||
        new TextEncoder().encode(params.text).length > 16384 || /[\u0000-\u0008\u000b-\u001f\u007f]/.test(params.text)) ||
      (params.phase !== 'inspect' && (typeof params.expected_document_id !== 'string' || !params.expected_document_id)) ||
      (params.expected_document_id !== undefined && (typeof params.expected_document_id !== 'string' || params.expected_document_id.length > 128)))
    return mediaError('media_prepare_params_invalid');
  if (selectedTabId !== params.tab_id) return mediaError('media_prepare_target_mismatch');
  if (mediaBusyTabs.has(params.tab_id)) return mediaError('media_prepare_busy');
  const now = Date.now();
  for (const [key, value] of mediaRuns) if (value.expires < now && !value.fillStarted) mediaRuns.delete(key);
  const run = mediaRuns.get(params.run_id);
  if (run && (run.tab !== params.tab_id || run.account !== params.expected_account_ref.toLowerCase()))
    return mediaError('media_prepare_run_mismatch');
  if (run && params.phase === 'fill' && (run.unknown || run.fillStarted))
    return mediaError(run.unknown ? 'media_prepare_outcome_unknown' : 'media_prepare_fill_already_started');
  if (params.phase !== 'inspect' && (!run || run.document !== params.expected_document_id))
    return mediaError('media_prepare_document_changed');
  mediaBusyTabs.add(params.tab_id);
  let writeDispatched = false;
  const markUnknown = async () => {
    if (run && writeDispatched) {
      run.unknown = true;
      try { await persistMediaRuns(); } catch { /* durable fillStarted still forbids replay */ }
    }
  };
  try {
    let tab;
    try { tab = await mediaBrowser.tabs.get(params.tab_id); } catch { return mediaError('media_prepare_target_unavailable'); }
    if (!mediaTabAllowed(tab)) return mediaError('media_prepare_scope_rejected');
    const injectionTarget = params.expected_document_id ?
      {tabId:params.tab_id, documentIds:[params.expected_document_id]} : {tabId:params.tab_id, frameIds:[0]};
    let injected;
    try {
      injected = await bounded(mediaBrowser.scripting.executeScript({target:injectionTarget, files:['content.js']}),
        1800, 'media_prepare_injection_timeout');
    } catch { return mediaError('media_prepare_document_changed'); }
    const document = injected?.find(item => item.frameId === 0)?.documentId;
    if (typeof document !== 'string' || !document || document.length > 128 ||
        (params.expected_document_id && document !== params.expected_document_id) ||
        (run && run.document !== document)) return mediaError('media_prepare_document_changed');
    if (selectedTabId !== params.tab_id || !mediaTabAllowed(await mediaBrowser.tabs.get(params.tab_id)))
      return mediaError('media_prepare_target_mismatch');
    if (run && params.phase === 'fill') {
      run.fillStarted = true;
      // Commit the no-replay marker before dispatch. Session storage survives
      // MV3 service-worker suspension; it contains no draft text or secrets.
      await persistMediaRuns();
    }
    writeDispatched = params.phase === 'fill';
    let rows;
    try {
      rows = await bounded(mediaBrowser.scripting.executeScript({target:{tabId:params.tab_id, documentIds:[document]},
        func: request => globalThis.__aoitalkBrowser?.mediaPrepareText(request), args:[params]}),
        3500, 'media_prepare_operation_timeout');
    } catch {
      await markUnknown();
      return {...mediaError('media_prepare_outcome_unknown', writeDispatched), document_id:document};
    }
    const frame = rows?.find(item => item.frameId === 0), result = frame?.result;
    const resultKeys = new Set(['platform','account_ref','account_ref_kind','document_id','text_sha256',
      'composer_ready','final_confirmation_available','prepared','executed','outcome_unknown','error']);
    const valid = result && typeof result === 'object' && !Array.isArray(result) &&
      Object.keys(result).every(key => resultKeys.has(key)) && result.platform === 'x' &&
      result.account_ref_kind === 'x_handle' && typeof result.account_ref === 'string' &&
      typeof result.document_id === 'string' &&
      (result.text_sha256 === null || (typeof result.text_sha256 === 'string' && /^[a-f0-9]{64}$/.test(result.text_sha256))) &&
      ['composer_ready','final_confirmation_available','prepared','executed','outcome_unknown'].every(key => typeof result[key] === 'boolean') &&
      (result.error === undefined || (typeof result.error === 'string' && /^media_prepare_[a-z_]+$/.test(result.error))) &&
      frame.documentId === document && !(params.phase !== 'verify' && result.prepared) &&
      !(params.phase !== 'fill' && result.executed);
    if (!valid) {
      await markUnknown();
      return {...mediaError('media_prepare_response_invalid', writeDispatched), document_id:document};
    }
    if (selectedTabId !== params.tab_id || !mediaTabAllowed(await mediaBrowser.tabs.get(params.tab_id))) {
      await markUnknown();
      return {...mediaError('media_prepare_target_mismatch', writeDispatched), document_id:document};
    }
    if (!result.error && result.account_ref !== params.expected_account_ref.toLowerCase()) {
      await markUnknown();
      return {...mediaError('media_prepare_account_mismatch', writeDispatched), document_id:document};
    }
    if (params.phase === 'inspect' && !result.error && !run) {
      if (mediaRuns.size >= 256) return mediaError('media_prepare_run_limit');
      mediaRuns.set(params.run_id, {tab:params.tab_id, account:result.account_ref, document, expires:now+300000,
        unknown:false,fillStarted:false,browser_context:{...guard.browser_context}});
    }
    if (run && result.outcome_unknown) run.unknown = true;
    if (run && params.phase === 'verify' && !result.error && result.prepared) run.verifiedTextHash = result.text_sha256;
    await persistMediaRuns();
    return {...result, document_id:document};
  } catch {
    await markUnknown();
    return mediaError('media_prepare_outcome_unknown', writeDispatched);
  } finally { mediaBusyTabs.delete(params.tab_id); }
}
async function mediaPrepareYouTube(params,guard) {
  const mediaBrowser=mediaContextChrome(guard);
  await mediaYouTubeReady;
  const keys = new Set(['platform','phase','tab_id','run_id','expected_account_ref','expected_document_id','metadata','metadata_sha256']);
  const error = (code, unknown = false) => ({platform:'youtube',account_ref:'',account_ref_kind:'youtube_channel_id',document_id:'',
    metadata_sha256:null,title_sha256:null,description_sha256:null,asset_sha256:null,composer_ready:false,
    final_confirmation_available:false,prepared:false,executed:unknown,outcome_unknown:unknown,error:code});
  if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).some(key => !keys.has(key)) ||
      params.platform !== 'youtube' || !['inspect','fill','verify'].includes(params.phase) || !Number.isInteger(params.tab_id) || params.tab_id < 0 ||
      typeof params.run_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(params.run_id) ||
      typeof params.expected_account_ref !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(params.expected_account_ref) ||
      (params.expected_document_id !== undefined && (typeof params.expected_document_id !== 'string' || !params.expected_document_id || params.expected_document_id.length > 128)) ||
      (params.phase === 'inspect' ? params.metadata !== undefined || params.metadata_sha256 !== undefined :
        !params.expected_document_id || !params.metadata || typeof params.metadata !== 'object' || Array.isArray(params.metadata) ||
        Object.keys(params.metadata).sort().join(',') !== 'audience,description,tags,title,visibility' ||
        typeof params.metadata.title !== 'string' || [...params.metadata.title].length < 1 || [...params.metadata.title].length > 100 ||
        typeof params.metadata.description !== 'string' || [...params.metadata.description].length > 5000 ||
        /[<>\u0000-\u0008\u000b-\u001f\u007f]/.test(params.metadata.title+params.metadata.description) ||
        params.metadata.visibility !== 'private' || params.metadata.audience !== 'not_made_for_kids' ||
        !Array.isArray(params.metadata.tags) || params.metadata.tags.length ||
        typeof params.metadata_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(params.metadata_sha256)))
    return error('media_prepare_params_invalid');
  if (selectedTabId !== params.tab_id) return error('media_prepare_target_mismatch');
  if (mediaBusyTabs.has(params.tab_id)) return error('media_prepare_busy');
  let run = mediaYouTubeRuns.get(params.run_id), dispatched = false;
  if (run && (run.tab !== params.tab_id || run.account !== params.expected_account_ref)) return error('media_prepare_run_mismatch');
  if (params.phase !== 'inspect' && (!run || run.document !== params.expected_document_id)) return error('media_prepare_document_changed');
  if (params.phase === 'fill' && (run.fillStarted || run.unknown)) return error(run.unknown ? 'media_prepare_outcome_unknown' : 'media_prepare_fill_already_started');
  if (params.phase !== 'inspect' && (!run.assetSha || run.metadataHash !== params.metadata_sha256)) return error('media_prepare_asset_unverified');
  mediaBusyTabs.add(params.tab_id);
  try {
    if (!mediaYouTubeTabAllowed(await mediaBrowser.tabs.get(params.tab_id),params.expected_account_ref)) return error('media_prepare_scope_rejected');
    const target = params.expected_document_id ? {tabId:params.tab_id,documentIds:[params.expected_document_id]} : {tabId:params.tab_id,frameIds:[0]};
    const injected = await bounded(mediaBrowser.scripting.executeScript({target,files:['media-prepare-youtube.js']}),1800,'media_prepare_injection_timeout');
    const document = injected?.find(item => item.frameId === 0)?.documentId;
    if (typeof document !== 'string' || !document || document.length > 128 ||
        params.expected_document_id && document !== params.expected_document_id || run && document !== run.document)
      return error('media_prepare_document_changed');
    if (selectedTabId !== params.tab_id || !mediaYouTubeTabAllowed(await mediaBrowser.tabs.get(params.tab_id),params.expected_account_ref)) return error('media_prepare_target_mismatch');
    if (!await mediaYouTubeIdentity(mediaBrowser,params,document)) return error('media_prepare_account_unverified');
    if (params.phase === 'fill') {run.fillStarted = true;await persistMediaYouTubeRuns();}
    const dispatchTab = await mediaBrowser.tabs.get(params.tab_id);
    if (selectedTabId !== params.tab_id || !mediaYouTubeTabAllowed(dispatchTab,params.expected_account_ref)) return error('media_prepare_target_mismatch');
    dispatched = params.phase === 'fill';
    const rows = await bounded(mediaBrowser.scripting.executeScript({target:{tabId:params.tab_id,documentIds:[document]},
      func: request => globalThis.__aoitalkMediaPrepareYouTube?.(request),args:[params]}),
      params.phase === 'fill' ? 12000 : 4500,'media_prepare_operation_timeout');
    const frame = rows?.find(item => item.frameId === 0), result = frame?.result;
    const replyKeys = new Set(['platform','account_ref','account_ref_kind','document_id','metadata_sha256','title_sha256','description_sha256',
      'asset_sha256','composer_ready','final_confirmation_available','prepared','executed','outcome_unknown','error']);
    const valid = result && typeof result === 'object' && !Array.isArray(result) && Object.keys(result).every(key => replyKeys.has(key)) &&
      result.platform === 'youtube' && result.account_ref_kind === 'youtube_channel_id' && typeof result.account_ref === 'string' &&
      typeof result.document_id === 'string' && frame.documentId === document &&
      ['metadata_sha256','title_sha256','description_sha256','asset_sha256'].every(key => result[key] === null || typeof result[key] === 'string' && /^[a-f0-9]{64}$/.test(result[key])) &&
      ['composer_ready','final_confirmation_available','prepared','executed','outcome_unknown'].every(key => typeof result[key] === 'boolean') &&
      (result.error === undefined || typeof result.error === 'string' && /^media_prepare_[a-z_]+$/.test(result.error)) &&
      !(params.phase !== 'fill' && result.executed) && !(params.phase !== 'verify' && result.prepared) &&
      (!result.prepared || !result.error && !result.outcome_unknown && result.composer_ready && result.final_confirmation_available &&
        result.metadata_sha256 === params.metadata_sha256 && result.asset_sha256 === run.assetSha && result.title_sha256 && result.description_sha256);
    if (!valid || selectedTabId !== params.tab_id || !mediaYouTubeTabAllowed(await mediaBrowser.tabs.get(params.tab_id),params.expected_account_ref) ||
        !await mediaYouTubeIdentity(mediaBrowser,params,document))
      throw new Error('media_prepare_response_invalid');
    if (!result.error && result.account_ref !== params.expected_account_ref) throw new Error('media_prepare_account_mismatch');
    if (params.phase === 'inspect' && !result.error && !run) {
      if (mediaYouTubeRuns.size >= 128) return error('media_prepare_run_limit');
      run = {tab:params.tab_id,account:params.expected_account_ref,document,fillStarted:false,unknown:false,
        browser_context:{...guard.browser_context}};
      mediaYouTubeRuns.set(params.run_id,run);
    }
    if (run && result.outcome_unknown) run.unknown = true;
    await persistMediaYouTubeRuns();
    return {...result,document_id:document};
  } catch {
    if (run && dispatched) {run.unknown = true;try {await persistMediaYouTubeRuns();} catch { /* precommitted fill marker prevents replay */ }}
    return error('media_prepare_outcome_unknown',dispatched);
  } finally {mediaBusyTabs.delete(params.tab_id);}
}
async function mediaPrepareAsset(phase, params, platform = 'x',guard) {
  const mediaBrowser=mediaContextChrome(guard);
  await mediaRunsReady;await mediaAssetsReady;await mediaYouTubeReady;
  const youtube = platform === 'youtube', bodyHash = youtube ? 'metadata_sha256' : 'text_sha256';
  const pins = ['platform','tab_id','run_id','expected_document_id','expected_account_ref',bodyHash,'scope_hash','transfer_id'];
  const keys = new Set([...pins,...(['begin','inspect'].includes(phase) ? ['asset_sha256','size_bytes','mime_type'] : phase === 'append' ? ['offset','base64'] : [])]);
  let state = mediaAssetStates.get(params?.transfer_id), dispatched = false;
  const reply = (error, unknown = false) => ({transfer_id:params?.transfer_id || '',scope_hash:params?.scope_hash || '',
    document_id:params?.expected_document_id || '',account_ref:params?.expected_account_ref || '',
    asset_sha256:state?.sha || params?.asset_sha256 || '',offset:state?.offset || 0,
    accepted:false,dispatched:unknown,files_verified:false,attachment_visible:false,executed:unknown,outcome_unknown:unknown,error});
  if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).some(key => !keys.has(key)) ||
      params.platform !== platform || !Number.isInteger(params.tab_id) || params.tab_id < 0 ||
      !['run_id','transfer_id'].every(key => typeof params[key] === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(params[key])) ||
      typeof params.expected_document_id !== 'string' || !params.expected_document_id || params.expected_document_id.length > 128 ||
      typeof params.expected_account_ref !== 'string' || !(youtube ? /^UC[A-Za-z0-9_-]{22}$/ : /^@[a-z0-9_]{1,15}$/).test(params.expected_account_ref) ||
      ![bodyHash,'scope_hash'].every(key => typeof params[key] === 'string' && /^[a-f0-9]{64}$/.test(params[key])) ||
      (['begin','inspect'].includes(phase) && (!Number.isInteger(params.size_bytes) || params.size_bytes < 1 || params.size_bytes > (youtube ? 67108864 : 26214400) ||
        !(youtube ? ['video/mp4','video/webm'] : ['image/jpeg','image/png','image/webp']).includes(params.mime_type) || typeof params.asset_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(params.asset_sha256))) ||
      (phase === 'append' && (!Number.isInteger(params.offset) || params.offset < 0 || typeof params.base64 !== 'string' ||
        !params.base64 || params.base64.length > 349528 || !/^[A-Za-z0-9+/]*={0,2}$/.test(params.base64))))
    return reply('media_prepare_asset_params_invalid');
  const pin = JSON.stringify(pins.map(key => params[key])), run = (youtube ? mediaYouTubeRuns : mediaRuns).get(params.run_id);
  if (!run || run.tab !== params.tab_id || run.account !== params.expected_account_ref || run.document !== params.expected_document_id ||
      (youtube ? run.metadataHash && run.metadataHash !== params.metadata_sha256 : run.verifiedTextHash !== params.text_sha256))
    return reply('media_prepare_asset_text_unverified');
  if (selectedTabId !== params.tab_id) return reply('media_prepare_target_mismatch');
  if (mediaBusyTabs.has(params.tab_id)) return reply('media_prepare_busy');
  if (phase === 'begin' ? Boolean(state) : !state || state.pin !== pin)
    return reply('media_prepare_asset_run_mismatch');
  if (phase === 'inspect' && (state.sha !== params.asset_sha256 || state.size !== params.size_bytes || state.mime !== params.mime_type))
    return reply('media_prepare_asset_run_mismatch');
  if (phase !== 'inspect' && (state?.unknown || state?.dispatchStarted)) return reply('media_prepare_asset_outcome_unknown',Boolean(state.dispatchStarted));
  if (phase === 'append' && params.offset !== state.offset) return reply('media_prepare_asset_chunk_rejected');
  if (phase === 'finish' && state.offset !== state.size) return reply('media_prepare_asset_incomplete');
  mediaBusyTabs.add(params.tab_id);
  try {
    const scopeAllowed = tab => youtube ? mediaYouTubeTabAllowed(tab,params.expected_account_ref) : mediaTabAllowed(tab);
    if (!scopeAllowed(await mediaBrowser.tabs.get(params.tab_id))) return reply('media_prepare_scope_rejected');
    if (phase === 'begin') {
      if (mediaAssetStates.size >= 256) return reply('media_prepare_asset_run_limit');
      state = {pin,sha:params.asset_sha256,size:params.size_bytes,mime:params.mime_type,offset:0,dispatchStarted:false,unknown:false,
        browser_context:{...guard.browser_context}};
      mediaAssetStates.set(params.transfer_id,state);
      if (youtube) {run.metadataHash = params.metadata_sha256;await persistMediaYouTubeRuns();}
    }
    if (phase === 'finish') state.dispatchStarted = true;
    await persistMediaAssets();
    const target = {tabId:params.tab_id,documentIds:[params.expected_document_id]};
    await bounded(mediaBrowser.scripting.executeScript({target,files:[youtube ? 'media-prepare-youtube.js' : 'content.js']}),1800,'media_prepare_asset_injection_timeout');
    if (selectedTabId !== params.tab_id || !scopeAllowed(await mediaBrowser.tabs.get(params.tab_id))) return reply('media_prepare_target_mismatch');
    if (youtube && !await mediaYouTubeIdentity(mediaBrowser,{...params,phase},params.expected_document_id)) return reply('media_prepare_account_unverified');
    const dispatchTab = await mediaBrowser.tabs.get(params.tab_id);
    if (selectedTabId !== params.tab_id || !scopeAllowed(dispatchTab)) return reply('media_prepare_target_mismatch');
    dispatched = phase === 'finish';
    const rows = await bounded(mediaBrowser.scripting.executeScript({target,
      func: request => request.platform === 'youtube' ? globalThis.__aoitalkMediaPrepareYouTubeAsset?.(request.phase,request.params) :
        globalThis.__aoitalkBrowser?.mediaPrepareAsset(request.phase,request.params),args:[{phase,params,platform}]}),
      phase === 'finish' ? 6000 : 3500,'media_prepare_asset_operation_timeout');
    const frame = rows?.find(item => item.frameId === 0), result = frame?.result && {...frame.result,document_id:frame.documentId};
    const receiptKeys = new Set(['transfer_id','scope_hash','document_id','account_ref','asset_sha256','offset',
      'accepted','dispatched','files_verified','attachment_visible','executed','outcome_unknown','error']);
    const valid = result && typeof result === 'object' && !Array.isArray(result) && Object.keys(result).every(key => receiptKeys.has(key)) &&
      result.transfer_id === params.transfer_id && result.scope_hash === params.scope_hash && result.document_id === params.expected_document_id &&
      result.account_ref === params.expected_account_ref && result.asset_sha256 === state.sha && frame.documentId === params.expected_document_id &&
      Number.isInteger(result.offset) && result.offset >= state.offset && result.offset <= state.size &&
      ['accepted','dispatched','files_verified','attachment_visible','executed','outcome_unknown'].every(key => typeof result[key] === 'boolean') &&
      (result.error === undefined || typeof result.error === 'string' && /^media_prepare_[a-z_]+$/.test(result.error)) &&
      (phase === 'finish' || !result.dispatched && !result.executed && (phase === 'inspect' || !result.files_verified && !result.attachment_visible)) &&
      (!result.accepted || !result.error && !result.outcome_unknown &&
        (phase !== 'finish' || result.dispatched && result.executed && result.files_verified && result.attachment_visible) &&
        (phase !== 'inspect' || result.files_verified && result.attachment_visible));
    if (!valid || selectedTabId !== params.tab_id || !scopeAllowed(await mediaBrowser.tabs.get(params.tab_id)) ||
        youtube && !await mediaYouTubeIdentity(mediaBrowser,{...params,phase},params.expected_document_id)) {
      state.unknown = true;await persistMediaAssets();return reply('media_prepare_asset_response_invalid',dispatched);
    }
    state.offset = result.offset;
    if (result.outcome_unknown) state.unknown = true;
    if (youtube && ['finish','inspect'].includes(phase) && result.accepted && result.files_verified && result.attachment_visible) {
      run.assetSha = state.sha;await persistMediaYouTubeRuns();
    }
    await persistMediaAssets();
    return result;
  } catch {
    if (state) {state.unknown = true;try {await persistMediaAssets();} catch { /* precommitted dispatch marker prevents replay */ }}
    return reply('media_prepare_asset_outcome_unknown',dispatched);
  } finally { mediaBusyTabs.delete(params.tab_id); }
}
async function execute(command,params={}) {
  await selectedTabReady;
  await browserContextReady;
  if(command==='status') return {connected,selected_tab_id:selectedTabId,bridge_script_version:9};
  if(command==='media_prepare_context') {
    if(!params||typeof params!=='object'||Array.isArray(params)||Object.keys(params).length)
      throw Error('media_prepare_params_invalid');
    return browserContext();
  }
  if(command==='media_prepare_capabilities') {
    if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).length)
      return {version:2,platforms:[],text:false,media:false,x_image:false,youtube_private:false,final_confirmation:false,
        browser_context_pin:false,browser_context:browserContext()};
    return {version:2,platforms:['x','youtube'],text:true,media:false,x_image:true,youtube_private:true,final_confirmation:false,
      browser_context_pin:contextMatches(browserContext()),browser_context:browserContext()};
  }
  if(command.startsWith('media_prepare_')) {
    await Promise.all([mediaRunsReady,mediaAssetsReady,mediaYouTubeReady]);
    const expected=params?.expected_browser_context;
    if(!contextMatches(expected))return mediaContextError(command,params);
    const clean={...params};delete clean.expected_browser_context;
    const captured={...expected},guard=()=>contextMatches(captured);
    Object.defineProperty(guard,'browser_context',{value:captured});
    const contextSame=value=>contextValid(value)&&value.context_id===captured.context_id&&value.context_epoch===captured.context_epoch;
    const oldRun=(clean.platform==='youtube'?mediaYouTubeRuns:mediaRuns).get(clean.run_id),oldAsset=mediaAssetStates.get(clean.transfer_id);
    if(oldRun&&!contextSame(oldRun.browser_context)||oldAsset&&!contextSame(oldAsset.browser_context))
      return mediaContextError(command,clean);
    let result;
    if(command==='media_prepare_text')result=await mediaPrepareText(clean,guard);
    else if(command==='media_prepare_readiness')result=await mediaPrepareReadiness(clean,guard);
    else if(command==='media_prepare_youtube')result=await mediaPrepareYouTube(clean,guard);
    else {
      const match=/^media_prepare_(youtube_)?asset_(begin|append|finish|inspect)$/.exec(command);
      if(!match)throw Error('edge_command_unknown');
      result=await mediaPrepareAsset(match[2],clean,match[1]?'youtube':'x',guard);
    }
    if(!guard()) {
      const mutating=clean.phase==='fill'||/asset_(?:begin|append|finish)$/.test(command);
      const run=(clean.platform==='youtube'?mediaYouTubeRuns:mediaRuns).get(clean.run_id);
      const asset=mediaAssetStates.get(clean.transfer_id);
      if(mutating&&run){run.unknown=true;await (clean.platform==='youtube'?persistMediaYouTubeRuns():persistMediaRuns());}
      if(mutating&&asset){asset.unknown=true;await persistMediaAssets();}
      return mediaContextError(command,clean,mutating);
    }
    return result;
  }
  if(command==='close_tab') { await chrome.tabs.remove(params.tab_id); if(selectedTabId===params.tab_id) {selectedTabId=null;await chrome.storage.session.remove('selectedTabId');} return {}; }
  if(command==='select_tab') {selectedTabId=params.tab_id ?? null;await chrome.storage.session.set({selectedTabId});return {selected_tab_id:selectedTabId};}
  if(command==='wake_tab') return summary(await activateTab(params.tab_id));
  if(command==='tabs') return {tabs:(await chrome.tabs.query({})).map(summary),selected_tab_id:selectedTabId};
  if(command==='resolve_tab') return resolveTab(params);
  if(command==='crowdworks_inspect') return crowdworksInspect(params);
  if(command==='observe') return observe(params);
  if(command==='act') return act(params);
  if(command==='navigate') { await chrome.tabs.update(params.tab_id,{url:params.url});return summary(await ready(params.tab_id)); }
  if(command==='back') { await chrome.tabs.goBack(params.tab_id);return summary(await ready(params.tab_id)); }
  if(command==='scroll') {
    await chrome.scripting.executeScript({target:{tabId:params.tab_id},func:direction=>window.scrollBy(0,direction==='up'?-650:650),args:[params.direction]});
    return summary(await chrome.tabs.get(params.tab_id));
  }
  throw new Error('edge_command_unknown');
}
async function connect() {
  if(nativePort) return;
  await browserContextReady;
  const settings=await chrome.storage.local.get({enabled:true});
  if(!settings.enabled||nativePort) return;
  connectionError='';
  const port=chrome.runtime.connectNative('com.aoitalk.edge');
  const portEpoch=crypto.randomUUID();browserContextEpoch=portEpoch;
  nativePort=port;
  port.onMessage.addListener(async message=>{
    if(nativePort!==port||browserContextEpoch!==portEpoch)return;
    if(message.event==='connected') { connected=true; connectionError='';return; }
    if(message.event==='host_error') { connectionError=message.error;return; }
    if(!message.id) return;
    try {
      const result=await execute(message.command,message.params);
      if(nativePort===port&&browserContextEpoch===portEpoch)port.postMessage({id:message.id,ok:true,result});
    } catch(error) {
      try {if(nativePort===port&&browserContextEpoch===portEpoch)port.postMessage({id:message.id,ok:false,error:error.message || String(error)});} catch { /* port closed */ }
    }
  });
  port.onDisconnect.addListener(()=>{
    if(nativePort!==port||browserContextEpoch!==portEpoch)return;
    const lastError=chrome.runtime.lastError;
    connectionError=connectionError || lastError?.message || '接続が切れました';
    nativePort=null;connected=false;browserContextEpoch='';
  });
}
chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
  (async()=>{
    await selectedTabReady;
    if(message.command==='select_tab') {
      selectedTabId=message.tab_id;await chrome.storage.session.set({selectedTabId});
    } else if(message.command==='connect') {
      await chrome.storage.local.set({enabled:true});await connect();
    } else if(message.command==='disconnect') {
      await chrome.storage.local.set({enabled:false});nativePort?.disconnect();nativePort=null;connected=false;browserContextEpoch='';
    }
    return {connected,error:connectionError,selected_tab_id:selectedTabId};
  })().then(sendResponse,error=>sendResponse({connected:false,error:error.message}));
  return true;
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='reconnect') void connect();});
chrome.alarms.create('reconnect',{periodInMinutes:0.5});
const selectedTabReady = chrome.storage.session.get('selectedTabId').then(value=>{selectedTabId=value.selectedTabId ?? null;});
void connect();
