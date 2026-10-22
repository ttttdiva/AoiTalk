// Closed YouTube Studio preparation operations in the extension's isolated
// world. They use the selected channel's ordinary UI, never network endpoints,
// cookies, page globals, credentials, arbitrary selectors, or final Save.
(() => {
  const readinessParamsValid = params => params && typeof params === 'object' && !Array.isArray(params) &&
    Object.keys(params).sort().join(',') === 'expected_account_ref,phase,platform,run_id,tab_id' &&
    params.platform === 'youtube' && params.phase === 'readiness' && params.run_id === 'readiness_probe' &&
    Number.isInteger(params.tab_id) && params.tab_id >= 0 && typeof params.expected_account_ref === 'string' &&
    /^UC[A-Za-z0-9_-]{22}$/.test(params.expected_account_ref);
  const readinessResponse = (documentId, account = '', composerReady = false, accountVerified = false, error = '') => ({
    platform:'youtube',account_ref:account,account_ref_kind:'youtube_channel_id',document_id:documentId,helper_revision:4,
    composer_ready:composerReady,account_verified:accountVerified,available:composerReady && accountVerified && !error,
    executed:false,publication_confirmed:false,outcome_unknown:false,...(error ? {error} : {}),
  });
  const legacyPrepare = globalThis.__aoitalkMediaPrepareYouTube;
  const legacyAsset = globalThis.__aoitalkMediaPrepareYouTubeAsset;
  if (legacyPrepare?.version === 4 && legacyAsset?.version === 4) return;
  if (legacyPrepare || legacyAsset) {
    // Loaded v1/v2/v3 closures own upload evidence we cannot recreate. They did
    // not prove the channel against visible current-account UI. Preserve only
    // noncertifying read-only evidence and require a new document for writes.
    // Never clear its maps, reload the tab, or resume a previous dispatch.
    const legacyDocument = crypto.randomUUID();
    const required = () => ({platform:'youtube',account_ref:'',account_ref_kind:'youtube_channel_id',
      document_id:legacyDocument,metadata_sha256:null,title_sha256:null,description_sha256:null,asset_sha256:null,
      composer_ready:false,final_confirmation_available:false,prepared:false,executed:false,outcome_unknown:false,
      error:'media_prepare_new_document_required'});
    const preservedPrepare = async params => {
      if (params?.phase === 'readiness') return readinessResponse(legacyDocument, '', false, false,
        readinessParamsValid(params) ? 'media_prepare_new_document_required' : 'media_prepare_params_invalid');
      if ([1,2,3].includes(legacyPrepare?.version) && ['inspect','verify'].includes(params?.phase)) {
        const observed = await legacyPrepare(params);
        if (params.phase === 'inspect' && observed?.error === 'media_prepare_new_document_required') return required();
        if (params.phase === 'verify' || observed?.error || /^[a-f0-9]{64}$/.test(observed?.asset_sha256 || ''))
          return {...observed,composer_ready:false,final_confirmation_available:false,prepared:false,
            executed:false,outcome_unknown:true,error:'media_prepare_legacy_revision_unverified'};
      }
      return required();
    };
    const preservedAsset = async (phase, params) => {
      if ([1,2,3].includes(legacyAsset?.version) && phase === 'inspect') {
        const observed = await legacyAsset(phase,params);
        return {...observed,accepted:false,dispatched:false,files_verified:false,attachment_visible:false,
          executed:false,outcome_unknown:true,error:'media_prepare_legacy_revision_unverified'};
      }
      // Do not call the old closure for any write, including to retrieve a
      // receipt. Its evidence remains retained by the read-only closure above.
      return {transfer_id:params?.transfer_id || '',scope_hash:params?.scope_hash || '',
        document_id:legacyDocument,account_ref:params?.expected_account_ref || '',
        asset_sha256:params?.asset_sha256 || '',offset:0,
        accepted:false,dispatched:false,files_verified:false,attachment_visible:false,executed:false,outcome_unknown:false,
        error:'media_prepare_new_document_required'};
    };
    Object.defineProperty(preservedPrepare, 'version', {value:4});
    Object.defineProperty(preservedPrepare, 'readiness_revision', {value:4});
    Object.defineProperty(preservedPrepare, 'identity', {value:params => params && Object.keys(params).sort().join(',') === 'expected_account_ref,phase' &&
      params.phase === 'inspect' && /^UC[A-Za-z0-9_-]{22}$/.test(params.expected_account_ref)
      ? {mode:'legacy',account_ref:params.expected_account_ref,read_only:true} : null});
    Object.defineProperty(preservedAsset, 'version', {value:4});
    globalThis.__aoitalkMediaPrepareYouTube = preservedPrepare;
    globalThis.__aoitalkMediaPrepareYouTubeAsset = preservedAsset;
    return;
  }
  const documentId = crypto.randomUUID();
  const runs = new Map(), uploads = new Map();
  let busy = false;
  const MAX_VIDEO_BYTES = 64 * 1024 * 1024, MAX_CHUNK_BYTES = 256 * 1024;
  const CHANNEL = /^UC[A-Za-z0-9_-]{22}$/, ID = /^[A-Za-z0-9_-]{1,128}$/, SHA = /^[a-f0-9]{64}$/;
  const bytes = value => new TextEncoder().encode(value);
  const hash = async value => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
    typeof value === 'string' ? bytes(value) : value)), n => n.toString(16).padStart(2, '0')).join('');
  const canonical = value => JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])));
  const tick = () => new Promise(resolve => {
    const timer = setTimeout(resolve, 60);
    requestAnimationFrame(() => { clearTimeout(timer); resolve(); });
  });
  const visible = node => Boolean(node?.isConnected && node.ownerDocument === document &&
    node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden' &&
    (!node.checkVisibility || node.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})));
  const enabled = node => {
    if (!node || node.disabled || node.matches(':disabled') || node.readOnly || node.getAttribute('aria-readonly') === 'true') return false;
    for (let parent = node; parent; parent = parent.parentElement)
      if (parent.inert || parent.hasAttribute('disabled') || parent.getAttribute('aria-disabled') === 'true') return false;
    return true;
  };
  const unique = (root, selector, predicate = () => true) => {
    const nodes = Array.from(root.querySelectorAll(selector)).filter(predicate);
    return nodes.length === 1 ? nodes[0] : null;
  };
  function channelPath() {
    if (window !== window.top || location.protocol !== 'https:' || location.hostname !== 'studio.youtube.com' ||
        location.port || location.hash || location.search.length > 512) return '';
    const current = new URL(location.href);
    if (current.username || current.password) return '';
    const match = /^\/channel\/(UC[A-Za-z0-9_-]{22})(?:\/(?:videos(?:\/upload)?|dashboard))?$/.exec(current.pathname);
    if (!match) return '';
    const query = current.searchParams, keys = [...query.keys()];
    if (new Set(keys).size !== keys.length || keys.some(key => !['filter','sort','d','udvid'].includes(key)) ||
        keys.length && current.pathname !== '/channel/'+match[1]+'/videos/upload' ||
        query.has('filter') !== query.has('sort') || query.has('d') !== query.has('udvid')) return '';
    try {
      if (query.has('filter')) {
        const filter = JSON.parse(query.get('filter')), sort = JSON.parse(query.get('sort'));
        if (!Array.isArray(filter) || filter.length || !sort || typeof sort !== 'object' || Array.isArray(sort) ||
            Object.keys(sort).sort().join(',') !== 'columnType,sortOrder' || sort.columnType !== 'date' ||
            sort.sortOrder !== 'DESCENDING') return '';
      }
      if (query.has('d') && (query.get('d') !== 'ud' || !/^[A-Za-z0-9_-]{11}$/.test(query.get('udvid')))) return '';
    } catch { return ''; }
    if (query.has('udvid')) {
      const root = dialog(), videos = root && Array.from(root.querySelectorAll('a[href]')).filter(visible).flatMap(link => {
        try {
          const target = new URL(link.href);
          if (target.protocol !== 'https:' || target.port || target.username || target.password || target.hash) return [];
          if (target.hostname === 'youtu.be' && !target.search && /^\/[A-Za-z0-9_-]{11}$/.test(target.pathname))
            return [target.pathname.slice(1)];
          if (['youtube.com','www.youtube.com'].includes(target.hostname) && target.pathname === '/watch' &&
              [...target.searchParams.keys()].join(',') === 'v' && /^[A-Za-z0-9_-]{11}$/.test(target.searchParams.get('v')))
            return [target.searchParams.get('v')];
        } catch { /* An unrelated link cannot establish the current draft. */ }
        return [];
      });
      if (!videos?.length || !videos.every(video => video === query.get('udvid'))) return '';
    }
    return match[1];
  }
  let accountProof = null;
  const ACCOUNT_PROOF_MS = 180000;
  function visibleChannel() {
    const expected = channelPath();
    if (!expected) return '';
    // The URL alone cannot establish the account of a manual browser binding.
    // Accept only ordinary visible canonical links in the channel sidebar.
    // This fixed light-DOM selector has not been accepted on a live Studio;
    // an absent/unsupported sidebar must remain unverified, never guessed.
    const drawer = unique(document, 'ytcp-navigation-drawer', visible);
    if (!drawer) return '';
    const identities = [];
    for (const link of drawer.querySelectorAll('a[href]')) {
      if (!visible(link) || !hit(link)) continue;
      let target;
      try { target = new URL(link.href); } catch { continue; }
      if (target.protocol !== 'https:' || !['youtube.com','www.youtube.com'].includes(target.hostname) ||
          target.port || target.username || target.password || target.search || target.hash) continue;
      const match = /^\/channel\/(UC[A-Za-z0-9_-]{22})\/?$/.exec(target.pathname);
      if (match) identities.push(match[1]);
    }
    return identities.length && identities.every(identity => identity === expected) ? expected : '';
  }
  function accountCandidate() {
    const account = channelPath(), drawer = unique(document, 'ytcp-navigation-drawer', visible);
    if (!account || !drawer) return null;
    const canonicalLink = unique(drawer, 'a#overlay-link-to-youtube[href]');
    const sidebar = unique(drawer, '#entity-label-container > .entity-name-container', visible);
    const home = unique(document, 'a#home-button[href]', visible);
    const menu = unique(document, 'ytcp-popup-container ytd-multi-page-menu-renderer');
    const name = menu && unique(menu, '#channel-container #account-name');
    const handle = menu && unique(menu, '#channel-container #channel-handle');
    if (!canonicalLink || !sidebar || !home || !menu || !name || !handle) return null;
    const displayName = name.textContent.trim(), ownerHandle = handle.textContent.trim();
    if (!displayName || displayName !== sidebar.innerText.trim() ||
        !/^@[\p{L}\p{N}\p{M}_.-]{1,100}$/u.test(ownerHandle)) return null;
    const channelLink = unique(menu, 'ytd-compact-link-renderer a#endpoint[href]', link =>
      ['チャンネル','Your channel'].includes(link.textContent.trim()));
    if (!channelLink) return null;
    try {
      const canonical = new URL(canonicalLink.href), dashboard = new URL(home.href), publicChannel = new URL(channelLink.href);
      if (canonical.protocol !== 'https:' || !['youtube.com','www.youtube.com'].includes(canonical.hostname) ||
          canonical.port || canonical.username || canonical.password || canonical.search || canonical.hash ||
          !new RegExp('^/channel/'+account+'/?$').test(canonical.pathname) ||
          dashboard.protocol !== 'https:' || dashboard.hostname !== 'studio.youtube.com' || dashboard.port ||
          dashboard.username || dashboard.password || dashboard.search || dashboard.hash || dashboard.pathname !== '/channel/'+account ||
          !['http:','https:'].includes(publicChannel.protocol) || !['youtube.com','www.youtube.com'].includes(publicChannel.hostname) ||
          publicChannel.port || publicChannel.username || publicChannel.password || publicChannel.search || publicChannel.hash ||
          decodeURIComponent(publicChannel.pathname) !== '/'+ownerHandle) return null;
    } catch { return null; }
    return {account,displayName,ownerHandle,drawer,canonicalLink,sidebar,home,menu,name,handle,channelLink,
      canonicalHref:canonicalLink.href,homeHref:home.href,channelHref:channelLink.href};
  }
  function retainedAccount() {
    const current = accountCandidate(), saved = accountProof?.candidate;
    if (!current || !saved || Date.now() >= accountProof.expires ||
        Object.keys(current).some(key => current[key] !== saved[key])) {
      accountProof = null;
      return null;
    }
    return current;
  }
  function accountIdentity(params) {
    const keys = params?.phase === 'inspect' ? 'expected_account_ref,phase'
      : params?.phase === 'invalidate' ? 'capture_token,expected_account_ref,phase' : 'capture_token,expected_account_ref,owner_document_id,owner_tab_id,phase';
    if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).sort().join(',') !== keys ||
        !CHANNEL.test(params.expected_account_ref) || !['inspect','bind','invalidate'].includes(params.phase)) return null;
    if (params.phase === 'invalidate') {
      if (accountProof?.token === params.capture_token && accountProof.candidate.account === params.expected_account_ref) accountProof = null;
      return null;
    }
    if (params.phase === 'inspect') {
      const direct = visibleChannel();
      if (direct === params.expected_account_ref) return {mode:'visible',account_ref:direct};
      let current = retainedAccount();
      if (!current) {
        current = accountCandidate();
        // A residual closed menu can never issue a new identity proof.
        if (!current || current.account !== params.expected_account_ref ||
            ![current.menu,current.name,current.handle,current.channelLink].every(hit)) return null;
        accountProof = {candidate:current,token:crypto.randomUUID(),expires:Date.now()+ACCOUNT_PROOF_MS,
          bound:false,ownerDocument:null,ownerTab:null};
      }
      if (current.account !== params.expected_account_ref) return null;
      return {mode:'captured',account_ref:current.account,handle:current.ownerHandle,display_name:current.displayName,
        capture_token:accountProof.token,owner_document_id:accountProof.ownerDocument,owner_tab_id:accountProof.ownerTab};
    }
    if (!retainedAccount() || accountProof.candidate.account !== params.expected_account_ref ||
        params.capture_token !== accountProof.token || typeof params.owner_document_id !== 'string' ||
        !params.owner_document_id || params.owner_document_id.length > 128 ||
        !Number.isInteger(params.owner_tab_id) || params.owner_tab_id < 0 ||
        accountProof.ownerDocument && (accountProof.ownerDocument !== params.owner_document_id || accountProof.ownerTab !== params.owner_tab_id))
      return null;
    accountProof.bound = true;
    accountProof.ownerDocument = params.owner_document_id;
    accountProof.ownerTab = params.owner_tab_id;
    return {mode:'bound',account_ref:params.expected_account_ref,capture_token:accountProof.token};
  }
  function channel() {
    const direct = visibleChannel();
    if (direct) return direct;
    const current = retainedAccount();
    return current && accountProof.bound ? current.account : '';
  }
  function dialog() {
    // The upload dialog is required. Existing video edit pages, watch pages,
    // profile labels and unrelated body text cannot establish this operation.
    return unique(document, 'ytcp-uploads-dialog', visible);
  }
  function editors(root, requireVisible = true) {
    const valid = node => node.isConnected && node.ownerDocument === document && node.isContentEditable &&
      node.getAttribute('role') === 'textbox' && enabled(node) && (!requireVisible || visible(node));
    const title = unique(root, '#title-textarea #textbox[contenteditable="true"]', valid);
    const description = unique(root, '#description-textarea #textbox[contenteditable="true"]', valid);
    return title && description ? {title, description} : null;
  }
  function radio(root, name, requireVisible = true) {
    return unique(root, `tp-yt-paper-radio-button[name="${name}"]`, node =>
      node.isConnected && node.ownerDocument === document && enabled(node) && (!requireVisible || visible(node)));
  }
  const checked = node => Boolean(node && (node.checked === true || node.hasAttribute('checked') || node.getAttribute('aria-checked') === 'true'));
  function named(node, names) {
    const aria = (node.getAttribute('aria-label') || '').trim();
    return names.includes(node.innerText.trim()) && (!aria || names.includes(aria));
  }
  function finalButton(root) {
    return unique(root, 'ytcp-button#done-button', node => visible(node) && named(node, ['Save','SAVE','保存']));
  }
  function nextButton(root) {
    return unique(root, 'ytcp-button#next-button', node => visible(node) && enabled(node) &&
      !node.matches('#done-button') && [null,'button'].includes(node.getAttribute('role')) &&
      [null,'button'].includes(node.getAttribute('type')) && named(node, ['Next','NEXT','次へ']));
  }
  function nextMeaning(node) {
    return JSON.stringify([node.localName, ...['id','role','type','aria-label','aria-haspopup','title','href',
      'formaction','formmethod','formtarget'].map(name => node.getAttribute(name)), node.innerText]);
  }
  function stage(root) {
    const selector = unique(root, 'ytcp-video-visibility-select', visible);
    if (selector) return {kind:'visibility', node:selector};
    for (const [kind, name] of [['details','ytcp-video-metadata-editor'], ['elements','ytcp-video-elements'], ['checks','ytcp-video-checks']]) {
      const node = unique(root, name, visible);
      if (node) return {kind, node};
    }
    return null;
  }
  function hit(node) {
    if (!visible(node) || !enabled(node)) return false;
    const rectangle = node.getBoundingClientRect();
    const x = rectangle.left + rectangle.width / 2, y = rectangle.top + rectangle.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
    const top = document.elementFromPoint(x, y);
    return top === node || Boolean(top && node.contains(top));
  }
  async function guard(node, intact) {
    if (!hit(node) || !intact()) return false;
    const before = node.getBoundingClientRect();
    await tick(); await tick();
    const after = node.getBoundingClientRect();
    return hit(node) && intact() && ['x','y','width','height'].every(key => Math.abs(before[key] - after[key]) < 1);
  }
  async function waitFor(predicate, intact, timeout = 1500) {
    const deadline = performance.now() + timeout;
    while (intact() && performance.now() < deadline) {
      if (predicate()) return true;
      await tick();
    }
    return intact() && predicate();
  }
  function metadataValid(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).sort().join(',') !== 'audience,description,tags,title,visibility') return false;
    return typeof value.title === 'string' && [...value.title].length > 0 && [...value.title].length <= 100 &&
      typeof value.description === 'string' && [...value.description].length <= 5000 &&
      ![...(value.title + value.description)].some(character => character.length === 1 &&
        character.charCodeAt(0) >= 0xd800 && character.charCodeAt(0) <= 0xdfff) &&
      !/[<>\u0000-\u0008\u000b-\u001f\u007f]/.test(value.title + value.description) &&
      Array.isArray(value.tags) && value.tags.length === 0 && value.visibility === 'private' &&
      value.audience === 'not_made_for_kids';
  }
  function pinValid(params) {
    return params && typeof params === 'object' && !Array.isArray(params) && params.platform === 'youtube' &&
      Number.isInteger(params.tab_id) && params.tab_id >= 0 && typeof params.run_id === 'string' && ID.test(params.run_id) &&
      typeof params.expected_account_ref === 'string' && CHANNEL.test(params.expected_account_ref) &&
      (params.expected_document_id === undefined || (typeof params.expected_document_id === 'string' &&
        params.expected_document_id.length > 0 && params.expected_document_id.length <= 128));
  }
  function runMatches(run, params) {
    return run && run.tab === params.tab_id && run.account === params.expected_account_ref &&
      run.url === location.href && run.document === document && run.dialog === dialog() && run.dialog.isConnected;
  }
  function emptyTags(root) {
    // Channel defaults can supply tags. An absent advanced editor is not
    // evidence that tags are empty; the operation fails closed in that case.
    const container = unique(root, 'ytcp-video-metadata-editor-advanced ytcp-form-input-container#tags-container', node => node.isConnected);
    if (!container || container.querySelector('ytcp-chip')) return false;
    const bar = unique(container, 'ytcp-free-text-chip-bar', node => node.isConnected);
    const input = bar && unique(bar, 'input#text-input', node => node.isConnected);
    return Boolean(input && input.value === '');
  }
  function showMore(root) {
    const wrapper = unique(root, 'ytcp-video-metadata-editor#details ytcp-button#toggle-button', node =>
      visible(node) && enabled(node) && ['Show more','SHOW MORE','すべて表示'].includes(node.innerText.trim()) &&
      ['','Show more','SHOW MORE','詳細設定を表示'].includes((node.getAttribute('aria-label') || '').trim()) &&
      [null,'button'].includes(node.getAttribute('role')) && [null,'button'].includes(node.getAttribute('type')));
    if (!wrapper) return null;
    const button = unique(wrapper, 'button', node => visible(node) && enabled(node) && !node.closest('form') &&
      [null,'button'].includes(node.getAttribute('role')) && [null,'button'].includes(node.getAttribute('type')) &&
      ['Show more','SHOW MORE','詳細設定を表示'].includes((node.getAttribute('aria-label') || '').trim()) &&
      ['Show more','SHOW MORE','すべて表示'].includes(node.innerText.trim()));
    return button ? {wrapper,button} : null;
  }
  function snapshot(node) { return {node, html:node.innerHTML, text:node.innerText}; }
  function readSnapshot(saved) {
    if (!saved?.node.isConnected || saved.node.innerHTML !== saved.html) return null;
    // Studio keeps the Details nodes hidden during Visibility. Preserve the
    // exact rendered text read while visible, only if the same DOM bytes remain.
    return visible(saved.node) ? saved.node.innerText : saved.text;
  }
  function metadataStillMatches(run, root, metadata, includeFinal = true) {
    const visibility = stage(root), privateRadio = visibility && radio(visibility.node, 'PRIVATE');
    const kidsNo = radio(root, 'VIDEO_MADE_FOR_KIDS_NOT_MFK', false), kidsYes = radio(root, 'VIDEO_MADE_FOR_KIDS_MFK', false);
    const audienceNodes = Array.from(root.querySelectorAll('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"],tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_MFK"]'));
    const selectedAudience = audienceNodes.filter(checked);
    const selectedVisibility = visibility?.node && Array.from(visibility.node.querySelectorAll('tp-yt-paper-radio-button[name]')).filter(checked);
    return readSnapshot(run.title) === metadata.title && readSnapshot(run.description) === metadata.description &&
      visibility?.kind === 'visibility' && checked(privateRadio) && selectedVisibility.length === 1 &&
      Boolean(kidsNo && kidsYes) && audienceNodes.length === 2 && selectedAudience.length === 1 && selectedAudience[0] === kidsNo &&
      checked(kidsNo) && !checked(kidsYes) && emptyTags(root) &&
      (!includeFinal || visible(finalButton(root)) && enabled(finalButton(root)));
  }
  function fileInput(root, mime) {
    return unique(root, 'input[type="file"]', node => {
      if (!node.isConnected || node.ownerDocument !== document || !enabled(node) || node.files.length || node.multiple) return false;
      const accepts = String(node.accept).split(',').map(value => value.trim().toLowerCase());
      const extension = mime === 'video/mp4' ? '.mp4' : '.webm';
      return accepts.includes('video/*') || accepts.includes(mime) || accepts.includes(extension);
    });
  }
  function readiness(params) {
    const response = (error = '', account = '', composerReady = false, accountVerified = false) =>
      readinessResponse(documentId, account, composerReady, accountVerified, error);
    if (!readinessParamsValid(params)) return response('media_prepare_params_invalid');
    const account = channel();
    if (!account) return response(channelPath() ? 'media_prepare_account_unverified' : 'media_prepare_scope_rejected');
    if (account !== params.expected_account_ref) return response('media_prepare_account_mismatch', account);
    if (busy) return response('media_prepare_busy', account, false, true);
    // A completed, abandoned or unknown transfer is a retained tombstone.
    // Readiness never creates a run or retires this document's evidence.
    if (uploads.size) return response('media_prepare_new_document_required', account, false, true);
    if (new URL(location.href).searchParams.has('udvid')) return response('media_prepare_existing_draft_rejected', account, false, true);
    const root = dialog(), input = root && (fileInput(root, 'video/mp4') || fileInput(root, 'video/webm'));
    if (!root || !input || root.querySelectorAll('input[type="file"]').length !== 1 || stage(root) || editors(root))
      return response('media_prepare_composer_unavailable', account, false, true);
    const current = editors(root, false);
    if (current && (current.title.textContent !== '' || current.description.textContent !== ''))
      return response('media_prepare_existing_draft_rejected', account, false, true);
    return response('', account, true, true);
  }
  async function prepare(params) {
    if (params?.phase === 'readiness') return readiness(params);
    let run = runs.get(params?.run_id), performed = false, account = '', root = null;
    const response = (error = '', hashes = {}, prepared = false, unknown = false) => {
      if (unknown && run) run.unknown = true;
      return ({
      platform:'youtube', account_ref:account, account_ref_kind:'youtube_channel_id', document_id:documentId,
      metadata_sha256:hashes.metadata || null, title_sha256:hashes.title || null,
      description_sha256:hashes.description || null, asset_sha256:run?.asset?.sha || null,
      composer_ready:Boolean(root && (editors(root, false) || fileInput(root, 'video/mp4') || fileInput(root, 'video/webm'))),
      final_confirmation_available:Boolean(root && stage(root)?.kind === 'visibility' && visible(finalButton(root)) && enabled(finalButton(root))),
      prepared, executed:performed, outcome_unknown:unknown, ...(error ? {error} : {}),
      });
    };
    const keys = new Set(['platform','phase','tab_id','run_id','expected_document_id','expected_account_ref','metadata','metadata_sha256']);
    if (!pinValid(params) || Object.keys(params).some(key => !keys.has(key)) || !['inspect','fill','verify'].includes(params.phase) ||
        (params.phase === 'inspect' ? params.metadata !== undefined || params.metadata_sha256 !== undefined :
          !params.expected_document_id || !metadataValid(params.metadata) || typeof params.metadata_sha256 !== 'string' || !SHA.test(params.metadata_sha256)))
      return response('media_prepare_params_invalid');
    if (busy) return response('media_prepare_busy');
    busy = true;
    try {
      account = channel();
      if (!account) return response(channelPath() ? 'media_prepare_account_unverified' : 'media_prepare_scope_rejected');
      if (account !== params.expected_account_ref) return response('media_prepare_account_mismatch');
      root = dialog();
      if (!root) return response('media_prepare_composer_unavailable');
      if (run && !runMatches(run, params)) return response('media_prepare_document_changed');
      if (!run) {
        if (new URL(location.href).searchParams.has('udvid')) return response('media_prepare_existing_draft_rejected');
        if (params.phase !== 'inspect') return response('media_prepare_run_mismatch');
        if (uploads.size) return response('media_prepare_new_document_required');
        if (runs.size >= 128) return response('media_prepare_run_limit');
        run = {tab:params.tab_id, account, url:location.href, document, dialog:root, fillStarted:false, unknown:false, asset:null};
        runs.set(params.run_id, run);
      }
      const intact = () => runMatches(run, params) && channel() === account;
      if (params.phase === 'inspect') {
        const current = editors(root);
        let observed = {};
        if (current) observed = {title:await hash(current.title.innerText), description:await hash(current.description.innerText)};
        else if (stage(root) && run.title && run.description) {
          // A lost fill reply can leave Studio on Visibility. Read the same
          // exact Details DOM without returning to it or advancing any step.
          const title = readSnapshot(run.title), description = readSnapshot(run.description);
          if (title === null || description === null) return response('media_prepare_metadata_mismatch');
          observed = {title:await hash(title), description:await hash(description)};
          if (readSnapshot(run.title) !== title || readSnapshot(run.description) !== description)
            return response('media_prepare_metadata_mismatch');
        } else if (!fileInput(root, 'video/mp4') && !fileInput(root, 'video/webm')) return response('media_prepare_composer_unavailable');
        return intact() ? response('', observed) : response('media_prepare_document_changed');
      }
      const metadataHash = await hash(canonical(params.metadata));
      if (metadataHash !== params.metadata_sha256) return response('media_prepare_metadata_hash_mismatch');
      if (!run.asset?.filesVerified || run.asset.metadataHash !== metadataHash) return response('media_prepare_asset_unverified');
      if (params.phase === 'fill' && run.fillStarted)
        return response(run.unknown ? 'media_prepare_outcome_unknown' : 'media_prepare_fill_already_started');
      if (params.phase === 'fill') {
        const current = editors(root), details = stage(root);
        if (!current || details?.kind !== 'details') return response('media_prepare_composer_unavailable');
        const existingTitle = current.title.innerText, existingDescription = current.description.innerText;
        const ownTitle = run.asset.filename.replace(/\.(?:mp4|webm)$/, '');
        if ((existingTitle && existingTitle !== params.metadata.title && existingTitle !== ownTitle) ||
            (existingDescription && existingDescription !== params.metadata.description)) return response('media_prepare_existing_draft_rejected');
        const notKids = radio(root, 'VIDEO_MADE_FOR_KIDS_NOT_MFK');
        if (!notKids) return response('media_prepare_metadata_controls_unavailable');
        // Studio lazily creates advanced controls. Expand only its observed
        // display toggle; absence or ambiguity must never mean empty tags.
        if (!root.querySelector('ytcp-video-metadata-editor-advanced ytcp-form-input-container#tags-container')) {
          const more = showMore(root), meaning = more && nextMeaning(more.button);
          const detailsPinned = () => {
            const fresh = editors(root);
            return intact() && stage(root)?.node === details.node && fresh?.title === current.title &&
              fresh.description === current.description && current.title.innerText === existingTitle &&
              current.description.innerText === existingDescription;
          };
          if (!more || !await guard(more.button, detailsPinned) || showMore(root)?.button !== more.button ||
              showMore(root)?.wrapper !== more.wrapper || nextMeaning(more.button) !== meaning || !detailsPinned())
            return response('media_prepare_metadata_controls_unavailable');
          run.fillStarted = true; performed = true;
          more.button.click();
          if (!await waitFor(() => Boolean(root.querySelector('ytcp-video-metadata-editor-advanced ytcp-form-input-container#tags-container')),
              detailsPinned)) {
            run.unknown = true; return response('media_prepare_metadata_controls_unavailable', {}, false, true);
          }
        }
        if (!emptyTags(root)) {
          run.unknown = performed;
          return response('media_prepare_metadata_controls_unavailable', {}, false, performed);
        }
        // Mark before the first editing/click operation. A changed DOM, lost
        // response, or partial step sequence must never cause another fill.
        run.fillStarted = true;
        const editorPinned = node => {
          const fresh = editors(root);
          return intact() && node.isConnected && node.ownerDocument === document &&
            node.closest('ytcp-uploads-dialog') === root && fresh?.title === current.title &&
            fresh.description === current.description && (node === current.title || node === current.description);
        };
        const selectionInside = node => {
          const selection = getSelection();
          if (document.activeElement !== node || !selection || selection.rangeCount !== 1 ||
              !node.contains(selection.anchorNode) || !node.contains(selection.focusNode)) return false;
          const range = selection.getRangeAt(0);
          return node.contains(range.startContainer) && node.contains(range.endContainer) && node.contains(range.commonAncestorContainer);
        };
        const write = async (node, value, expectedOld) => {
          if (!editorPinned(node) || node.innerText !== expectedOld) return false;
          if (node.innerText === value) return true;
          if (!await guard(node, intact) || !editorPinned(node) || node.innerText !== expectedOld) return false;
          node.focus({preventScroll:true});
          if (!editorPinned(node) || node.innerText !== expectedOld) return false;
          const selection = getSelection(), range = document.createRange();
          range.selectNodeContents(node); selection.removeAllRanges(); selection.addRange(range);
          const input = (command, text) => {
            // Input listeners can synchronously move focus or Selection.
            // Recheck before every native edit, without an await before it.
            if (!editorPinned(node) || !selectionInside(node)) return false;
            performed = true;
            const accepted = document.execCommand(command, false, text);
            return accepted && editorPinned(node) && selectionInside(node);
          };
          // Native insertText turns consecutive empty lines into extra visual
          // breaks in Chromium. Insert each reviewed line with a native line
          // break instead; no HTML normalization or hash approximation.
          if (!input('delete', null)) return false;
          const lines = value.split('\n');
          for (let index = 0; index < lines.length; index++) {
            if (index && !input('insertLineBreak', null)) return false;
            if (lines[index] && !input('insertText', lines[index])) return false;
          }
          await tick(); await tick();
          return intact() && node.innerText === value;
        };
        if (!await write(current.title, params.metadata.title, existingTitle) ||
            !await write(current.description, params.metadata.description, existingDescription)) {
          run.unknown = performed; return response('media_prepare_metadata_mismatch', {}, false, performed);
        }
        if (!checked(notKids)) {
          if (!await guard(notKids, intact)) return response('media_prepare_control_blocked', {}, false, performed);
          performed = true; notKids.click(); await tick(); await tick();
          if (!intact() || !checked(notKids)) { run.unknown = true; return response('media_prepare_audience_mismatch', {}, false, true); }
        }
        run.title = snapshot(current.title); run.description = snapshot(current.description);
        for (const [expected, destination] of [['details','elements'],['elements','checks'],['checks','visibility']]) {
          const step = stage(root), next = nextButton(root), meaning = next && nextMeaning(next);
          // Only these three code-owned review steps may advance. The final
          // done-button is never a click target, even if its label says Next.
          if (!intact() || step?.kind !== expected || !next || !await guard(next, intact)) {
            run.unknown = performed; return response('media_prepare_review_navigation_unavailable', {}, false, performed);
          }
          // Re-resolve the exact control and its meaning after the last await.
          // A reused ID, moved node, final button or replacement is not Next.
          const freshStep = stage(root);
          if (!intact() || nextButton(root) !== next || !next.isConnected || next.ownerDocument !== document ||
              next.closest('ytcp-uploads-dialog') !== root || !next.matches('ytcp-button#next-button') || next.matches('#done-button') ||
              freshStep?.kind !== expected || freshStep.node !== step.node || nextMeaning(next) !== meaning) {
            run.unknown = performed; return response('media_prepare_review_navigation_unavailable', {}, false, performed);
          }
          performed = true; next.click();
          if (!await waitFor(() => stage(root)?.kind === destination, intact, 1000)) {
            run.unknown = true; return response(intact() ? 'media_prepare_review_navigation_unavailable' : 'media_prepare_document_changed', {}, false, true);
          }
        }
        const visibility = stage(root), privateRadio = visibility && radio(visibility.node, 'PRIVATE');
        if (visibility?.kind !== 'visibility' || !privateRadio) { run.unknown = true; return response('media_prepare_visibility_unavailable', {}, false, true); }
        if (!checked(privateRadio)) {
          if (!await guard(privateRadio, intact)) { run.unknown = true; return response('media_prepare_control_blocked', {}, false, true); }
          performed = true; privateRadio.click(); await tick(); await tick();
        }
      }
      const readTitle = readSnapshot(run.title), readDescription = readSnapshot(run.description);
      const hashes = {metadata:metadataHash, title:readTitle === null ? null : await hash(readTitle), description:readDescription === null ? null : await hash(readDescription)};
      // Read all settings after the final asynchronous hash. Old detached
      // controls cannot attest the current visibility or audience selection.
      if (!intact() || readTitle !== params.metadata.title || readDescription !== params.metadata.description ||
          readSnapshot(run.title) !== readTitle || readSnapshot(run.description) !== readDescription ||
          !metadataStillMatches(run, root, params.metadata, false)) {
        run.unknown ||= performed; return response('media_prepare_metadata_mismatch', hashes, false, performed);
      }
      if (!visible(finalButton(root)) || !enabled(finalButton(root))) return response('media_prepare_final_confirmation_unavailable', hashes);
      if (params.phase === 'verify') run.verifiedMetadata = JSON.parse(canonical(params.metadata));
      return response('', hashes, params.phase === 'verify');
    } catch {
      if (run && performed) run.unknown = true;
      return response('media_prepare_outcome_unknown', {}, false, performed);
    } finally { busy = false; }
  }
  const assetPinKeys = ['platform','tab_id','run_id','expected_document_id','expected_account_ref','metadata_sha256','scope_hash','transfer_id'];
  const assetPin = params => JSON.stringify(assetPinKeys.map(key => params[key]));
  const videoMagic = (value, mime) => mime === 'video/mp4' ? value.length >= 16 &&
    [102,116,121,112].every((n,index) => value[index+4] === n) &&
    new DataView(value.buffer,value.byteOffset,value.byteLength).getUint32(0) >= 16 &&
    ![113,116,32,32].every((n,index) => value[index+8] === n) :
    value.length >= 8 && [26,69,223,163].every((n,index) => value[index] === n) &&
    new TextDecoder().decode(value.slice(0,Math.min(value.length,4096))).includes('webm');
  async function prepareAsset(phase, params) {
    let state = uploads.get(params?.transfer_id), performed = false;
    const response = (error = '', accepted = false, filesVerified = false, attached = false, unknown = false) => ({
      transfer_id:params?.transfer_id || '', scope_hash:params?.scope_hash || '', document_id:documentId,
      account_ref:channel(), asset_sha256:state?.sha || params?.asset_sha256 || '', offset:state?.offset || 0,
      accepted, dispatched:performed, files_verified:filesVerified, attachment_visible:attached,
      executed:performed, outcome_unknown:unknown, ...(error ? {error} : {}),
    });
    const keys = new Set([...assetPinKeys, ...(['begin','inspect'].includes(phase) ? ['asset_sha256','size_bytes','mime_type'] : phase === 'append' ? ['offset','base64'] : [])]);
    if (!pinValid(params) || Object.keys(params).some(key => !keys.has(key)) || !params.expected_document_id ||
        !['begin','append','finish','inspect'].includes(phase) || typeof params.transfer_id !== 'string' || !ID.test(params.transfer_id) ||
        !['scope_hash','metadata_sha256'].every(key => typeof params[key] === 'string' && SHA.test(params[key]))) return response('media_prepare_asset_params_invalid');
    if (busy) return response('media_prepare_busy');
    busy = true;
    try {
      if (channel() !== params.expected_account_ref) return response('media_prepare_account_mismatch');
      const run = runs.get(params.run_id), root = dialog();
      if (phase === 'begin' && state?.dispatched && state.pin === assetPin(params))
        return response('media_prepare_asset_outcome_unknown', false, false, false, true);
      if (phase === 'begin' && !state && uploads.size) {
        if (Array.from(uploads.values()).some(upload => upload.run === run && upload.dispatched))
          return response('media_prepare_asset_outcome_unknown', false, false, false, true);
        return response('media_prepare_new_document_required');
      }
      if (!runMatches(run, params)) return response('media_prepare_run_mismatch');
      const intact = () => runMatches(run, params) && channel() === params.expected_account_ref;
      if (phase === 'begin') {
        if (state || uploads.size || run.asset || !Number.isInteger(params.size_bytes) || params.size_bytes < 1 || params.size_bytes > MAX_VIDEO_BYTES ||
            !['video/mp4','video/webm'].includes(params.mime_type) || typeof params.asset_sha256 !== 'string' || !SHA.test(params.asset_sha256))
          return response('media_prepare_asset_begin_rejected');
        const input = fileInput(root, params.mime_type);
        if (!input || editors(root)) return response('media_prepare_asset_input_unavailable');
        state = {pin:assetPin(params), input, inputMeaning:JSON.stringify([input.type,input.id,input.accept,input.multiple]), root, run, mime:params.mime_type, size:params.size_bytes, sha:params.asset_sha256,
          metadataHash:params.metadata_sha256, offset:0, bytes:new Uint8Array(params.size_bytes), dispatched:false};
        uploads.set(params.transfer_id, state); return response('', true);
      }
      if (!state || state.pin !== assetPin(params)) return response('media_prepare_asset_run_mismatch');
      if (phase === 'inspect') {
        if (params.asset_sha256 !== state.sha || params.size_bytes !== state.size || params.mime_type !== state.mime || !state.dispatched ||
            !intact() || !state.input.isConnected || state.input.ownerDocument !== document || state.input.files.length !== 1 ||
            JSON.stringify([state.input.type,state.input.id,state.input.accept,state.input.multiple]) !== state.inputMeaning)
          return response('media_prepare_asset_unverified');
        const observed = state.input.files[0];
        if (observed.size !== state.size || observed.type !== state.mime) return response('media_prepare_asset_unverified');
        const observedBytes = new Uint8Array(await observed.arrayBuffer());
        const valid = observed.name === `asset-${state.sha.slice(0,12)}.${state.mime === 'video/mp4' ? 'mp4' : 'webm'}` &&
          videoMagic(observedBytes, state.mime) && await hash(observedBytes) === state.sha;
        const attached = Boolean(editors(root, false) && stage(root));
        if (!valid || !intact() || state.input.files.length !== 1 || state.input.files[0] !== observed || !attached ||
            JSON.stringify([state.input.type,state.input.id,state.input.accept,state.input.multiple]) !== state.inputMeaning ||
            (run.verifiedMetadata && !metadataStillMatches(run, root, run.verifiedMetadata)))
          return response('media_prepare_asset_unverified');
        return response('', true, true, true);
      }
      if (state.dispatched) return response('media_prepare_asset_outcome_unknown', false, false, false, true);
      if (!intact() || !state.input.isConnected || state.input.ownerDocument !== document || state.input.files.length || !enabled(state.input) ||
          JSON.stringify([state.input.type,state.input.id,state.input.accept,state.input.multiple]) !== state.inputMeaning ||
          fileInput(root, state.mime) !== state.input)
        return response('media_prepare_document_changed');
      if (phase === 'append') {
        if (!Number.isInteger(params.offset) || params.offset !== state.offset || typeof params.base64 !== 'string' ||
            !params.base64 || params.base64.length > 349528 || !/^[A-Za-z0-9+/]*={0,2}$/.test(params.base64)) return response('media_prepare_asset_chunk_rejected');
        let decoded;
        try { decoded = atob(params.base64); } catch { return response('media_prepare_asset_chunk_rejected'); }
        if (!decoded.length || decoded.length > MAX_CHUNK_BYTES || state.offset + decoded.length > state.size || btoa(decoded) !== params.base64)
          return response('media_prepare_asset_chunk_rejected');
        for (let index = 0; index < decoded.length; index++) state.bytes[state.offset+index] = decoded.charCodeAt(index);
        state.offset += decoded.length; return response('', true);
      }
      if (state.offset !== state.size) return response('media_prepare_asset_incomplete');
      if (await hash(state.bytes) !== state.sha || !videoMagic(state.bytes, state.mime)) return response('media_prepare_asset_integrity_rejected');
      if (!intact() || state.input.files.length || fileInput(root, state.mime) !== state.input ||
          JSON.stringify([state.input.type,state.input.id,state.input.accept,state.input.multiple]) !== state.inputMeaning)
        return response('media_prepare_document_changed');
      const extension = state.mime === 'video/mp4' ? 'mp4' : 'webm';
      const file = new File([state.bytes], `asset-${state.sha.slice(0,12)}.${extension}`, {type:state.mime});
      const transfer = new DataTransfer(); transfer.items.add(file);
      // No picker, platform request, remote URL or caller filename. Assignment
      // and change are the only upload effects; consume before either happens.
      state.dispatched = true; performed = true;
      state.input.files = transfer.files;
      const assigned = state.input.files;
      const verified = assigned.length === 1 && assigned[0].size === state.size && assigned[0].type === state.mime &&
        assigned[0].name === file.name && await hash(await assigned[0].arrayBuffer()) === state.sha;
      if (!verified || !intact() || state.input.files.length !== 1 || state.input.files[0] !== assigned[0] ||
          JSON.stringify([state.input.type,state.input.id,state.input.accept,state.input.multiple]) !== state.inputMeaning)
        return response('media_prepare_asset_files_unverified', false, false, false, true);
      run.asset = {sha:state.sha, metadataHash:state.metadataHash, filename:file.name, filesVerified:true};
      state.input.dispatchEvent(new Event('change', {bubbles:true})); state.bytes = null;
      await waitFor(() => Boolean(editors(root) && stage(root)?.kind === 'details'), intact);
      const attached = Boolean(editors(root) && stage(root)?.kind === 'details');
      if (!intact() || !attached) return response('media_prepare_asset_attachment_unverified', false, true, attached, true);
      return response('', true, true, true);
    } catch {
      if (state?.dispatched) state.bytes = null;
      return response('media_prepare_asset_outcome_unknown', false, false, false, performed);
    } finally { busy = false; }
  }
  Object.defineProperty(prepare, 'version', {value:4});
  Object.defineProperty(prepare, 'readiness_revision', {value:4});
  Object.defineProperty(prepare, 'identity', {value:accountIdentity});
  Object.defineProperty(prepareAsset, 'version', {value:4});
  globalThis.__aoitalkMediaPrepareYouTube = prepare;
  globalThis.__aoitalkMediaPrepareYouTubeAsset = prepareAsset;
})();
