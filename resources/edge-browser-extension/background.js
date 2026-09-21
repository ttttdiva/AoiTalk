let nativePort=null, connected=false, connectionError='';
let selectedTabId=null;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const summary=tab=>({tab_id:tab.id,url:tab.url || '',title:tab.title || ''});

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
async function observe(params) {
  const tab=await ready(params.tab_id);
  await chrome.scripting.executeScript({target:{tabId:tab.id,allFrames:true},files:['content.js']});
  const results=await chrome.scripting.executeScript({target:{tabId:tab.id,allFrames:true},
    func:action=>globalThis.__aoitalkBrowser?.observe(action),args:[params.action || 'read']});
  const candidates={},texts=[];
  for(const frame of results) {
    if(!frame.result) continue;
    texts.push(frame.result.text);
    for(const [id,item] of Object.entries(frame.result.candidates || {})) {
      if(Object.keys(candidates).length>=100) break;
      candidates[`${frame.frameId}:${id}`]=item;
    }
  }
  return {...summary(tab),text:texts.join('\n').slice(0,10000),candidates};
}
async function act(params) {
  const separator=String(params.target).indexOf(':');
  const frameId=Number(params.target.slice(0,separator));
  const target=params.target.slice(separator+1);
  let createdTabId=null;
  const onCreated=tab=>{ if(tab.openerTabId===params.tab_id) createdTabId=tab.id; };
  chrome.tabs.onCreated.addListener(onCreated);
  try {
    const results=await chrome.scripting.executeScript({target:{tabId:params.tab_id,frameIds:[frameId]},
      func:request=>globalThis.__aoitalkBrowser?.act(request) || {ok:false,error:'edge_stale_element'},
      args:[{...params,target}]});
    const result=results[0]?.result;
    if(!result?.ok) throw new Error(result?.error || 'edge_action_failed');
    await sleep(250);
    const nextId=createdTabId ?? params.tab_id;
    selectedTabId=nextId;
    await chrome.storage.session.set({selectedTabId});
    return summary(await ready(nextId));
  } finally { chrome.tabs.onCreated.removeListener(onCreated); }
}
async function execute(command,params={}) {
  if(command==='status') return {connected,selected_tab_id:selectedTabId};
  if(command==='close_tab') { await chrome.tabs.remove(params.tab_id); if(selectedTabId===params.tab_id) {selectedTabId=null;await chrome.storage.session.remove('selectedTabId');} return {}; }
  if(command==='select_tab') {selectedTabId=params.tab_id ?? null;await chrome.storage.session.set({selectedTabId});return {selected_tab_id:selectedTabId};}
  if(command==='tabs') return {tabs:(await chrome.tabs.query({})).map(summary),selected_tab_id:selectedTabId};
  if(command==='resolve_tab') return resolveTab(params);
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
  const settings=await chrome.storage.local.get({enabled:true});
  if(!settings.enabled) return;
  connectionError='';
  const port=chrome.runtime.connectNative('com.aoitalk.edge');
  nativePort=port;
  port.onMessage.addListener(async message=>{
    if(message.event==='connected') { connected=true; connectionError='';return; }
    if(message.event==='host_error') { connectionError=message.error;return; }
    if(!message.id) return;
    try { port.postMessage({id:message.id,ok:true,result:await execute(message.command,message.params)}); }
    catch(error) { try {port.postMessage({id:message.id,ok:false,error:error.message || String(error)});} catch { /* port closed */ } }
  });
  port.onDisconnect.addListener(()=>{
    const lastError=chrome.runtime.lastError;
    connectionError=connectionError || lastError?.message || '接続が切れました';
    if(nativePort===port) { nativePort=null;connected=false; }
  });
}
chrome.runtime.onMessage.addListener((message,_sender,sendResponse)=>{
  (async()=>{
    if(message.command==='select_tab') {
      selectedTabId=message.tab_id;await chrome.storage.session.set({selectedTabId});
    } else if(message.command==='connect') {
      await chrome.storage.local.set({enabled:true});await connect();
    } else if(message.command==='disconnect') {
      await chrome.storage.local.set({enabled:false});nativePort?.disconnect();nativePort=null;connected=false;
    }
    return {connected,error:connectionError,selected_tab_id:selectedTabId};
  })().then(sendResponse,error=>sendResponse({connected:false,error:error.message}));
  return true;
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='reconnect') void connect();});
chrome.alarms.create('reconnect',{periodInMinutes:0.5});
chrome.storage.session.get('selectedTabId').then(value=>{selectedTabId=value.selectedTabId ?? null;});
void connect();
