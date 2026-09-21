async function update(command='status',tab_id) {
  const state=await chrome.runtime.sendMessage({command,tab_id});
  document.getElementById('status').textContent=state.connected?'AoiTalkに接続済み':state.error || '未接続';
  if(state.selected_tab_id!=null) {
    try {const tab=await chrome.tabs.get(state.selected_tab_id);document.getElementById('tab').textContent=`対象: ${tab.title || tab.url}`;}
    catch {document.getElementById('tab').textContent='選択したタブは閉じられています';}
  }
}
document.getElementById('select').onclick=async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});if(tab) await update('select_tab',tab.id);};
document.getElementById('connect').onclick=()=>update('connect');
document.getElementById('disconnect').onclick=()=>update('disconnect');
void update();setInterval(()=>update(),1500);
