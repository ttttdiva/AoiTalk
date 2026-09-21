// Runs in the extension's isolated JavaScript world in the user's existing tab.
(() => {
  if (globalThis.__aoitalkBrowser) return;
  let targets = new Map();
  const visible = e => e.isConnected && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' && !e.disabled;
  const label = e => e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim() ||
    Array.from(e.labels || []).map(l => { const c=l.cloneNode(true); c.querySelectorAll('input,textarea,select').forEach(n=>n.remove()); return c.textContent.trim(); }).join(' ') ||
    e.getAttribute('placeholder') || e.getAttribute('title') || e.innerText || e.name || '';
  function compatible(e, action) {
    const tag=e.tagName.toLowerCase(), type=e.type || '', role=e.getAttribute('role') || '';
    if(action==='read') return false;
    if(action==='type') return (['input','textarea'].includes(tag) && !['hidden','checkbox','radio','submit','button','reset','file'].includes(type)) || e.isContentEditable;
    if(action==='select') return tag==='select';
    if(['check','uncheck'].includes(action)) return ['checkbox','radio'].includes(type) || ['checkbox','radio'].includes(role);
    return true;
  }
  function elementsIn(root) {
    const items=Array.from(root.querySelectorAll('a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="combobox"],[role="textbox"],[contenteditable="true"]'));
    for(const e of root.querySelectorAll('*')) if(e.shadowRoot) items.push(...elementsIn(e.shadowRoot));
    return items;
  }
  function observe(action) {
    targets=new Map();
    const nonce=crypto.randomUUID().replaceAll('-','').slice(0,12), candidates={};
    for(const e of elementsIn(document)) {
      if(!visible(e) || !compatible(e,action)) continue;
      const id=`e_${nonce}_${targets.size}`;
      targets.set(id,e);
      candidates[id]={tag:e.tagName.toLowerCase(),type:e.type || '',role:e.getAttribute('role') || '',label:label(e).trim().slice(0,220),
        options:e.tagName==='SELECT'?Array.from(e.options).slice(0,60).map(o=>o.label):[]};
      if(e.tagName==='A') candidates[id].href=e.href;
      if(targets.size>=100) break;
    }
    return {url:location.href,title:document.title,text:(document.body?.innerText || '').slice(0,10000),candidates};
  }
  function act({action,target,value}) {
    const e=targets.get(target);
    if(!e || !visible(e)) return {ok:false,error:'edge_stale_element'};
    e.scrollIntoView({block:'center',inline:'nearest'}); e.focus();
    if(action==='click') e.click();
    else if(action==='type') {
      if(e.isContentEditable) { e.textContent=value; }
      else {
        const prototype=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype,'value').set.call(e,value);
      }
      e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));
      e.dispatchEvent(new Event('change',{bubbles:true}));
    } else if(action==='select') {
      const option=Array.from(e.options).find(o=>o.label===value || o.value===value);
      if(!option) return {ok:false,error:'edge_option_not_found'};
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,option.value);
      e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
    } else if(action==='check' || action==='uncheck') {
      const checked=('checked' in e)?e.checked:e.getAttribute('aria-checked')==='true';
      if(checked!==(action==='check')) e.click();
    } else return {ok:false,error:'edge_action_unknown'};
    return {ok:true};
  }
  globalThis.__aoitalkBrowser={observe,act};
})();
