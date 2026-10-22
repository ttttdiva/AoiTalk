/* Owned-profile private stdio transport. Product DOM/controller sources remain
 * the exact shipped extension sources; CDP is confined to this owned context.
 * This file never attaches to an external browser or implements publication.
 * Exact LF bytes are pinned by dedicated-manifest.json for every checkout.
 */
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const readline = require('node:readline'), crypto = require('node:crypto');
const MAX = 16 * 1024 * 1024;
let context, config, controller, port, selected = null, closed = false, nextTab = 1;
let runId=null, loginPaused=false;
const GENERAL=new Set(['resolve_tab','observe','act','verify','navigate','back','scroll','end_run','jev_run']);
const tabs = new Map(), pending = new Map(), registrations = new WeakMap();
const write = value => process.stdout.write(JSON.stringify(value) + '\n');
const fail = code => { throw Error(code); };
const event = () => ({addListener(){}, removeListener(){}});
const pageFor = id => {const row=tabs.get(id);if(!row||row.page.isClosed())fail('profile_driver_target_missing');return row;};
function register(page) {
  if(closed)return Promise.reject(Error('profile_driver_closed'));
  if(page.isClosed())return Promise.resolve(null);
  const existing=[...tabs].find(([,row])=>row.page===page);
  if(existing)return Promise.resolve(existing[0]);
  if(registrations.has(page))return registrations.get(page);
  // Publish the shared task before CDP work starts: a page event and the
  // explicit newPage/discovery caller can overlap at either awaited boundary.
  const task=Promise.resolve().then(async()=>{
    let session,registered=false;
    try {
      if(closed)fail('profile_driver_closed');
      if(page.isClosed())return null;
      session=await context.newCDPSession(page);
      if(closed)fail('profile_driver_closed');
      if(page.isClosed())return null;
      await session.send('Page.enable');
      if(closed)fail('profile_driver_closed');
      if(page.isClosed())return null;
      const id=nextTab++;
      page.once('close',()=>{tabs.delete(id);if(selected===id)selected=null;});
      tabs.set(id,{page,session});registered=true;
      return id;
    } catch(error) {
      if(closed)fail('profile_driver_closed');
      if(page.isClosed())return null;
      fail('profile_driver_target_registration_failed');
    } finally {
      if(session&&!registered)try{await session.detach();}catch{}
      registrations.delete(page);
    }
  });
  registrations.set(page,task);
  return task;
}
async function document(row) {
  const {frameTree} = await row.session.send('Page.getFrameTree');
  const frame=frameTree.frame;
  if (!frame.id || !frame.loaderId) fail('profile_driver_document_unverified');
  return {frameId:frame.id, documentId:frame.loaderId};
}
async function tab(id) {
  const row=pageFor(id), target=await row.session.send('Target.getTargetInfo');
  const window=await row.session.send('Browser.getWindowForTarget',{targetId:target.targetInfo.targetId});
  return {id,url:row.page.url(),title:await row.page.title(),windowId:window.windowId,status:'complete',active:selected===id};
}
async function executeScript(request) {
  if (closed || !request?.target || request.target.allFrames ||
      request.target.frameIds && JSON.stringify(request.target.frameIds)!=='[0]') fail('profile_driver_scope_rejected');
  const row=pageFor(request.target.tabId), before=await document(row);
  if(request.target.documentIds && JSON.stringify(request.target.documentIds)!==JSON.stringify([before.documentId]))
    fail('profile_driver_document_changed');
  const world=await row.session.send('Page.createIsolatedWorld',{frameId:before.frameId,worldName:'AoiTalkPreparation'});
  let response;
  if(request.files) {
    if(!Array.isArray(request.files)||request.files.length!==1||!['content.js','media-prepare-youtube.js'].includes(request.files[0]))
      fail('profile_driver_source_rejected');
    response=await row.session.send('Runtime.evaluate',{expression:fs.readFileSync(path.join(config.resource_dir,request.files[0]),'utf8'),
      contextId:world.executionContextId,awaitPromise:true,returnByValue:true});
  } else {
    if(typeof request.func!=='function'||!Array.isArray(request.args||[]))fail('profile_driver_script_rejected');
    response=await row.session.send('Runtime.callFunctionOn',{
      functionDeclaration:`function(...args){return (${request.func.toString()})(...args);}`,
      executionContextId:world.executionContextId,arguments:(request.args||[]).map(value=>({value})),
      awaitPromise:true,returnByValue:true});
  }
  if(response.exceptionDetails || (await document(row)).documentId!==before.documentId)fail('profile_driver_document_changed');
  return [{frameId:0,documentId:before.documentId,result:response.result?.value}];
}
function storage(values, persistent) {
  return {get:async key=>typeof key==='string'?{[key]:values[key]}:Array.isArray(key)?
    Object.fromEntries(key.map(k=>[k,values[k]])):{...key,...values},
    set:async data=>{Object.assign(values,data);if(persistent)save(values);},
    remove:async key=>{delete values[key];if(persistent)save(values);}};
}
function save(values) {
  const text=JSON.stringify(values);if(Buffer.byteLength(text)>2*1024*1024)fail('profile_driver_state_limit');
  const temporary=path.join(config.state_dir,'preparation-state.tmp');
  const fd=fs.openSync(temporary,'w',0o600);
  try{fs.writeFileSync(fd,text);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  fs.renameSync(temporary,path.join(config.state_dir,'preparation-state.json'));
}
async function initialize(value) {
  const keys=['user_data_dir','state_dir','resource_dir','package_dir','browser_context','service','channel','headless','fixture'];
  if(config||!value||Object.keys(value).sort().join()!==keys.sort().join()||
     !['x','youtube','browser'].includes(value.service)||!['msedge','chrome'].includes(value.channel)||typeof value.headless!=='boolean')
    fail('profile_driver_config_invalid');
  config=value;
  const {chromium}=require(config.package_dir);
  try {context=await chromium.launchPersistentContext(config.user_data_dir,{channel:config.channel,headless:config.headless,
    acceptDownloads:false,serviceWorkers:'block',chromiumSandbox:true,
    ignoreDefaultArgs:['--password-store=basic','--use-mock-keychain','--unsafely-disable-devtools-self-xss-warnings'],
    timeout:15000});}catch(error){
    if(config.fixture)process.stderr.write(String(error)+'\n');fail('profile_driver_browser_launch_failed');}
  context.on('close',()=>{closed=true;write({event:'browser_closed'});});
  if(config.fixture) {
    const pages=JSON.parse(fs.readFileSync(config.fixture,'utf8'));
    await context.route('**/*',route=>route.fulfill({contentType:'text/html',body:pages[route.request().url()]||'<html></html>'}));
  }
  // A failed event registration creates no selectable row. Explicit discovery
  // retries it and surfaces any live-page failure; a closed page is ignored.
  context.on('page',page=>register(page).catch(()=>{}));
  for(const page of context.pages())await register(page);
  if(config.service==='browser')selected=tabs.keys().next().value??null;
  const values={};
  const state=path.join(config.state_dir,'preparation-state.json');
  if(fs.existsSync(state)) {
    if(fs.statSync(state).size>2*1024*1024)fail('profile_driver_state_invalid');
    Object.assign(values,JSON.parse(fs.readFileSync(state,'utf8')));
  }
  values.selectedTabId=config.service==='browser'?selected:null;
  const chrome={
    tabs:{get:tab,query:async options=>{
      for(const page of context.pages())await register(page);
      const rows=await Promise.all([...tabs].filter(([,row])=>!row.page.isClosed()).map(([id])=>tab(id)));
      return rows.filter(row=>(options.windowId===undefined||row.windowId===options.windowId)&&(!options.active||row.active));
    },update:async(id,options)=>{
      const row=pageFor(id);
      if(options.url)await row.page.goto(options.url);
      if(options.active){selected=id;await row.page.bringToFront();}
      return tab(id);
    },create:async options=>{
      const page=await context.newPage(),id=await register(page);
      if(id===null)fail('profile_driver_target_missing');
      selected=id;if(options.url)await page.goto(options.url);return tab(id);
    },goBack:async id=>{await pageFor(id).page.goBack();return tab(id);},onCreated:event(),onUpdated:event()},
    windows:{update:async()=>({focused:true})},
    scripting:{executeScript},storage:{local:storage({enabled:true,mediaBrowserContextId:config.browser_context.context_id},false),
      session:storage(values,true)},
    runtime:{onMessage:event(),onStartup:event(),onInstalled:event(),connectNative:()=>{
      let listener;
      port={onMessage:{addListener:handler=>{listener=handler;queueMicrotask(()=>handler({event:'connected'}));}},
        onDisconnect:event(),postMessage:message=>{
          const resolve=pending.get(message.id);if(resolve){pending.delete(message.id);resolve(message);}},
        dispatch:message=>listener(message)};
      return port;
    }},alarms:{onAlarm:event(),create(){}}};
  // The private IPC connection supplies its already-created lease epoch. This
  // is transport identity, never account/document verification or a DOM result.
  const hostCrypto={randomUUID:()=>config.browser_context.context_epoch};
  controller=vm.createContext({chrome,crypto:hostCrypto,URL,AOITALK_OWNED_MATERIAL_GUARD:config.service==='browser'?1:0,
    console:{log(){},error(){}},setTimeout,clearTimeout,
    TextEncoder,TextDecoder,Uint8Array,ArrayBuffer,Buffer,atob,btoa});
  vm.runInContext(fs.readFileSync(path.join(config.resource_dir,'background.js'),'utf8'),controller);
  await new Promise(resolve=>setTimeout(resolve,0));
  const capabilities=await media('media_prepare_capabilities',{});
  if(!capabilities.browser_context_pin||JSON.stringify(capabilities.browser_context)!==JSON.stringify(config.browser_context))
    fail('profile_driver_context_unverified');
  return {ready:true,version:1,browser_context:capabilities.browser_context};
}
async function media(command,params) {
  if(closed||!port)fail('profile_driver_closed');
  const id=crypto.randomUUID();
  const reply=await new Promise(resolve=>{pending.set(id,resolve);port.dispatch({id,command,params});});
  if(!reply.ok)fail(/^(?:media_prepare|edge)_[a-z_]+$/.test(reply.error||'')?reply.error:'profile_driver_command_failed');
  return reply.result;
}
async function authStatus(id) {
  const row=pageFor(id);
  const doc=await document(row);
  const world=await row.session.send('Page.createIsolatedWorld',{frameId:doc.frameId,worldName:'AoiTalkLoginActivity'});
  const response=await row.session.send('Runtime.evaluate',{contextId:world.executionContextId,returnByValue:true,
    expression:`(()=>{
      if(!globalThis.__aoiActivity){
        globalThis.__aoiActivity={count:0};
        for(const kind of ['pointerdown','keydown','input','wheel'])addEventListener(kind,event=>{
          if(event.isTrusted)globalThis.__aoiActivity.count++;
        },true);
      }
      const visible=e=>e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
      const password=[...document.querySelectorAll('input[type=password],input[autocomplete=username],input[autocomplete=current-password],input[autocomplete=one-time-code]')].some(visible);
      const authPath=/(?:^|\\/)(?:login|signin|sign-in|auth)(?:\\/|$)/i.test(location.pathname);
      const authHost=/(?:^|\\.)(?:login|accounts)\\./i.test(location.hostname);
      return {authentication_required:password||((authPath||authHost)&&[...document.querySelectorAll('input')].some(visible)),
        document_visible:document.visibilityState==='visible',activity_count:globalThis.__aoiActivity.count,origin:location.origin};
    })()`});
  if(response.exceptionDetails)fail('profile_driver_document_unverified');
  return {...response.result.value,document_id:doc.documentId,tab_id:id,browser_context:config.browser_context,paused:loginPaused};
}
// jev_run: the Jev navigation loop (Jev chooses the next operation and target on
// every iteration, then the page is re-observed) hosted on the owned Page. Judgment
// requests and the pre-mutation gate are relayed to the Bridge host, which holds
// the server connection: no Jev key is ever present in this process.
const jevPending=new Map();
let jevBusy=false, jevLib=null;
const jevHistory=[];
const jevGeneratedTexts=[];
let jevHistoryTab=null;
async function jevTarget(tabId,selector) {
  if(typeof selector!=='string')return null;
  const row=pageFor(tabId),doc=await document(row);
  const world=await row.session.send('Page.createIsolatedWorld',{frameId:doc.frameId,worldName:'AoiTalkPreparation'});
  const response=await row.session.send('Runtime.callFunctionOn',{
    functionDeclaration:`function(selector){
      const nodes=document.querySelectorAll(selector);if(nodes.length!==1)return null;
      globalThis.__aoiJevTargetIds??=new WeakMap();const ids=globalThis.__aoiJevTargetIds;
      if(!ids.has(nodes[0]))ids.set(nodes[0],crypto.randomUUID());return ids.get(nodes[0]);
    }`,executionContextId:world.executionContextId,arguments:[{value:selector}],returnByValue:true});
  if(response.exceptionDetails||typeof response.result?.value!=='string'||
     (await document(row)).documentId!==doc.documentId)return null;
  return doc.documentId+':'+response.result.value;
}
async function jevMaterial(tabId,selector) {
  if(typeof selector!=='string')return null;
  const observed=await media('observe',{tab_id:tabId,action:'click'});
  const row=pageFor(tabId),doc=await document(row);
  const world=await row.session.send('Page.createIsolatedWorld',{frameId:doc.frameId,worldName:'AoiTalkPreparation'});
  // Material guard supports native forms only. Its submit states are emitted
  // in document order. Require a complete one-to-one match before binding the
  // Jev selector to the existing controller reference; ambiguity fails closed.
  const response=await row.session.send('Runtime.callFunctionOn',{
    functionDeclaration:`function(selector){
      const matches=document.querySelectorAll(selector);
      const visible=e=>e.isConnected&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden'&&
        (!e.checkVisibility||e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
      const submits=[...document.querySelectorAll('button,input')].filter(e=>e.type==='submit'&&e.form&&visible(e));
      return {index:matches.length===1?submits.indexOf(matches[0]):-1,count:submits.length};
    }`,executionContextId:world.executionContextId,arguments:[{value:selector}],returnByValue:true});
  const match=response.result?.value,states=Object.entries(observed.material_states||{});
  if(response.exceptionDetails||!match||match.index<0||match.count!==states.length||
     (await document(row)).documentId!==doc.documentId)return null;
  const [target,material_state]=states[match.index];
  if(material_state.document_id!==doc.documentId||material_state.tab_id!==tabId)return null;
  return {target,material_state};
}
function jevRelay(kind,payload,timeoutMs) {
  const id=crypto.randomUUID();
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{jevPending.delete(id);reject(Error('profile_driver_jev_timeout'));},timeoutMs);
    jevPending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},
      reject:error=>{clearTimeout(timer);reject(error);}});
    write({event:kind,id,run_id:runId,...payload});
  });
}
function jevLoad() {
  if(jevLib)return jevLib;
  // The bundle imports "playwright"; its shim maps that name to this runtime's
  // pinned package, so the loop runs on the exact Playwright the driver owns.
  process.env.JEV_PLAYWRIGHT_PACKAGE=config.package_dir;
  jevLib=require(path.join(__dirname,'jev-lib.cjs'));
  if(typeof jevLib.navigate!=='function')fail('profile_driver_jev_bundle_invalid');
  return jevLib;
}
async function jevRun(params) {
  const keys=['tab_id','task','max_steps','max_seconds'];
  const optional=['completion_text','completion_url'];
  if(keys.some(key=>!Object.hasOwn(params,key))||Object.keys(params).some(key=>!keys.includes(key)&&!optional.includes(key))||
     optional.some(key=>Object.hasOwn(params,key)&&(typeof params[key]!=='string'||params[key].length>16000))||!Number.isInteger(params.tab_id)||
     typeof params.task!=='string'||!params.task.trim()||params.task.length>16000||
     !Number.isInteger(params.max_steps)||params.max_steps<1||params.max_steps>60||
     !Number.isInteger(params.max_seconds)||params.max_seconds<5||params.max_seconds>900)
    fail('profile_driver_jev_params_invalid');
  if(jevBusy)fail('profile_driver_jev_busy');
  if(jevHistoryTab!==null&&jevHistoryTab!==params.tab_id)fail('profile_driver_jev_target_changed');
  jevHistoryTab=params.tab_id;
  jevBusy=true;
  let unwatchPage=null;
  try {
    const lib=jevLoad(), row=pageFor(params.tab_id);
    let refusedForLogin=false,refusedForPage=false,guardedClick=null;
    // The host owns one explicit tab. A new Page cannot inherit that tab's
    // approvals, auth checks or atomic click Proxy; stop before reasoning or
    // acting there. The upstream adapter also disables raw Page adoption.
    const onNewPage=page=>{if(page!==row.page)refusedForPage=true;};
    context.on('page',onNewPage);
    unwatchPage=()=>context.off('page',onNewPage);
    const ensureBoundary=async()=>{
      if(refusedForPage)fail('profile_driver_jev_page_changed');
      const status=await authStatus(params.tab_id);
      if(refusedForPage)fail('profile_driver_jev_page_changed');
      if(status.authentication_required){
        loginPaused=true;refusedForLogin=true;fail('authentication_required');
      }
    };
    const generatedTexts=jevGeneratedTexts;
    const publicTrace=(value,limit)=>{
      let text=String(value||'');
      for(const secret of generatedTexts)for(const variant of [secret,encodeURIComponent(secret)])
        text=text.split(variant).join('[redacted]');
      return text.slice(0,limit);
    };
    const ownedPage=new Proxy(row.page,{get(page,key){
      if(key==='click')return async(selector,options)=>{
        const guarded=guardedClick;guardedClick=null;
        if(!guarded)return page.click(selector,options);
        if(selector!==guarded.selector)fail('profile_driver_jev_target_changed');
        const receipt=await media('act',{tab_id:params.tab_id,action:'click',target:guarded.target,
          expected_material_state:guarded.expected_material_state,
          expected_material_state_digest:guarded.expected_material_state_digest});
        if(receipt.ok!==true||receipt.executed!==true)fail('profile_driver_jev_material_state_changed');
      };
      const value=Reflect.get(page,key,page);return typeof value==='function'?value.bind(page):value;
    }});
    const transport={name:'aoitalk-relay',async ask(request){
      // Extraction is local; login DOM must never reach the provider. Check
      // again for each judgment, including option questions after a relay.
      await ensureBoundary();
      // Only the closed judgment envelope crosses the boundary (never the abort signal).
      const answer=await jevRelay('jev_ask',{request:{state:request.state,questions:request.questions,
        ...(typeof request.model==='string'?{model:request.model}:{})}},60000);
      if(!answer||answer.ok!==true||!answer.reply||typeof answer.reply!=='object')fail('profile_driver_jev_relay_failed');
      return answer.reply;
    }};
    // Field text is generated by the host from the user's goal only; this
    // process never holds a provider key and never types a locally made guess.
    const typingText=async request=>{
      await ensureBoundary();
      const reply=await jevRelay('jev_type',{request:{elementDescription:request.elementDescription,url:request.url}},35000).catch(()=>null);
      if(reply&&reply.ok===true&&typeof reply.text==='string'&&reply.text.length<=16000){
        if(reply.text.length)generatedTexts.push(reply.text);
        return {ok:true,text:reply.text,via:'host'};
      }
      // Upstream standalone search falls back for ok:false. Host-generated
      // input must fail without filling or pressing Enter, so throw instead.
      fail('profile_driver_jev_typing_failed');
    };
    const result=await lib.navigate({task:params.task,page:ownedPage,transport,allowTyping:true,typingText,history:jevHistory,
      followNewPages:false,
      maxSteps:params.max_steps,maxSeconds:params.max_seconds,screenshot:'none',stopOnActionError:true,
      beforeAction:async info=>{
        // A login document is never operated on: stop without executing and let
        // the existing human handoff take over. Fail closed on any relay error.
        try {
          guardedClick=null;
          await ensureBoundary();
          const material=await jevMaterial(params.tab_id,info.target?.selector);
          const stableId=await jevTarget(params.tab_id,info.target?.selector);
          const verdict=await jevRelay('jev_gate',{info:{...info,
            target:info.target?{...info.target,stable_id:stableId}:null,browser_context:config.browser_context,
            material_state:material?.material_state??null}},650000);
          if(refusedForPage)return {allow:false,reason:'page_changed'};
          if(verdict?.allow===true&&info.phase==='commit'&&verdict.expected_material_state){
            if(info.mutation!=='click'||!material||
               JSON.stringify(material.material_state)!==JSON.stringify(verdict.expected_material_state)||
               typeof verdict.expected_material_state_digest!=='string')return {allow:false,reason:'material_state_changed'};
            guardedClick={...material,selector:info.target.selector,
              expected_material_state:verdict.expected_material_state,
              expected_material_state_digest:verdict.expected_material_state_digest};
          }
          return verdict&&verdict.allow===true?{allow:true}:
            {allow:false,reason:typeof verdict?.reason==='string'?verdict.reason.slice(0,160):'host_refused'};
        } catch {return {allow:false,reason:refusedForLogin?'authentication_required':refusedForPage?'page_changed':'gate_unavailable'};}
      }});
    const steps=(Array.isArray(result.steps)?result.steps:[]).slice(0,params.max_steps).map(s=>({step:s.step,
      proposed_action:String(s.proposed_action||'').slice(0,80),
      executed_action:s.executed_action===null?null:String(s.executed_action||'').slice(0,80),
      detail:publicTrace(s.detail,200),outcome:publicTrace(s.outcome,200),
      ...(s.action_error?{action_error:publicTrace(s.action_error,160)}:{})}));
    const executed=steps.some(s=>s.executed_action!==null);
    return {status:refusedForLogin||refusedForPage?'blocked':String(result.status||'error').slice(0,40),steps,executed,
      execution_status:executed?'performed':'not_dispatched',jev_calls:result.usage?.jev_calls??null,
      url:row.page.url(),tab_id:params.tab_id,
      ...(refusedForPage?{blocked_reason:'page_changed'}:{}),
      ...(refusedForLogin?{authentication_required:true}:{})};
  } finally {unwatchPage?.();jevBusy=false;}
}
async function command(action,params) {
  if(action==='initialize')return initialize(params);
  if(action==='close'){
    if(!context)fail('profile_driver_closed');
    closed=true;await context.close();return {closed:true};
  }
  if(!context||closed)fail('profile_driver_closed');
  if(config.service==='browser'&&(GENERAL.has(action)||['auth_status','pause_login','resume_login','cancel_login','show'].includes(action))) {
    if(typeof params.run_id!=='string'||!/^[a-zA-Z0-9_-]{1,128}$/.test(params.run_id))fail('profile_driver_run_invalid');
    if(runId!==null&&runId!==params.run_id)fail('profile_driver_run_changed');
    if(action==='resolve_tab'&&runId===null)runId=params.run_id;
    if(action==='end_run'){
      if(!loginPaused){runId=null;jevHistory.length=0;jevGeneratedTexts.length=0;jevHistoryTab=null;}
      vm.runInContext('observedFrames.clear()',controller);
      return {ended:true,run_id:params.run_id,refs_invalidated:true,connection_retained:true,paused:loginPaused};
    }
    if(action==='pause_login'){loginPaused=true;await pageFor(params.tab_id).page.bringToFront();return authStatus(params.tab_id);}
    if(action==='show'){await pageFor(params.tab_id).page.bringToFront();return {shown:true,tab_id:params.tab_id};}
    if(action==='auth_status')return authStatus(params.tab_id);
    if(action==='cancel_login'){loginPaused=true;runId=null;jevHistory.length=0;jevGeneratedTexts.length=0;jevHistoryTab=null;return {cancelled:true,paused:false};}
    if(action==='resume_login'){
      const status=await authStatus(params.tab_id);
      if(status.authentication_required)fail('browser_login_still_required');
      loginPaused=false;return {resumed:true,tab_id:params.tab_id,browser_context:config.browser_context};
    }
    if(loginPaused) {
      if(action==='resolve_tab'&&!params.url&&Number.isInteger(params.tab_id))
        return {...await tab(params.tab_id),tab_id:params.tab_id,browser_context:config.browser_context};
      fail('browser_login_handoff_active');
    }
    if(action!=='resolve_tab'&&(await authStatus(params.tab_id)).authentication_required) {
      // This branch precedes media/action dispatch. It proves only this command
      // was not dispatched; post-dispatch errors remain unknown to the caller.
      loginPaused=true;return {authentication_required:true,executed:false,execution_status:'not_dispatched',
        tab_id:params.tab_id,browser_context:config.browser_context,
        ...(action==='jev_run'?{status:'blocked',steps:[],jev_calls:0,url:pageFor(params.tab_id).page.url()}:{})};
    }
    const clean={...params};delete clean.run_id;
    if(action==='verify') {
      const observed=await media('observe',{tab_id:params.tab_id,action:'read'});
      const normalized=value=>String(value||'').replace(/\s+/g,' ').trim();
      return {matched:(!params.expected_text||normalized(observed.text).includes(normalized(params.expected_text)))&&
        (!params.expected_url||String(observed.url).replace(/\/$/,'')===String(params.expected_url).replace(/\/$/,''))};
    }
    if(action==='jev_run')return {...await jevRun(clean),browser_context:config.browser_context};
    const result=await media(action,clean);
    if(action==='resolve_tab')selected=result.tab_id;
    return {...result,browser_context:config.browser_context,
      ...(['navigate','back','scroll'].includes(action)?{executed:true,execution_status:'performed'}:{})};
  }
  if(action==='discover_targets') {
    const rows=[];
    for(const page of context.pages())await register(page);
    for(const [id,row] of tabs)if(!row.page.isClosed()) {
      const info=await tab(id);const url=new URL(info.url);
      if(['https:','http:'].includes(url.protocol))rows.push({tab_id:id,type:'page',origin:url.origin,
        title:info.title.slice(0,200),selected:selected===id});
    }
    return {targets:rows};
  }
  if(action==='select_target') {
    if(Object.keys(params).join()!=='tab_id'||!Number.isInteger(params.tab_id))fail('profile_driver_target_invalid');
    await tab(params.tab_id);selected=params.tab_id;
    await media('select_tab',{tab_id:selected});return {selected:true,tab_id:selected};
  }
  if(action==='open_service') {
    if(config.service==='browser')fail('profile_driver_command_rejected');
    if(Object.keys(params).length)fail('profile_driver_params_invalid');
    const page=await context.newPage();
    if(await register(page)===null)fail('profile_driver_target_missing');
    await page.goto(config.service==='x'?'https://x.com/home':'https://studio.youtube.com/');
    return {opened:true};
  }
  if(action==='fixture'&&config.fixture) {
    if(params.action==='open') {
      const page=await context.newPage(),id=await register(page);
      if(id===null)fail('profile_driver_target_missing');
      await page.goto(params.url);return {tab_id:id};
    }
    if(params.action==='evaluate')return pageFor(params.tab_id).page.evaluate(params.script);
    if(params.action==='reload'){await pageFor(params.tab_id).page.reload();return {reloaded:true};}
    fail('profile_driver_fixture_invalid');
  }
  if(!/^media_prepare_(?:capabilities|context|readiness|text|youtube|(?:youtube_)?asset_(?:begin|append|finish|inspect))$/.test(action))
    fail('profile_driver_command_rejected');
  return media(action,params);
}
let chain=Promise.resolve();
readline.createInterface({input:process.stdin,crlfDelay:Infinity}).on('line',line=>{
  // Relay answers resolve a command that is already running, so they must not
  // queue behind it on the command chain.
  if(line.includes('"jev_reply"')&&Buffer.byteLength(line)<=MAX) {
    try {
      const reply=JSON.parse(line),waiting=jevPending.get(reply.jev_reply);
      if(waiting){
        jevPending.delete(reply.jev_reply);
        if(reply.ok===true)waiting.resolve(reply.result);else waiting.reject(Error('profile_driver_jev_relay_failed'));
      }
      return;
    } catch {}
  }
  chain=chain.then(async()=>{
    if(Buffer.byteLength(line)>MAX)fail('profile_driver_input_limit');
    let message;
    try{message=JSON.parse(line);const result=await command(message.command,message.params||{});
      write({id:message.id,ok:true,result});}
    catch(error){write({id:message?.id,ok:false,error:/^(?:profile_driver|media_prepare|edge|browser_login)_[a-z_]+$/.test(error.message)?
      error.message:'profile_driver_command_failed'});}
  }).catch(()=>{closed=true;process.exitCode=1;});
}).on('close',async()=>{closed=true;await chain;if(context)await context.close();});
