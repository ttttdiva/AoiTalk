// Runs only in the extension's isolated world in the selected user's tab.
(() => {
  if (globalThis.__aoitalkBrowser?.version === 9 && globalThis.__aoitalkBrowser?.materialGuardVersion === 3) return;
  // randomUUID is unavailable on ordinary HTTP pages; getRandomValues is not.
  const nonce = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  const documentId = nonce().replaceAll('-', '').slice(0, 12);
  const stableIds = new WeakMap();
  let nextId = 0, targets = new Map();
  const selector = 'a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="combobox"],[role="textbox"],[role="option"],[role="menuitem"],[contenteditable="true"],[tabindex]';
  const excludedText = 'input,textarea,select,[contenteditable],script,style,[hidden],[aria-hidden="true"]';
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const tick = () => new Promise(resolve => {
    const timer = setTimeout(resolve, 60);
    requestAnimationFrame(() => { clearTimeout(timer); resolve(); });
  });
  const stableId = e => {
    if (!stableIds.has(e)) stableIds.set(e, `${documentId}_${++nextId}`);
    return stableIds.get(e);
  };
  const visible = e => e.isConnected && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' &&
    (!e.checkVisibility || e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
  const parentOf = e => e.parentElement || e.getRootNode()?.host;
  function enabled(e, action) {
    if (e.disabled || e.matches(':disabled')) return false;
    for (let node = e; node; node = parentOf(node)) {
      if (node.inert || node.getAttribute('aria-disabled') === 'true') return false;
    }
    return action !== 'type' || !(e.readOnly || e.getAttribute('aria-readonly') === 'true');
  }
  // Never include entered form values, including contenteditable/textarea text.
  function safeText(root, limit = 10000) {
    if (!root) return '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let text = '', visited = 0, node;
    while ((node = walker.nextNode()) && visited++ < 12000 && text.length < limit) {
      const parent = node.parentElement;
      if (parent && !parent.closest(excludedText) && parent.getClientRects().length &&
          getComputedStyle(parent).visibility !== 'hidden') text += ` ${node.textContent}`;
    }
    return text.replace(/\s+/g, ' ').trim().slice(0, limit);
  }
  function label(e) {
    const root = e.getRootNode();
    return (e.getAttribute('aria-label') ||
      (e.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => safeText(root.getElementById?.(id), 300)).join(' ').trim() ||
      Array.from(e.labels || []).map(l => safeText(l, 300)).join(' ') ||
      e.getAttribute('placeholder') || e.getAttribute('title') ||
      (e.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) ? '' : safeText(e, 1000)) ||
      e.name || '').trim();
  }
  function context(e, limit = 220) {
    const container = e.closest('tr,[role="row"],li,[role="listitem"],article,fieldset') || e.parentElement;
    return container && !['BODY', 'HTML'].includes(container.tagName) ? safeText(container, limit) : '';
  }
  function heading(e) {
    const group = e.closest('fieldset,section,article,[role="dialog"]');
    return safeText(group?.querySelector('legend,h1,h2,h3,h4,[role="heading"]'), 120);
  }
  function compatible(e, action) {
    const tag = e.tagName.toLowerCase(), type = e.type || '', role = e.getAttribute('role') || '';
    if (action === 'read') return false;
    if (action === 'type') return (['input', 'textarea'].includes(tag) &&
      !['hidden', 'checkbox', 'radio', 'submit', 'button', 'reset', 'file'].includes(type)) || e.isContentEditable;
    if (action === 'select') return tag === 'select';
    if (['check', 'uncheck'].includes(action)) return ['checkbox', 'radio'].includes(type) || ['checkbox', 'radio'].includes(role);
    return true;
  }
  function meaning(e) {
    // No entered values. Row/label/href changes on a reused node invalidate it.
    return JSON.stringify([e.tagName, e.type, e.getAttribute('role'), label(e),
      e.getAttribute('href'), context(e, 2000), heading(e), e.getAttribute('aria-labelledby')]);
  }
  const canonical = value => JSON.stringify(value, (_key, item) =>
    item && typeof item === 'object' && !Array.isArray(item) ?
      Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  // Secret-bearing submission fields never leave the isolated world. A fresh
  // document-local salt prevents dictionary checks against the exported digest.
  const hiddenSalt = nonce(), hiddenSnapshots = new WeakMap();
  function hiddenPayload(form) {
    const fields = [];
    for (const control of form.elements) if (control.type === 'hidden') {
      if (control.tagName !== 'INPUT' || fields.length >= 128 || control.value.length > 16000 ||
          control.name.length > 1200) return null;
      fields.push({stable_id: stableId(control), name: control.name, value: control.value,
        disabled: control.disabled || control.matches(':disabled')});
    }
    const raw = canonical(fields);
    return new TextEncoder().encode(raw).length <= 65536 ? raw : null;
  }
  async function materialObservation(snapshot, identity, records) {
    const material_states = {};
    for (const [id, record] of records) {
      const form = record.node.form;
      if (!form || record.node.type !== 'submit' || !crypto.subtle) continue;
      const raw = hiddenPayload(form);
      if (raw === null) continue;
      if (hiddenSnapshots.get(form)?.raw !== raw) {
        try {
          const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
            new TextEncoder().encode(hiddenSalt + raw))), n => n.toString(16).padStart(2, '0')).join('');
          // A mutation while hashing cannot produce a complete observation.
          if (hiddenPayload(form) !== raw) { hiddenSnapshots.delete(form); continue; }
          hiddenSnapshots.set(form, {raw, digest});
        } catch { hiddenSnapshots.delete(form); continue; }
      }
      const state = materialState(record.node, identity);
      if (state) material_states[id] = state;
    }
    return {...snapshot, material_states};
  }
  const materialSecret = /password|passwd|secret|token|one.?time|\botp\b|\btotp\b|verification.?code|username|パスワード|暗証番号|認証コード|確認コード/i;
  function publicMaterialUrl(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password &&
        !url.search && !url.hash;
    } catch { return false; }
  }
  function materialState(e, identity) {
    // Deliberately narrow: only fully inspectable native forms in the top
    // document. Incomplete/custom/iframe forms cannot authorize a write.
    const form = e.form;
    if (!identity || window !== window.top || !Number.isInteger(identity.tab_id) ||
        !form || !form.isConnected || document.forms.length !== 1 || e.type !== 'submit' || !publicMaterialUrl(location.href) ||
        !publicMaterialUrl(form.action) || !publicMaterialUrl(e.formAction || form.action) ||
        document.querySelector('iframe,frame,object,embed') ||
        document.querySelectorAll('*').length > 12000 ||
        Array.from(document.querySelectorAll('*')).some(node => node.shadowRoot) ||
        document.querySelector('[contenteditable], [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"]') ||
        Array.from(document.querySelectorAll('input,select,textarea')).some(control => control.type !== 'hidden' && control.form !== form)) return null;
    const controls = [], controlMetadata = [];
    const hidden = hiddenSnapshots.get(form);
    // This is synchronous, including the final post-await act comparison.
    if (!hidden || hiddenPayload(form) !== hidden.raw) return null;
    for (const control of form.elements) {
      const tag = control.tagName, type = control.type;
      if (['BUTTON', 'FIELDSET', 'OUTPUT'].includes(tag) || ['submit', 'button', 'reset', 'hidden'].includes(type)) continue;
      const name = label(control);
      // Never read a secret value, even when it would cause an incomplete state.
      if (materialSecret.test([type, control.autocomplete, control.name, name].join(' ')) ||
          !visible(control) || !name || name.length > 1200 || controls.length >= 32) return null;
      let kind, value, display;
      if (tag === 'SELECT' && !control.multiple && control.selectedOptions.length === 1) {
        kind = 'select'; value = control.value; display = control.selectedOptions[0].label;
      } else if (tag === 'INPUT' && ['checkbox', 'radio'].includes(type)) {
        kind = type; value = control.value; display = String(control.checked);
      } else if (tag === 'TEXTAREA' || tag === 'INPUT' && ['text', 'date', 'time'].includes(type)) {
        kind = ['date', 'time'].includes(type) ? type : 'text'; value = display = control.value;
      } else return null;
      if (value.length > 1200 || display.length > 1200) return null;
      controls.push({stable_id: stableId(control), label: name, type: kind, value, display_value: display});
      controlMetadata.push({stable_id: stableId(control), name: control.name,
        disabled: control.disabled || control.matches(':disabled'),
        read_only: Boolean(control.readOnly), checked: ['checkbox', 'radio'].includes(type) ? control.checked : null,
        selected_disabled: tag === 'SELECT' ? Boolean(control.selectedOptions[0]?.disabled || control.selectedOptions[0]?.parentElement?.disabled) : null});
    }
    const textNodes = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let count = 0;
    while (textNodes.nextNode()) if (++count > 12000) return null;
    // Include public account/context outside the form too. Unknown concurrent
    // public changes conservatively invalidate approval instead of ignoring them.
    const facts = safeText(document.body, 8193);
    if (facts.length >= 8193 || !facts) return null;
    const dispatch = canonical({action: form.action, method: form.method, enctype: form.enctype,
      target: form.target, control_action: e.formAction, control_method: e.formMethod,
      control_enctype: e.formEnctype, control_target: e.formTarget, no_validate: e.formNoValidate || form.noValidate,
      submit_name: e.name, submit_value: e.value, controls_meta: controlMetadata,
      hidden_controls_digest: hidden.digest});
    const facts_text = `${facts}\n${dispatch}`;
    if (facts_text.length > 8192) return null;
    return {version: 1, complete: true, tab_id: identity.tab_id,
      document_id: identity.document_id || documentId, target_stable_id: stableId(e),
      url: location.href, facts_text, controls: controls.sort((a, b) => a.stable_id < b.stable_id ? -1 : a.stable_id > b.stable_id ? 1 : 0)};
  }
  function observe(action = 'read', materialIdentity = null) {
    const started = performance.now();
    targets = new Map();
    const observationId = nonce().replaceAll('-', '').slice(0, 12);
    if (action === 'inspect') {
      const candidates = {};
      let bytes = 0, eligible = 0, candidateCount = 0, truncated = false;
      const links = document.querySelectorAll('a[href*="/public/jobs/"]');
      for (const e of links) {
        eligible++;
        if (eligible > 600) { truncated = true; break; }
        const href = e.href || '';
        if (!href || !e.isConnected) continue;
        // Avoid layout/style resolution on very large marketplace pages.
        const rawLabel = String(e.getAttribute('aria-label') || e.textContent || '')
          .slice(0, 1200).replace(/\s+/g, ' ').trim();
        const container = e.closest('tr,[role="row"],li,[role="listitem"],article') || e.parentElement;
        const rawContext = String(container?.textContent || rawLabel)
          .slice(0, 5000).replace(/\s+/g, ' ').trim();
        const item = {
          tag: 'a', type: '', role: e.getAttribute('role') || '',
          label: rawLabel.slice(0, 220), context: rawContext.slice(0, 2000), heading: '',
          stable_id: stableId(e), enabled: true, options: [], href,
        };
        const size = new TextEncoder().encode(JSON.stringify(item)).length + 64;
        if (candidateCount >= 500 || bytes + size > 320000) { truncated = true; break; }
        candidates[`i_${observationId}_${candidateCount++}`] = item;
        bytes += size;
      }
      const bodyText = String(document.body?.textContent || '')
        .slice(0, 20000).replace(/\s+/g, ' ').trim().slice(0, 10000);
      return {
        url: location.href, title: document.title, text: bodyText, candidates,
        document_id: documentId, observation_id: observationId, ready: Boolean(document.body),
        coverage: {scanned_nodes: links.length, candidate_count: Object.keys(candidates).length,
          omitted_count: Math.max(0, eligible - Object.keys(candidates).length), truncated,
          unscanned: eligible < links.length, shadow_scope: 'none'},
        observation_ms: Math.round(performance.now() - started), candidate_bytes: bytes,
      };
    }
    const candidates = {}, walkers = [document.createTreeWalker(document, NodeFilter.SHOW_ELEMENT)];
    let nodes = 0, eligible = 0, bytes = 0, truncated = false;
    while (walkers.length) {
      if (nodes >= 12000) { truncated = true; break; }
      const e = walkers.at(-1).nextNode();
      if (!e) { walkers.pop(); continue; }
      nodes++;
      if (e.shadowRoot) walkers.push(document.createTreeWalker(e.shadowRoot, NodeFilter.SHOW_ELEMENT));
      if (!e.matches(selector) || !visible(e) || !compatible(e, action)) continue;
      eligible++;
      const options = e.tagName === 'SELECT' ? Array.from(e.options).slice(0, 2048).map(o => o.label) : [];
      const item = {
        tag: e.tagName.toLowerCase(), type: e.type || '', role: e.getAttribute('role') || '',
        label: label(e).slice(0, 220), context: context(e), heading: heading(e),
        stable_id: stableId(e), enabled: enabled(e, action), options,
      };
      if (e.tagName === 'SELECT') {
        item.option_count = e.options.length;
        item.options_truncated = e.options.length > options.length;
      }
      if (e.tagName === 'A') item.href = e.href;
      const size = new TextEncoder().encode(JSON.stringify(item)).length + 64;
      if (targets.size >= 2048 || bytes + size > 420000) { truncated = true; continue; }
      const id = `e_${observationId}_${targets.size}`;
      targets.set(id, {node: e, meaning: meaning(e), action, url: location.href});
      candidates[id] = item;
      bytes += size;
    }
    const snapshot = {
      url: location.href, title: document.title, text: safeText(document.body), candidates,
      document_id: documentId, observation_id: observationId, ready: Boolean(document.body),
      coverage: {scanned_nodes: nodes, candidate_count: targets.size,
        omitted_count: eligible - targets.size, truncated, unscanned: walkers.length > 0,
        shadow_scope: 'open_only'},
      observation_ms: Math.round(performance.now() - started), candidate_bytes: bytes,
    };
    return action === 'click' && materialIdentity ? materialObservation(snapshot, materialIdentity, new Map(targets)) : snapshot;
  }
  function containsComposed(e, hit) {
    for (let node = hit; node; node = parentOf(node)) if (node === e) return true;
    return false;
  }
  function hitAt(doc, x, y) {
    let hit = doc.elementFromPoint(x, y);
    while (hit?.shadowRoot) {
      const next = hit.shadowRoot.elementFromPoint(x, y);
      if (!next || next === hit) break;
      hit = next;
    }
    return hit;
  }
  function hitPoint(e) {
    for (const rect of e.getClientRects()) {
      const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
      const right = Math.min(innerWidth, rect.right), bottom = Math.min(innerHeight, rect.bottom);
      if (right <= left || bottom <= top) continue;
      for (const [fx, fy] of [[0.5, 0.5], [0.2, 0.2], [0.8, 0.2], [0.2, 0.8], [0.8, 0.8]]) {
        const x = left + (right - left) * fx, y = top + (bottom - top) * fy;
        if (containsComposed(e, hitAt(document, x, y))) return {x, y};
      }
    }
    return null;
  }
  function parentFrameGuard(point) {
    let child = window, x = point.x, y = point.y;
    try {
      while (child !== child.top) {
        const frame = child.frameElement;
        // Cross-origin parent geometry cannot be attested by this DOM route.
        if (!frame || !frame.isConnected) return 'edge_parent_frame_unverified';
        const parent = child.parent, rect = frame.getBoundingClientRect();
        const transform = parent.getComputedStyle(frame).transform;
        if (transform !== 'none' || Math.abs(rect.width - frame.offsetWidth) > 1 ||
            Math.abs(rect.height - frame.offsetHeight) > 1) return 'edge_parent_frame_unverified';
        x = rect.left + frame.clientLeft + x;
        y = rect.top + frame.clientTop + y;
        if (hitAt(parent.document, x, y) !== frame) return 'edge_target_covered';
        child = parent;
      }
    } catch { return 'edge_parent_frame_unverified'; }
    return '';
  }
  function current(record, action) {
    const e = record?.node;
    if (!e || !e.isConnected || e.ownerDocument !== document || record.url !== location.href ||
        record.action !== action || record.meaning !== meaning(e)) return 'edge_stale_element';
    if (!compatible(e, action) || !visible(e) || !enabled(e, action)) return 'edge_target_not_actionable';
    return '';
  }
  async function guard(record, action, waitMs) {
    const deadline = performance.now() + waitMs;
    let error = '';
    do {
      error = current(record, action);
      if (error === 'edge_stale_element') return error;
      if (!error) {
        const e = record.node;
        e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
        e.focus({preventScroll: true});
        const before = e.getBoundingClientRect();
        await tick(); await tick();
        error = current(record, action);
        const after = e.getBoundingClientRect();
        if (!error && ['x', 'y', 'width', 'height'].some(k => Math.abs(before[k] - after[k]) > 1))
          error = 'edge_target_moving';
        if (!error) {
          const point = hitPoint(e);
          error = point ? parentFrameGuard(point) : 'edge_target_covered';
          if (!error) return '';
        }
      }
      if (performance.now() < deadline) await pause(30);
    } while (performance.now() < deadline);
    return error || 'edge_target_not_actionable';
  }
  async function act({action, target, value = '', wait_ms = 900,
    expected_material_state, expected_material_state_digest, material_identity}) {
    const started = performance.now(), record = targets.get(target);
    const blocked = error => ({ok: false, error, executed: false, execution_status: 'blocked'});
    if (!['click', 'type', 'select', 'check', 'uncheck'].includes(action)) return blocked('edge_action_unknown');
    const guarded = expected_material_state !== undefined || expected_material_state_digest !== undefined;
    let expectedCanonical;
    if (guarded) {
      if (action !== 'click' || !expected_material_state || !material_identity ||
          !/^[a-f0-9]{64}$/.test(expected_material_state_digest || '') || !crypto.subtle) return blocked('material_state_changed');
      try {
        expectedCanonical = canonical(expected_material_state);
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
          new TextEncoder().encode(expectedCanonical))), n => n.toString(16).padStart(2, '0')).join('');
        if (hash !== expected_material_state_digest) return blocked('material_state_changed');
      } catch { return blocked('material_state_changed'); }
    }
    const initial = current(record, action);
    if (initial === 'edge_stale_element') return blocked(guarded ? 'material_state_changed' : initial);
    const error = await guard(record, action, Math.max(0, Math.min(1500, Number(wait_ms) || 0)));
    if (error) return blocked(guarded ? 'material_state_changed' : error);
    // No await between this last identity/hit test and dispatch.
    const e = record.node, point = hitPoint(e);
    const finalError = current(record, action) || (point ? parentFrameGuard(point) : 'edge_target_covered');
    if (finalError) return blocked(guarded ? 'material_state_changed' : finalError);
    // The hash was authenticated above. After the LAST await, resample all
    // conditions and compare canonical bytes in this same synchronous task.
    // No promise, browser IPC, or user event can interleave before dispatch.
    if (guarded && canonical(materialState(e, material_identity)) !== expectedCanonical)
      return blocked('material_state_changed');
    let expectedIndex = -1, performed = false;
    if (action === 'select') {
      const matches = Array.from(e.options).filter(o => o.label === value || o.value === value);
      if (matches.length !== 1) return blocked(matches.length ? 'edge_option_ambiguous' : 'edge_option_not_found');
      if (matches[0].disabled || matches[0].parentElement?.disabled) return blocked('edge_option_disabled');
      expectedIndex = matches[0].index;
    }
    if (action === 'click') {
      let changed = false;
      const effects = new MutationObserver(() => { changed = true; });
      effects.observe(document.body, {childList: true, subtree: true, characterData: true,
        attributes: true, attributeFilter: ['hidden', 'open', 'aria-expanded', 'aria-selected', 'aria-checked']});
      try {
        e.click();
        // Deliver synchronous SPA mutations without imposing a fixed sleep.
        await Promise.resolve();
        changed ||= effects.takeRecords().length > 0;
      } finally { effects.disconnect(); }
      return {ok: true, executed: true, execution_status: 'executed', effect_observed: changed,
        verification: {status: 'unknown', kind: 'click_effect'}, action_ms: Math.round(performance.now() - started)};
    }
    if (action === 'type') {
      if (e.isContentEditable) {
        const range = document.createRange(), selection = getSelection();
        range.selectNodeContents(e); selection.removeAllRanges(); selection.addRange(range);
        if (!document.execCommand(value ? 'insertText' : 'delete', false, value))
          return {ok: false, error: 'edge_contenteditable_input_unavailable', executed: null, execution_status: 'unknown'};
      } else {
        const prototype = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(e, value);
        e.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
        e.dispatchEvent(new Event('change', {bubbles: true}));
      }
      performed = true;
    } else if (action === 'select') {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex').set.call(e, expectedIndex);
      e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
      performed = true;
    } else {
      const checked = 'checked' in e ? e.checked : e.getAttribute('aria-checked') === 'true';
      if (checked !== (action === 'check')) { e.click(); performed = true; }
    }
    await tick(); await tick();
    const matched = e.isConnected && (action === 'type' ?
      (e.isContentEditable ? e.innerText : e.value) === value :
      action === 'select' ? e.selectedIndex === expectedIndex :
      ('checked' in e ? e.checked : e.getAttribute('aria-checked') === 'true') === (action === 'check'));
    return {
      ok: matched, ...(matched ? {} : {error: 'edge_value_not_retained'}),
      executed: performed, execution_status: matched ? 'verified' : 'executed',
      verification: {status: matched ? 'verified' : 'failed', kind: action === 'type' ? 'value' : action === 'select' ? 'selection' : 'checked',
        matched, target_ref: target, document_id: documentId, checked_at: Date.now()},
      action_ms: Math.round(performance.now() - started),
    };
  }
  // This closed operation prepares a text-only draft in the user's existing
  // account. It never resolves a general target or clicks a publish control.
  const mediaPaths = new Set(['/home', '/compose/post', '/compose/tweet']);
  const mediaHash = async text => Array.from(new Uint8Array(await crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(text))), n => n.toString(16).padStart(2, '0')).join('');
  function mediaScope() {
    return window === window.top && location.protocol === 'https:' &&
      location.hostname === 'x.com' && !location.port && !location.search && !location.hash &&
      mediaPaths.has(location.pathname);
  }
  function mediaAccount() {
    const switches = Array.from(document.querySelectorAll('[data-testid="SideNav_AccountSwitcher_Button"]'))
      .filter(e => visible(e) && enabled(e, 'read') &&
        (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button') && !e.closest('article'));
    if (switches.length !== 1) return null;
    // Only this account UI may attest the logged-in handle. Profile links,
    // body text, display names and numeric strings are not account evidence.
    const handles = new Set(Array.from(switches[0].querySelectorAll('[dir="ltr"],[data-testid="UserName"]'))
      .filter(e => visible(e)).map(e => (e.textContent || '').trim())
      .filter(value => /^@[a-zA-Z0-9_]{1,15}$/.test(value)).map(value => value.toLowerCase()));
    return handles.size === 1 ? {node: switches[0], ref: [...handles][0]} : null;
  }
  function mediaComposer(allowAttachment = false) {
    const editors = Array.from(document.querySelectorAll('[data-testid="tweetTextarea_0"]'))
      .filter(e => visible(e) && enabled(e, 'type') && e.isContentEditable &&
        e.getAttribute('role') === 'textbox' && e.ownerDocument === document);
    const buttons = Array.from(document.querySelectorAll('[data-testid="tweetButtonInline"],[data-testid="tweetButton"]'))
      .filter(visible);
    if (editors.length !== 1 || buttons.length !== 1) return null;
    if (document.querySelector('[data-testid="tweetTextarea_1"]')) return null;
    const editor = editors[0], button = buttons[0];
    let container = editor.closest('[role="dialog"],form');
    if (!container) {
      let ancestor = editor.parentElement;
      for (let depth = 0; ancestor && depth < 7; depth++, ancestor = ancestor.parentElement) {
        if (['BODY','HTML','MAIN'].includes(ancestor.tagName) || ancestor.getAttribute('role') === 'main') break;
        if (ancestor.contains(button)) { container = ancestor; break; }
      }
    }
    // A known same-composer boundary is required; this cannot fill a reply,
    // an attachment draft, or a textbox paired with another Post control.
    if (!container || !container.contains(button) || container.closest('article') ||
        container.querySelector('article,[data-testid="card.layoutLarge.media"],[data-testid="poll"]') ||
        (!allowAttachment && container.querySelector('[data-testid="attachments"],[data-testid="mediaPreview"],[data-testid="tweetPhoto"]'))) return null;
    return {editor, button, container};
  }
  function mediaText(editor) {
    // innerText follows rendered contenteditable line breaks. No trimming or
    // Unicode normalization is permitted for the reviewed payload hash.
    return editor.innerText;
  }
  async function mediaPrepareReadiness(params) {
    let account = null, composer = null, verified = false;
    const reply = (error = '', available = false) => ({platform:'x',account_ref:account?.ref || '',account_ref_kind:'x_handle',
      document_id:documentId,helper_revision:9,composer_ready:Boolean(composer),account_verified:verified,available,
      executed:false,publication_confirmed:false,outcome_unknown:false,...(error ? {error} : {})});
    const keys = new Set(['platform','tab_id','expected_account_ref']);
    if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).some(key => !keys.has(key)) ||
        Object.keys(params).length !== 3 || params.platform !== 'x' || !Number.isInteger(params.tab_id) || params.tab_id < 0 ||
        typeof params.expected_account_ref !== 'string' || !/^@[a-zA-Z0-9_]{1,15}$/.test(params.expected_account_ref))
      return reply('media_prepare_params_invalid');
    if (!mediaScope()) return reply('media_prepare_scope_rejected');
    const initialUrl = location.href, initialDocument = document;
    account = mediaAccount();
    if (!account) return reply('media_prepare_identity_unavailable');
    if (account.ref !== params.expected_account_ref.toLowerCase()) return reply('media_prepare_account_mismatch');
    verified = true;composer = mediaComposer();
    if (!composer) return reply('media_prepare_composer_unavailable');
    const point = hitPoint(composer.editor);
    if (!point || parentFrameGuard(point)) return reply('media_prepare_composer_blocked');
    const inputs = Array.from(composer.container.querySelectorAll('input[type="file"][data-testid="fileInput"]'));
    if (mediaText(composer.editor) !== '' || inputs.some(input => input.files.length))
      return reply('media_prepare_existing_draft_rejected');
    const input = mediaFileInput(composer);
    if (!input) return reply('media_prepare_asset_input_unavailable');
    const inputMeaning = mediaFileMeaning(input);
    const intact = () => mediaScope() && location.href === initialUrl && document === initialDocument &&
      account.node === mediaAccount()?.node && account.ref === mediaAccount()?.ref &&
      composer.editor === mediaComposer()?.editor && composer.container === mediaComposer()?.container &&
      composer.button === mediaComposer()?.button && mediaText(composer.editor) === '' &&
      input === mediaFileInput(composer) && mediaFileMeaning(input) === inputMeaning && !input.files.length &&
      Boolean(hitPoint(composer.editor));
    const emptyHash = await mediaHash('');
    if (!intact() || emptyHash !== 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855') {
      verified = false;return reply('media_prepare_document_changed');
    }
    return reply('',true);
  }
  async function mediaPrepareText(params) {
    let account = null, composer = null, executed = false;
    const reply = (error = '', textSha = null, prepared = false, unknown = false) => ({
      platform: 'x', account_ref: account?.ref || '', account_ref_kind: 'x_handle',
      document_id: documentId, text_sha256: textSha, composer_ready: Boolean(composer),
      final_confirmation_available: Boolean(composer && visible(composer.button) && enabled(composer.button, 'click')),
      prepared, executed, outcome_unknown: unknown, ...(error ? {error} : {}),
    });
    const allowed = new Set(['platform', 'phase', 'expected_account_ref', 'text', 'tab_id', 'run_id', 'expected_document_id']);
    if (!params || typeof params !== 'object' || Array.isArray(params) ||
        Object.keys(params).some(key => !allowed.has(key)) || params.platform !== 'x' ||
        !['inspect', 'fill', 'verify'].includes(params.phase) ||
        typeof params.expected_account_ref !== 'string' || !/^@[a-zA-Z0-9_]{1,15}$/.test(params.expected_account_ref) ||
        !Number.isInteger(params.tab_id) || params.tab_id < 0 ||
        typeof params.run_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(params.run_id) ||
        (params.phase === 'inspect' ? params.text !== undefined :
          typeof params.text !== 'string' || !params.text || new TextEncoder().encode(params.text).length > 16384 ||
          /[\u0000-\u0008\u000b-\u001f\u007f]/.test(params.text)) ||
        (params.phase !== 'inspect' && (typeof params.expected_document_id !== 'string' || !params.expected_document_id)))
      return reply('media_prepare_params_invalid');
    if (!mediaScope()) return reply('media_prepare_scope_rejected');
    const startedUrl = location.href, startedDocument = document;
    account = mediaAccount();
    if (!account) return reply('media_prepare_identity_unavailable');
    if (account.ref !== params.expected_account_ref.toLowerCase()) return reply('media_prepare_account_mismatch');
    composer = mediaComposer();
    if (!composer) return reply('media_prepare_composer_unavailable');
    const intact = () => mediaScope() && document === startedDocument && location.href === startedUrl &&
      composer.editor.isConnected && composer.editor.ownerDocument === startedDocument &&
      mediaComposer()?.editor === composer.editor && mediaAccount()?.node === account.node &&
      mediaAccount()?.ref === account.ref;
    let currentText = mediaText(composer.editor);
    if (new TextEncoder().encode(currentText).length > 16384) return reply('media_prepare_existing_draft_rejected');
    if (params.phase === 'inspect') {
      const hash = await mediaHash(currentText);
      return intact() ? reply('', hash) : reply('media_prepare_document_changed');
    }
    if (currentText && currentText !== params.text) return reply('media_prepare_existing_draft_rejected');
    if (params.phase === 'fill' && currentText !== params.text) {
      const record = {node: composer.editor, meaning: meaning(composer.editor), action: 'type', url: startedUrl};
      const error = await guard(record, 'type', 250);
      if (error || !intact() || mediaText(composer.editor) !== currentText)
        return reply(error ? 'media_prepare_composer_blocked' : 'media_prepare_document_changed');
      // No await between identity verification and this sole write. The
      // browser editing command fires normal input events, never keypress,
      // submit, click, paste, arbitrary script or a private platform request.
      const range = document.createRange(), selection = getSelection();
      range.selectNodeContents(composer.editor); selection.removeAllRanges(); selection.addRange(range);
      executed = true;
      try {
        if (!document.execCommand('insertText', false, params.text))
          return reply('media_prepare_input_unavailable', null, false, true);
      } catch { return reply('media_prepare_input_unavailable', null, false, true); }
      await tick(); await tick();
    }
    if (!intact()) return reply('media_prepare_document_changed', null, false, executed);
    currentText = mediaText(composer.editor);
    const hash = await mediaHash(currentText);
    if (!intact() || mediaText(composer.editor) !== currentText)
      return reply('media_prepare_document_changed', null, false, executed);
    if (currentText !== params.text) return reply('media_prepare_text_mismatch', hash, false, executed);
    if (params.phase === 'verify' && !enabled(composer.button, 'click'))
      return reply('media_prepare_final_confirmation_unavailable', hash);
    return reply('', hash, params.phase === 'verify');
  }
  const mediaUploads = new Map();
  const mediaAssetMimes = new Map([['image/jpeg','jpg'],['image/png','png'],['image/webp','webp']]);
  const mediaAssetPinKeys = ['platform','tab_id','run_id','expected_document_id','expected_account_ref','text_sha256','scope_hash','transfer_id'];
  const mediaAssetPin = params => JSON.stringify(mediaAssetPinKeys.map(key => params[key]));
  function mediaFileInput(composer) {
    const inputs = Array.from(composer.container.querySelectorAll('input[type="file"][data-testid="fileInput"]'))
      .filter(e => e.isConnected && e.ownerDocument === document && enabled(e, 'read') &&
        !e.files.length && String(e.accept || '').split(',').some(value => value.trim().toLowerCase().startsWith('image/')));
    return inputs.length === 1 ? inputs[0] : null;
  }
  const mediaFileMeaning = input => JSON.stringify([input.tagName,input.type,input.id,input.name,input.accept,
    input.getAttribute('role'),label(input),input.getAttribute('aria-labelledby'),input.getAttribute('data-testid')]);
  function mediaSelectedFileIntact(composer, input, file, inputMeaning, size, mime, name) {
    const inputs = Array.from(composer.container.querySelectorAll('input[type="file"][data-testid="fileInput"]'));
    return input.isConnected && input.ownerDocument === document && enabled(input,'read') &&
      mediaFileMeaning(input) === inputMeaning && inputs.length === 1 && inputs[0] === input &&
      input.files.length === 1 && input.files[0] === file && file.size === size && file.type === mime && file.name === name;
  }
  function mediaImageMagic(bytes, mime) {
    if (mime === 'image/jpeg') return bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
    if (mime === 'image/png') return bytes.length >= 8 && [137,80,78,71,13,10,26,10].every((value,index) => bytes[index] === value);
    return bytes.length >= 12 && [82,73,70,70].every((value,index) => bytes[index] === value) &&
      [87,69,66,80].every((value,index) => bytes[index+8] === value);
  }
  async function mediaPrepareAsset(phase, params) {
    let state = mediaUploads.get(params?.transfer_id), executed = false;
    const reply = (error = '', accepted = false, filesVerified = false, attached = false, unknown = false) => ({
      transfer_id: params?.transfer_id || '', scope_hash: params?.scope_hash || '',
      document_id: params?.expected_document_id || '', account_ref: params?.expected_account_ref || '',
      asset_sha256: state?.sha || params?.asset_sha256 || '', offset: state?.offset || 0,
      accepted, dispatched: executed, files_verified: filesVerified, attachment_visible: attached,
      executed, outcome_unknown: unknown, ...(error ? {error} : {}),
    });
    const allowed = new Set([...mediaAssetPinKeys,...(['begin','inspect'].includes(phase) ? ['asset_sha256','size_bytes','mime_type'] : phase === 'append' ? ['offset','base64'] : [])]);
    if (!params || typeof params !== 'object' || Array.isArray(params) || Object.keys(params).some(key => !allowed.has(key)) ||
        !['begin','append','finish','inspect'].includes(phase) || params.platform !== 'x' || !Number.isInteger(params.tab_id) || params.tab_id < 0 ||
        typeof params.run_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(params.run_id) ||
        typeof params.transfer_id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(params.transfer_id) ||
        typeof params.expected_document_id !== 'string' || !params.expected_document_id || params.expected_document_id.length > 128 ||
        typeof params.expected_account_ref !== 'string' || !/^@[a-z0-9_]{1,15}$/.test(params.expected_account_ref) ||
        !['text_sha256','scope_hash'].every(key => typeof params[key] === 'string' && /^[a-f0-9]{64}$/.test(params[key])))
      return reply('media_prepare_asset_params_invalid');
    if (!mediaScope()) return reply('media_prepare_scope_rejected');
    const account = mediaAccount();
    if (!account || account.ref !== params.expected_account_ref) return reply('media_prepare_account_mismatch');
    const composer = mediaComposer(phase === 'inspect' || phase === 'finish' && Boolean(state?.dispatched));
    if (!composer) return reply('media_prepare_composer_unavailable');
    const inspectedText = mediaText(composer.editor), currentHash = await mediaHash(inspectedText);
    if (currentHash !== params.text_sha256) return reply('media_prepare_text_mismatch');
    const url = location.href, pin = mediaAssetPin(params);
    if (phase === 'inspect') {
      if (!Number.isInteger(params.size_bytes) || params.size_bytes < 1 || params.size_bytes > 26214400 ||
          !mediaAssetMimes.has(params.mime_type) || typeof params.asset_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(params.asset_sha256) ||
          state && (state.pin !== pin || state.sha !== params.asset_sha256 || state.size !== params.size_bytes || state.mime !== params.mime_type))
        return reply('media_prepare_asset_params_invalid');
      const input = Array.from(composer.container.querySelectorAll('input[type="file"][data-testid="fileInput"]'))
        .filter(node => node.isConnected && node.ownerDocument === document && enabled(node,'read'));
      if (input.length !== 1 || input[0].files.length !== 1) return reply('media_prepare_asset_files_unverified');
      const file = input[0].files[0];
      if (file.size !== params.size_bytes || file.type !== params.mime_type ||
          file.name !== `asset-${params.asset_sha256.slice(0,12)}.${mediaAssetMimes.get(params.mime_type)}`)
        return reply('media_prepare_asset_files_unverified');
      const inputMeaning = mediaFileMeaning(input[0]);
      const intact = () => mediaScope() && location.href === url && mediaAccount()?.node === account.node &&
        mediaAccount()?.ref === params.expected_account_ref && mediaComposer(true)?.editor === composer.editor &&
        mediaComposer(true)?.container === composer.container && mediaComposer(true)?.button === composer.button &&
        mediaText(composer.editor) === inspectedText && enabled(composer.button,'click') &&
        mediaSelectedFileIntact(composer,input[0],file,inputMeaning,params.size_bytes,params.mime_type,
          `asset-${params.asset_sha256.slice(0,12)}.${mediaAssetMimes.get(params.mime_type)}`);
      const fileBytes = new Uint8Array(await file.arrayBuffer());
      if (!intact()) return reply('media_prepare_asset_inspection_unverified');
      const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',fileBytes)), value => value.toString(16).padStart(2,'0')).join('');
      const verified = actual === params.asset_sha256 && mediaImageMagic(fileBytes,params.mime_type);
      const attached = () => Array.from(composer.container.querySelectorAll('[data-testid="attachments"],[data-testid="mediaPreview"],[data-testid="tweetPhoto"]')).some(visible);
      if (!verified || !intact() || !attached() || await mediaHash(mediaText(composer.editor)) !== params.text_sha256 ||
          !intact() || !attached())
        return reply('media_prepare_asset_inspection_unverified',false,verified && intact(),attached());
      if (!state) state = {sha:params.asset_sha256,size:params.size_bytes,mime:params.mime_type,offset:params.size_bytes};
      return reply('',true,true,true);
    }
    if (phase === 'begin') {
      for (const [key,value] of mediaUploads) if (!value.dispatched && value.expires < Date.now()) mediaUploads.delete(key);
      if (state || [...mediaUploads.values()].some(value => !value.dispatched) || !Number.isInteger(params.size_bytes) || params.size_bytes < 1 || params.size_bytes > 26214400 ||
          !mediaAssetMimes.has(params.mime_type) || typeof params.asset_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(params.asset_sha256))
        return reply('media_prepare_asset_begin_rejected');
      if (mediaAccount()?.node !== account.node || mediaAccount()?.ref !== params.expected_account_ref ||
          mediaComposer()?.editor !== composer.editor || location.href !== url)
        return reply('media_prepare_document_changed');
      const input = mediaFileInput(composer);
      if (!input) return reply('media_prepare_asset_input_unavailable');
      const accepts = String(input.accept).toLowerCase().split(',').map(value => value.trim());
      if (!accepts.includes('image/*') && !accepts.includes(params.mime_type)) return reply('media_prepare_asset_input_unavailable');
      state = {pin, input, meaning:meaning(input), composer:composer.editor, account:account.node, url,
        sha:params.asset_sha256, mime:params.mime_type, size:params.size_bytes, offset:0,
        bytes:new Uint8Array(params.size_bytes), dispatched:false, text:mediaText(composer.editor),expires:Date.now()+300000};
      for (const [key,value] of mediaUploads) if (value.dispatched) mediaUploads.delete(key);
      mediaUploads.set(params.transfer_id,state);
      return reply('',true);
    }
    if (!state || state.pin !== pin || state.dispatched) return reply('media_prepare_asset_run_mismatch');
    const intact = () => mediaScope() && location.href === state.url && state.input.isConnected &&
      state.input.ownerDocument === document && meaning(state.input) === state.meaning &&
      state.composer === mediaComposer()?.editor && state.account === mediaAccount()?.node &&
      mediaAccount()?.ref === params.expected_account_ref && !state.input.files.length;
    if (!intact()) return reply('media_prepare_document_changed');
    if (phase === 'append') {
      if (!Number.isInteger(params.offset) || params.offset !== state.offset || typeof params.base64 !== 'string' ||
          !params.base64 || params.base64.length > 349528 || !/^[A-Za-z0-9+/]*={0,2}$/.test(params.base64))
        return reply('media_prepare_asset_chunk_rejected');
      let decoded;
      try { decoded = atob(params.base64); } catch { return reply('media_prepare_asset_chunk_rejected'); }
      if (!decoded.length || decoded.length > 262144 || state.offset + decoded.length > state.size || btoa(decoded) !== params.base64)
        return reply('media_prepare_asset_chunk_rejected');
      for (let index = 0; index < decoded.length; index++) state.bytes[state.offset+index] = decoded.charCodeAt(index);
      state.offset += decoded.length;
      return reply('',true);
    }
    if (state.offset !== state.size) return reply('media_prepare_asset_incomplete');
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',state.bytes)), value => value.toString(16).padStart(2,'0')).join('');
    if (digest !== state.sha || !mediaImageMagic(state.bytes,state.mime)) return reply('media_prepare_asset_integrity_rejected');
    if (!intact() || await mediaHash(mediaText(composer.editor)) !== params.text_sha256 || !intact())
      return reply('media_prepare_document_changed');
    // Fixed, trusted file input only. No picker click, local path, arbitrary
    // selector, URL, cookie, platform endpoint, keypress or final publication.
    const file = new File([state.bytes],`asset-${state.sha.slice(0,12)}.${mediaAssetMimes.get(state.mime)}`,{type:state.mime});
    const transfer = new DataTransfer();transfer.items.add(file);
    const inputMeaning = mediaFileMeaning(state.input);
    state.dispatched = true;executed = true;
    try {
      state.input.files = transfer.files;
      state.bytes = null;
      const assignedFile = state.input.files[0];
      const selectedIntact = allowAttachment => mediaScope() && location.href === state.url &&
        mediaAccount()?.node === state.account && mediaAccount()?.ref === params.expected_account_ref &&
        mediaComposer(allowAttachment)?.editor === state.composer &&
        mediaComposer(allowAttachment)?.container === composer.container &&
        mediaComposer(allowAttachment)?.button === composer.button && mediaText(state.composer) === state.text &&
        mediaSelectedFileIntact(composer,state.input,assignedFile,inputMeaning,state.size,state.mime,file.name);
      if (!assignedFile || !selectedIntact(false))
        return reply('media_prepare_asset_files_unverified',false,false,false,true);
      const assignedBytes = new Uint8Array(await assignedFile.arrayBuffer());
      if (!selectedIntact(false)) return reply('media_prepare_asset_files_unverified',false,false,false,true);
      const verified = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',assignedBytes)),
        value => value.toString(16).padStart(2,'0')).join('') === state.sha && mediaImageMagic(assignedBytes,state.mime);
      // The actual selected File must still be the hashed object. No await
      // separates this live identity check from the sole change dispatch.
      if (!verified || !selectedIntact(false))
        return reply('media_prepare_asset_files_unverified',false,false,false,true);
      state.input.dispatchEvent(new Event('change',{bubbles:true}));
      await tick();await tick();
      const attached = () => Array.from(composer.container.querySelectorAll('[data-testid="attachments"],[data-testid="mediaPreview"],[data-testid="tweetPhoto"]')).some(visible);
      if (!selectedIntact(true)) return reply('media_prepare_asset_attachment_unverified',false,false,attached(),true);
      const readbackBytes = new Uint8Array(await assignedFile.arrayBuffer());
      if (!selectedIntact(true)) return reply('media_prepare_asset_attachment_unverified',false,false,attached(),true);
      const readbackVerified = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',readbackBytes)),
        value => value.toString(16).padStart(2,'0')).join('') === state.sha && mediaImageMagic(readbackBytes,state.mime);
      if (!readbackVerified || !selectedIntact(true) || !attached() || !enabled(composer.button,'click') ||
          await mediaHash(mediaText(composer.editor)) !== params.text_sha256 || !selectedIntact(true) ||
          !attached() || !enabled(composer.button,'click'))
        return reply('media_prepare_asset_attachment_unverified',false,readbackVerified && selectedIntact(true),attached(),true);
      return reply('',true,true,true);
    } catch { state.bytes = null;return reply('media_prepare_asset_outcome_unknown',false,false,false,true); }
  }
  globalThis.__aoitalkBrowser = {version: 9, materialGuardVersion: 3, observe, act, mediaPrepareText, mediaPrepareAsset,mediaPrepareReadiness};
})();
