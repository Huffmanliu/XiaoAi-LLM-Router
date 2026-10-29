'use strict';
// Keep inactive screens out of the live document, rather than merely hidden.
// References retain listeners/drafts without presenting unrelated password forms
// to autofill. Password-manager heuristics can still override site hints.
const initialElements=new Map([...document.querySelectorAll('[id]')].map(el=>[el.getAttribute('id'),el]));
const $ = (id) => document.getElementById(id)||initialElements.get(id);
const parkedScreens=document.createDocumentFragment();
const viewScreens=[...document.querySelectorAll('.view')];
const screenSlots=new Map([$('auth'),$('workspace'),...viewScreens,$('binding-form')].map(el=>{
  const id=el.getAttribute('id'),marker=document.createComment('screen: '+id);el.before(marker);return [id,{el,marker}];
}));
function mountScreen(id,mounted){const {el,marker}=screenSlots.get(id);if(mounted){if(el.parentNode!==marker.parentNode)marker.after(el);}else if(el.parentNode!==parkedScreens)parkedScreens.append(el);}
function displayView(view){viewScreens.forEach(el=>{const active=el.id==='view-'+view;el.hidden=!active;mountScreen(el.id,active);});}
function displayWorkspace(loggedIn){
  $('auth').hidden=loggedIn;$('workspace').hidden=!loggedIn;
  mountScreen('auth',!loggedIn);mountScreen('workspace',loggedIn);
  if(loggedIn)displayView(state.view);else viewScreens.forEach(el=>mountScreen(el.id,false));
}
const state = {me:null, csrf:'', deviceSaving:false, adminTab:'users', modelServices:[], modelTesting:null, view:'overview', devices:[], selected:new Set(), login:null,
  reverifyDismissed:false, bindingEditing:false, bindingMode:'bind', admin:null, historyBefore:null, historyAfter:null, historyBusy:false, historyReload:false, historyEpoch:0, refreshing:false, invite:null, loginCompleted:null};
const rankingState={page:1,pages:1,epoch:0,controller:null,lastAttempt:0,lastUpdated:null,autoDate:true};
const labels = {overview:['概览','一切尽在掌握'], devices:['我的设备','选择连接，让 AI 就位'],
  history:['对话历史','留住每一次好奇'], dialogue:['对话设置','让回答更合你的心意'],
  billing:['套餐与订单','服务与购买记录'], help:['帮助与反馈','使用指引与问题处理'], admin:['管理后台','关注运行，尊重隐私'], account:['账号与安全','安心使用，从这里开始']};
const statusName = {completed:'模型已返回', failed:'请求失败', interrupted:'请求中断', reserved:'处理中', cancelled:'未发出',
  submitted:'指令已提交', submitting:'提交中', not_requested:'不播报', unknown:'历史未记录', ready:'已绑定', unbound:'未绑定小米账号',
  needs_verification:'需要重新绑定', connection_error:'小米服务访问异常', idle:'待连接'};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (n) => n == null ? '未知' : Number(n).toLocaleString('zh-CN');
const date = (n) => new Date(n*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{$('toast').hidden=true;},5000);}
async function api(path,method='GET',data,signal){
  const options={method,credentials:'same-origin',headers:{'X-CSRF-Token':state.csrf},signal};
  if(data!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}
  const r=await fetch('/api'+path,options);
  const result=await r.json();
  if(!r.ok){const e=new Error(result.error||'操作未完成，请重试');e.status=r.status;if(r.status===401){state.me=null;showAuth();}throw e;}
  return result;
}
// Xiaomi credentials are a separate, explicitly entered form, never hydrated from platform login.
function resetBindingForm(){
  state.bindingEditing=false;state.bindingMode='bind';state.reverifyDismissed=false;
  const f=$('binding-form');f.reset();
  $('xiaomi-password-help').open=false;
  for(const name of ['xiaomi_account','xiaomi_secret']){const input=f.elements.namedItem(name);input.value='';input.readOnly=true;}
}
for(const name of ['xiaomi_account','xiaomi_secret']){
  const input=$('binding-form').elements.namedItem(name);
  input.addEventListener('focus',()=>{input.readOnly=false;});
}
function showAuth(){ if(typeof resetSupportHelp==='function')resetSupportHelp(); if(typeof resetAdminFeedback==='function')resetAdminFeedback(); $('speech-settings-form').reset();delete $('speech-settings-form').dataset.loaded; if(typeof resetBilling==='function')resetBilling();resetRanking();resetBindingForm();$('model-service-form').reset();$('model-service-form').elements.id.value='';state.modelServices=[];state.modelTesting=null;state.historyEpoch++;$('history-list').replaceChildren();displayWorkspace(false); }
async function busy(button,work){if(button?.disabled)return;if(button)button.disabled=true;try{return await work();}finally{if(button)button.disabled=false;}}
function action(id,work){$(id).addEventListener('click',()=>busy($(id),work).catch(e=>toast(e.message)));}
function form(id,work){$(id).addEventListener('submit',e=>{e.preventDefault();const data=new FormData(e.target);busy(e.submitter||e.target.querySelector('button[type=submit]'),()=>work(data,e.target)).catch(e=>toast(e.message));});}
function confirmation(title,text){return new Promise(resolve=>{$('confirm-title').textContent=title;$('confirm-text').textContent=text;$('confirm-dialog').addEventListener('close',()=>resolve($('confirm-dialog').returnValue==='ok'),{once:true});$('confirm-dialog').showModal();});}
function fill(formId,values){const f=$(formId);Object.entries(values).forEach(([key,value])=>{const el=f.elements.namedItem(key);if(el){if(el.type==='checkbox')el.checked=!!value;else el.value=value??'';}});}
function numeric(data,keys){const values={};keys.forEach(k=>values[k]=Number(data.get(k)));return values;}
function metrics(target,items){$(target).innerHTML=items.map(([name,value,note,unit])=>`<div class="metric"><div class="metric-label">${esc(name)}</div><div class="metric-value">${esc(value)}${unit?`<small>${esc(unit)}</small>`:''}</div><div class="metric-note">${esc(note)}</div></div>`).join('');}
function badge(value,speech=false){const good=['completed','submitted','ready'].includes(value);const bad=['failed','interrupted','needs_verification','connection_error'].includes(value);const name=speech?({interrupted:'播报已中断',failed:'播报失败',submitting:'播报处理中'}[value]||statusName[value]):statusName[value];return `<span class="badge${good?' green':bad?' orange':''}">${esc(name||value)}</span>`;}
function deviceName(id){return state.devices.find(d=>d.id===id)?.name||(id==='web-debug'?'历史文字调试':'音箱');}
function requests(target,rows,admin=false){
  const users=new Map((state.admin?.users||[]).map(u=>[u.id,u.username]));
  $(target).innerHTML=rows.length?rows.slice(0,30).map(r=>`<tr><td>${esc(date(r.created))}</td>${admin?`<td>${esc(users.get(r.user_id)||'历史用户')}</td>`:''}<td>${esc(r.source==='model_test'?'模型连接测试':r.source==='web'?'历史文字调试':deviceName(r.device_id))}</td><td>${badge(r.status)}</td><td>${esc(number(r.total_tokens))}</td><td>${r.elapsed_ms==null?'—':esc((r.elapsed_ms/1000).toFixed(1))+' 秒'}</td><td>${badge(r.speech_status,true)}${r.speech_status==='failed'?`<p class="small muted speech-diagnostic">${esc(r.speech_error?.message||'此记录未保存具体原因；旧记录无法补查，请再次提问后查看新记录。')}</p>`:''}</td></tr>`).join(''):`<tr><td colspan="${admin?7:6}"><div class="empty">还没有请求记录，连接设备后开始第一次对话吧。</div></td></tr>`;
}
function renderUsage(data){
  const p=state.me.policy;const t=data.totals;const enabled=state.devices.filter(d=>d.enabled&&d.present).length;
  metrics('metrics',[['今日提问',number(data.today),`每日上限 ${number(p.daily_limit)} 次`,'次'],['累计 token',number(t.total_tokens),`输入 ${number(t.prompt_tokens)} / 输出 ${number(t.completion_tokens)}${t.unknown_usage?' · '+t.unknown_usage+' 次未知':''}`,''],['已启用音箱',number(enabled),'仅处理已确认启用的设备','台'],['累计失败',number(t.failures||0),'失败请求也可能产生消耗','次']]);
  requests('requests',data.requests);
  const rows=[];for(let i=6;i>=0;i--){const d=new Date(Date.now()-i*86400000);const key=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);rows.push(data.daily.find(x=>x.day===key)||{day:key,requests:0,total_tokens:0});}
  const max=Math.max(1,...rows.map(r=>r.requests));
  $('daily-chart').innerHTML=rows.map(r=>`<div class="chart-day"><b>${number(r.requests)}</b><progress max="${max}" value="${r.requests}" aria-label="${esc(r.day)}，${r.requests} 次请求"></progress><span>${esc(r.day.slice(5).replace('-','/'))}</span></div>`).join('');
}
function updateDeviceSaveState(){
  const saved=new Set(state.devices.filter(d=>d.present&&d.supported&&d.enabled).map(d=>d.id));
  const changed=saved.size!==state.selected.size||[...state.selected].some(id=>!saved.has(id));
  $('save-devices').disabled=state.deviceSaving||!changed;
}
function renderDevices(){
  const shown=state.devices.filter(d=>d.present);
  $('device-preview').innerHTML=shown.length?shown.slice(0,3).map(d=>`<div class="device-mini"><span class="device-icon" aria-hidden="true">◉</span><div><strong>${esc(d.name)}</strong><small>${esc(d.hardware)}</small></div>${!d.supported?'<span class="badge">不开放接入</span>':d.enabled?'<span class="badge green">监听已启用</span>':'<span class="badge">未启用</span>'}</div>`).join(''):'<div class="empty">还没有连接音箱</div>';
  $('device-list').innerHTML=shown.length?shown.map(d=>`<div class="device-row"><label><input type="checkbox" data-device="${esc(d.id)}" ${state.selected.has(d.id)?'checked':''} ${!d.supported?'disabled':''}><span class="device-description"><strong>${esc(d.name)}</strong><small>${esc(d.hardware)}${d.reason?' · '+esc(d.reason):''}</small></span></label><span class="badge${d.enabled?' green':''}">${!d.supported?'不开放接入':d.enabled?'监听已启用':'未启用'}</span>${d.enabled?`<button data-pause="${esc(d.id)}">暂停</button>`:''}</div>`).join(''):'<div class="empty">绑定小米账号，获取可用音箱。</div>';
  $('device-list').querySelectorAll('[data-device]').forEach(el=>el.addEventListener('change',()=>{if(el.checked)state.selected.add(el.dataset.device);else state.selected.delete(el.dataset.device);updateDeviceSaveState();}));
  $('device-list').querySelectorAll('[data-pause]').forEach(el=>el.addEventListener('click',()=>busy(el,async()=>{await api('/devices','PUT',{devices:state.devices.filter(d=>d.enabled&&d.id!==el.dataset.pause).map(d=>d.id)});state.selected.delete(el.dataset.pause);await fetchDevices();toast('已暂停接收新问题，当前回答允许完成');}).catch(e=>toast(e.message))));
  updateDeviceSaveState();
  $('clear-device').innerHTML=shown.length?shown.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join(''):'<option value="">暂无可清空的设备</option>';
  $('clear-form').querySelector('button').disabled=!shown.length;
}
async function fetchDevices(restoreSaved=false){
  state.devices=await api('/devices');
  const selectable=new Set(state.devices.filter(d=>d.supported&&d.present).map(d=>d.id));
  state.selected=restoreSaved
    ?new Set(state.devices.filter(d=>d.enabled&&selectable.has(d.id)).map(d=>d.id))
    :new Set([...state.selected].filter(id=>selectable.has(id)));
  renderDevices();
}
async function hydrate(){
  state.me=await api('/me');state.csrf=state.me.csrf;
  if(state.view==='admin'&&state.me.role!=='admin')state.view='overview';
  displayWorkspace(true);$('username').textContent=state.me.username;$('avatar').textContent=state.me.username[0].toUpperCase();$('environment').textContent=state.me.environment;$('admin-nav').hidden=state.me.role!=='admin';$('retention').textContent=state.me.retention_days;
  resetBindingForm();updateConnection();await fetchDevices(true);renderDialogue();await refreshUsage();
}
function prefixCount(){const count=Array.from($('answer-prefix').value.trim()).length;$('prefix-count').textContent=`${count} / 12`;$('answer-prefix').setCustomValidity(count>12?'最多12个字符':'');}
function routingExplanation(){const keyword=$('routing-mode').value==='keyword';$('wake-words-field').hidden=!keyword;$('routing-explanation').textContent=keyword?'只有命中触发词时才调用 AI；小爱无法回答也不会自动接管。':'小爱明确无法回答时由 AI 补答，无需额外触发词；可能先听到小爱的失败提示。未识别的失败话术可能不会接管。';}
function renderDialogue(){
  fill('dialogue-form',{...state.me.dialogue,wake_words:state.me.dialogue.wake_words.join('\n')});
  fill('prefix-form',state.me.dialogue);routingExplanation();prefixCount();
  const seconds=state.me.policy.ttl_seconds;$('memory-ttl').textContent=seconds%60===0?`${seconds/60} 分钟`:`${seconds} 秒`;
  $('memory-turns').textContent=`${state.me.policy.max_turns} 轮`;
}
$('routing-mode').addEventListener('change',routingExplanation);
let prefixComposing=false;
function limitPrefix(){
  const field=$('answer-prefix'),chars=Array.from(field.value);
  if(chars.length>12){const caret=field.selectionStart;field.value=chars.slice(0,12).join('');field.setSelectionRange(Math.min(caret,field.value.length),Math.min(caret,field.value.length));}
  prefixCount();
}
function insertPrefix(text){
  const field=$('answer-prefix'),start=field.selectionStart,end=field.selectionEnd;
  const remaining=12-Array.from(field.value.slice(0,start)+field.value.slice(end)).length;
  field.setRangeText(Array.from(text).slice(0,Math.max(0,remaining)).join(''),start,end,'end');
  limitPrefix();
}
$('answer-prefix').addEventListener('beforeinput',event=>{
  if(!prefixComposing&&!event.isComposing&&event.inputType==='insertText'&&event.data!==null){event.preventDefault();insertPrefix(event.data);}
});
$('answer-prefix').addEventListener('paste',event=>{if(event.clipboardData){event.preventDefault();insertPrefix(event.clipboardData.getData('text'));}});
$('answer-prefix').addEventListener('compositionstart',()=>{prefixComposing=true;});
$('answer-prefix').addEventListener('compositionend',()=>{prefixComposing=false;limitPrefix();});
$('answer-prefix').addEventListener('input',event=>{if(!prefixComposing&&!event.isComposing)limitPrefix();});
const bindingNotices=new Map();
function notifyBindingState(status){
  const uid=state.me?.user_id;if(!uid)return;
  const key='mi-binding-notice:'+uid;let prior=bindingNotices.get(uid);
  if(prior===undefined){try{prior=sessionStorage.getItem(key);}catch{prior=null;}}
  if(prior!==status){
    bindingNotices.set(uid,status);try{sessionStorage.setItem(key,status);}catch{}
    if(status==='connection_error')toast('暂时无法访问小米服务，后台会自动重试；原绑定保留。');
    else if(status==='needs_verification')toast('小米账号绑定已失效，请前往“我的设备”输入当前小米密码重新绑定。');
    else if(status==='ready'&&['connection_error','needs_verification'].includes(prior))toast('小米服务访问已恢复，当前账号已绑定。');
  }
}
function updateConnection(){
  const connected=state.me.account_state!=='unbound';
  const s=state.me.account_state==='needs_verification'?'needs_verification':connected&&state.me.connection_state==='connection_error'?'connection_error':state.me.account_state;
  const expired=s==='needs_verification';
  const check=state.me.login_check;
  const canReverify=expired||Boolean(check?.can_reverify);
  const slowRetry=s==='connection_error'&&check?.retry_interval===300;
  const loginActive=['logging_in','verification_required','verifying','applying'].includes(state.login?.state);
  const pending=state.login?.state==='awaiting_confirmation';
  if(expired&&!state.reverifyDismissed&&!state.bindingEditing&&!pending&&!loginActive){
    state.bindingMode='reverify';state.bindingEditing=true;
  }else if(!canReverify&&state.bindingMode==='reverify'&&!loginActive){
    resetBindingForm();
  }
  const mode=loginActive?state.login.mode:state.bindingMode;
  const showForm=(!connected||state.bindingEditing)&&!pending&&!loginActive;
  const verified=s==='ready'&&check?.state==='valid'&&Date.now()/1000-check.checked_at<300;
  const statusText=verified?'已绑定':(statusName[s]||s);
  $('binding-title').textContent=expired?'需要重新绑定小米账号':connected?'已绑定的小米账号':'绑定小米账号';
  $('connected-account-icon').textContent=expired?'!':'✓';
  $('connected-account-icon').classList.toggle('warning',expired);
  $('connected-account').hidden=!connected;
  $('connected-account-label').textContent=state.me.account_label||'已绑定的小米账号';
  $('connected-account-state').textContent=s==='connection_error'?'已绑定 · 小米服务访问异常':statusText;
  $('connected-account-note').textContent=verified?'当前小米账号绑定正常，无需重新绑定。可以直接在下方管理音箱。':s==='ready'?'账号凭据已加密保存。系统会自动检查绑定状态，无需手动确认。':expired?'原账号及设备选择已保留，请重新绑定以恢复使用。平台登录不受影响。':slowRetry?'小米服务持续访问异常，已延长至约每5分钟检查一次，期间 AI 接管可能受影响。后台不会自动重新登录或发送验证码。你也可以主动重新绑定当前账号，原设备选择保留。':'暂时无法访问小米服务，后台约每分钟检查一次；持续异常约2分钟后会延长至约每5分钟。期间 AI 接管可能受影响。原绑定和设备选择保留，后台不会自动重新登录或发送验证码。';
  $('binding-access-state').hidden=s!=='connection_error'||pending;
  $('binding-access-state').textContent=slowRetry?'访问异常，约每5分钟重试':'访问异常，稍后重试';
  $('resume-reverify').hidden=!canReverify||showForm||pending||loginActive;
  $('replace-account').hidden=showForm||pending;
  $('replace-account').disabled=loginActive;
  $('binding-form').hidden=!showForm;
  mountScreen('binding-form',showForm&&state.view==='devices'&&Boolean(state.me));
  $('binding-hint').hidden=!showForm;
  $('binding-privacy-note').hidden=!showForm;
  $('xiaomi-account-field').hidden=mode==='reverify';
  $('xiaomi-account-input').disabled=mode==='reverify';
  $('xiaomi-account-input').required=mode!=='reverify';
  $('xiaomi-submit').textContent=mode==='reverify'?'重新绑定当前小米账号':mode==='replace'?'验证新绑定账号':'绑定并获取设备';
  const cancelLabel=mode==='replace'?'取消更换':mode==='reverify'?'暂不处理':'暂不绑定';
  $('cancel-login').textContent=cancelLabel;$('cancel-active-login').textContent=cancelLabel;
  $('binding-hint').textContent=mode==='reverify'?'仅重新绑定上方的小米账号，不能在此更换账号。请输入该小米账号的密码，按小米要求完成验证码验证。':mode==='replace'?'填写新小米账号和密码。提交时按需验证平台安全邮箱，原绑定保持不变；新账号验证成功后，还需确认才会切换。':'你已登录平台，还未绑定小米账号。请填写你自己的小米账号与密码，平台账号不能用于此处。';
  $('cancel-active-login').hidden=!loginActive;
  $('login-state').hidden=!loginActive&&!['failed','expired','cancelled'].includes(state.login?.state);
  $('switch-confirmation').hidden=!pending;
  if(pending)$('switch-summary').textContent=`将从 ${state.login.old_label} 更换为 ${state.login.new_label}，发现 ${state.login.device_count} 台音箱。旧账号目前仍在使用。`;
  $('unbind').disabled=!connected||loginActive||pending;
  $('refresh-devices').disabled=!connected||state.login?.state==='applying';
  $('connection-badge').textContent=s==='ready'?'小米账号已绑定':s==='needs_verification'?'小米账号绑定已失效':statusText;
  $('connection-badge').title=s==='connection_error'?'暂时无法访问小米服务，后台会自动检查并重试；原绑定保留。详情见“我的设备”。':s==='ready'?'已保存小米账号绑定；系统会自动检查，无需手动确认。':s==='needs_verification'?'小米账号绑定需要重新验证；平台登录不受影响。':'小米账号绑定状态';
  $('connection-badge').className='badge'+(s==='ready'?' green':s==='needs_verification'||s==='connection_error'?' orange':'');
  notifyBindingState(s);
}
async function refreshUsage(){renderUsage(await api('/usage'));}
async function navigate(view){
  if(!labels[view])return;
  if(view==='admin'&&state.me.role!=='admin')return;
  if(state.view==='devices'&&view!=='devices'){
    $('xiaomi-secret-input').value='';$('xiaomi-secret-input').readOnly=true;
  }
  state.view=view;displayView(view);document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===view));$('page-title').textContent=labels[view][0];$('page-kicker').textContent=labels[view][1];
  if(view==='devices'){resetBindingForm();updateConnection();await fetchDevices();}if(view==='history')await loadHistory(true);if(view==='admin')await loadAdmin();if(view==='dialogue')renderDialogue();if(view==='account')await loadSecurity();if(view==='billing')await loadBilling();if(view==='help'&&typeof loadSupportHelp==='function')await loadSupportHelp();
}
function historyEntry(r){
  const article=document.createElement('article');article.className='history-entry';article.dataset.historyId=r.id;
  article.innerHTML=`<div class="history-meta"><span>${esc(date(r.created))}</span><span>${esc(r.device_name||deviceName(r.device_id))}${r.account_label?' · '+esc(r.account_label):''}</span><button class="text-button danger">删除</button></div><h3></h3><p></p>`;
  article.querySelector('h3').textContent=r.question;article.querySelector('p').textContent=r.answer;
  article.querySelector('button').addEventListener('click',()=>busy(article.querySelector('button'),async()=>{
    if(await confirmation('删除这条对话？','删除后无法恢复，不影响当前上下文。')){
      await api('/history/'+encodeURIComponent(r.id),'DELETE');article.remove();historyEmpty();
    }
  }).catch(e=>toast(e.message)));return article;
}
function historyEmpty(){if(!$('history-list').querySelector('.history-entry'))$('history-list').innerHTML='<div class="empty">还没有历史记录。开始一次对话，它会自动出现在这里。</div>';}
function historyCursor(r){return {time:r.created,id:r.id};}
function historyDone(){
  state.historyBusy=false;
  if(state.historyReload){state.historyReload=false;loadHistory(true).catch(e=>toast(e.message));}
}
async function loadHistory(reset=false){
  if(state.historyBusy){if(reset){state.historyReload=true;state.historyEpoch++;}return;}
  state.historyBusy=true;const epoch=state.historyEpoch;
  try{
    const query=new URLSearchParams();
    if(!reset&&state.historyBefore){query.set('before',state.historyBefore.time);query.set('before_id',state.historyBefore.id);}
    const rows=await api('/history'+(query.size?'?'+query:''));
    if(epoch!==state.historyEpoch)return;
    if(reset){state.historyBefore=null;$('history-list').replaceChildren();state.historyAfter=rows.length?historyCursor(rows[0]):null;}
    const ids=new Set([...$('history-list').querySelectorAll('[data-history-id]')].map(el=>el.dataset.historyId));
    for(const r of rows){if(!ids.has(r.id)){$('history-list').querySelector('.empty')?.remove();$('history-list').append(historyEntry(r));ids.add(r.id);}}
    state.historyBefore=rows.length?historyCursor(rows[rows.length-1]):state.historyBefore;
    $('more-history').hidden=rows.length<30;historyEmpty();
    $('history-sync').textContent='自动更新 · 每 4 秒检查新对话';
  }finally{historyDone();}
}
async function refreshHistory(){
  if(state.historyBusy)return;
  if(!state.historyAfter){await loadHistory(true);return;}
  state.historyBusy=true;const epoch=state.historyEpoch;
  try{
    // Fetch oldest unseen first so even a long gap or >30 new records cannot skip a page.
    const query=new URLSearchParams({after:state.historyAfter.time,after_id:state.historyAfter.id});
    const rows=await api('/history?'+query);
    if(epoch!==state.historyEpoch||!rows.length)return;
    const list=$('history-list');
    const anchor=window.scrollY>100?[...list.children].find(el=>el.getBoundingClientRect().bottom>0):null;
    const oldTop=anchor?.getBoundingClientRect().top;
    const ids=new Set([...list.querySelectorAll('[data-history-id]')].map(el=>el.dataset.historyId));
    for(const r of rows){if(!ids.has(r.id)){list.querySelector('.empty')?.remove();list.prepend(historyEntry(r));ids.add(r.id);}}
    state.historyAfter=historyCursor(rows[rows.length-1]);
    if(anchor?.isConnected)window.scrollBy(0,anchor.getBoundingClientRect().top-oldTop);
    $('history-sync').textContent='已自动更新 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
  }catch(e){$('history-sync').textContent='连接暂不可用，恢复后自动更新';throw e;}
  finally{historyDone();}
}
async function loadAdmin(){
  const selected=$('policy-user').value;state.admin=await api('/admin');const {users,usage,defaults}=state.admin;
  metrics('admin-metrics',[['平台用户',number(users.filter(u=>u.role!=='admin').length),'邀请开通，独立平台账号','人'],['今日请求',number(usage.today),'全站共用平台模型密钥','次'],['累计 token',number(usage.totals.total_tokens),`${usage.totals.unknown_usage||0} 次消耗未知`,''],['累计失败',number(usage.totals.failures||0),'不代表没有产生消耗','次']]);
  $('admin-users').innerHTML=users.map(u=>`<tr><td>${esc(u.username)}${u.role==='admin'?' · 管理员':''}</td><td><span class="badge${u.active?' green':' orange'}">${u.active?'正常':'已暂停'}</span></td><td>${number(u.listening_count)} / ${number(u.device_count)}</td><td>${u.today} / ${u.policy.daily_limit}</td><td>${u.role!=='admin'?`<button data-edit="${esc(u.id)}">规则</button> <button data-reset="${esc(u.id)}">重置密码</button>`:'—'}</td></tr>`).join('');
  $('policy-user').innerHTML=users.filter(u=>u.role!=='admin').map(u=>`<option value="${esc(u.id)}">${esc(u.username)}</option>`).join('');if(selected&&users.some(u=>u.id===selected))$('policy-user').value=selected;
  fill('capacity-form',state.admin.capacity.limits);renderCapacity(state.admin.capacity);
  if(!$('speech-settings-form').dataset.loaded){fill('speech-settings-form',state.admin.speech_settings);$('speech-settings-form').dataset.loaded='true';speechSettingsExample();}
  fillUserPolicy();renderFailureRules();fill('defaults-form',defaults);requests('admin-requests',usage.requests,true);
  setAdminTab(state.adminTab);await loadModelServices();
  $('user-policy-form').querySelector('button[type=submit]').disabled=!$('policy-user').value;
  $('admin-users').querySelectorAll('[data-edit]').forEach(el=>el.addEventListener('click',()=>{$('policy-user').value=el.dataset.edit;fillUserPolicy();$('user-policy-form').scrollIntoView({behavior:'smooth',block:'center'});}));
  $('admin-users').querySelectorAll('[data-reset]').forEach(el=>el.addEventListener('click',()=>busy(el,async()=>{if(await confirmation('发送密码重置邮件？','重置链接只发送至用户已验证的安全邮箱。')){await ensureRecent();toast((await api('/admin/users/'+el.dataset.reset+'/reset','POST')).message);}}).catch(e=>toast(e.message))));
}
function setAdminTab(key){
  state.adminTab=key;
  document.querySelectorAll('[data-admin-tab]').forEach(tab=>{const selected=tab.dataset.adminTab===key;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;$(tab.getAttribute('aria-controls')).hidden=!selected;});
  refreshRankingIfDue();if(key==='feedback'&&typeof loadAdminFeedback==='function')loadAdminFeedback().catch(e=>toast(e.message));if(key==='billing')loadAdminBilling().catch(e=>toast(e.message));
}
function beijingDay(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
function resetRanking(){
  rankingState.epoch++;rankingState.controller?.abort();rankingState.controller=null;
  clearTimeout(rankingState.searchTimer);
  Object.assign(rankingState,{page:1,pages:1,lastAttempt:0,lastUpdated:null,autoDate:true});
  $('ranking-period').value='day';$('ranking-date-label').textContent='参考日期';$('ranking-date').value=beijingDay();$('ranking-date').max=beijingDay();
  $('ranking-search').value='';$('ranking-unknown').checked=false;
  $('ranking-rows').innerHTML='<tr><td colspan="9" class="empty">等待加载</td></tr>';
  $('ranking-metrics').replaceChildren();$('ranking-page').textContent='';$('ranking-range').textContent='按北京时间统计，周一为每周起点。';
  $('ranking-sync').textContent='等待加载';$('ranking-prev').disabled=true;$('ranking-next').disabled=true;$('ranking-refresh').disabled=false;
}
function refreshRankingIfDue(){
  if(state.me?.role==='admin'&&state.view==='admin'&&state.adminTab==='ranking'&&!document.hidden&&!rankingState.controller&&Date.now()-rankingState.lastAttempt>=30000)void loadRanking();
}
async function loadRanking(){
  if(state.me?.role!=='admin')return;
  clearTimeout(rankingState.searchTimer);
  $('ranking-date-label').textContent=$('ranking-period').value.startsWith('last_')?'截止日期':'参考日期';
  $('ranking-date').max=beijingDay();if(rankingState.autoDate)$('ranking-date').value=beijingDay();
  if(!$('ranking-date').reportValidity())return;
  rankingState.controller?.abort();const controller=new AbortController();rankingState.controller=controller;
  const epoch=++rankingState.epoch,uid=state.me.user_id;rankingState.lastAttempt=Date.now();
  const query=new URLSearchParams({period:$('ranking-period').value,search:$('ranking-search').value.trim(),only_unknown:$('ranking-unknown').checked,page:rankingState.page});
  if(!rankingState.autoDate)query.set('reference_date',$('ranking-date').value);
  $('ranking-refresh').disabled=true;$('ranking-prev').disabled=true;$('ranking-next').disabled=true;
  $('ranking-sync').textContent=rankingState.lastUpdated?'正在加载所选条件，下方暂为上次结果 · '+date(rankingState.lastUpdated):'正在加载排名…';
  try{
    const data=await api('/admin/usage-ranking?'+query,'GET',undefined,controller.signal);
    if(epoch!==rankingState.epoch||state.me?.user_id!==uid)return;
    rankingState.page=data.page;rankingState.pages=data.pages;rankingState.lastUpdated=data.generated_at;
    if(rankingState.autoDate)$('ranking-date').value=data.reference_date;
    $('ranking-range').textContent=`${data.start_date} 至 ${data.end_date}${data.period.startsWith('last_')?' · 滚动统计':''} · 北京时间 · 按请求开始时间归属周期`;
    metrics('ranking-metrics',[['已知总Token',number(data.summary.total_tokens),'不含未知消耗',''],['模型请求次数',number(data.summary.requests),'已发出，主备切换按一个问题计','次'],['存在未知消耗',number(data.summary.unknown_usage),'有未返回用量的已结束尝试','次请求'],['等待用量返回',number(data.summary.pending_usage),'进行中，未当作零消耗','次请求']]);
    $('ranking-rows').innerHTML=data.rows.length?data.rows.map(u=>{
      const completeness=[u.unknown_usage?`<span class="badge orange">存在未知消耗 · ${number(u.unknown_usage)} 次</span>`:'',u.pending_usage?`<span class="badge">等待用量返回 · ${number(u.pending_usage)} 次</span>`:''].filter(Boolean).join(' ');
      return `<tr><td>${number(u.rank)}</td><td>${esc(u.username)}${u.role==='admin'?' · 管理员':''}</td><td><span class="badge${u.active?' green':' orange'}">${u.active?'正常':'已暂停'}</span></td><td>${number(u.requests)}</td><td>${number(u.prompt_tokens)}</td><td>${number(u.completion_tokens)}</td><td><strong>${number(u.total_tokens)}</strong></td><td>${completeness||(!u.requests?'暂无调用':'已返回用量')}</td><td>${u.role!=='admin'?`<button type="button" data-ranking-edit="${esc(u.user_id)}">调整额度</button>`:'—'}</td></tr>`;
    }).join(''):'<tr><td colspan="9" class="empty">没有符合条件的用户</td></tr>';
    $('ranking-page').textContent=`共 ${number(data.users_total)} 人 · 第 ${data.page} / ${data.pages} 页 · 每页 ${data.page_size} 人`;
    $('ranking-sync').textContent='已更新 · '+date(data.generated_at)+' · 页面可见时每30秒自动更新';
  }catch(e){if(epoch===rankingState.epoch&&state.me?.user_id===uid&&e.name!=='AbortError')$('ranking-sync').textContent='排名更新失败，请重试'+(rankingState.lastUpdated?' · 已保留上次结果：'+date(rankingState.lastUpdated):'，暂无统计结果');}
  finally{if(epoch===rankingState.epoch){rankingState.controller=null;$('ranking-refresh').disabled=false;$('ranking-prev').disabled=rankingState.page<=1;$('ranking-next').disabled=rankingState.page>=rankingState.pages;}}
}
for(const id of ['ranking-period','ranking-date','ranking-unknown'])$(id).addEventListener('change',()=>{if(id==='ranking-date')rankingState.autoDate=false;rankingState.page=1;void loadRanking();});
$('ranking-search').addEventListener('input',()=>{clearTimeout(rankingState.searchTimer);rankingState.searchTimer=setTimeout(()=>{rankingState.page=1;void loadRanking();},300);});
$('ranking-refresh').addEventListener('click',()=>void loadRanking());
for(const [id,delta] of [['ranking-prev',-1],['ranking-next',1]])$(id).addEventListener('click',()=>{rankingState.page+=delta;void loadRanking();});
$('ranking-rows').addEventListener('click',event=>{
  const button=event.target.closest('[data-ranking-edit]');if(!button)return;
  void busy(button,async()=>{
    const uid=button.dataset.rankingEdit;
    if(!state.admin.users.some(u=>u.id===uid)){
      state.admin.users=(await api('/admin')).users;
      $('policy-user').innerHTML=state.admin.users.filter(u=>u.role!=='admin').map(u=>`<option value="${esc(u.id)}">${esc(u.username)}</option>`).join('');
    }
    setAdminTab('users');$('policy-user').value=uid;fillUserPolicy();$('user-policy-form').querySelector('button[type=submit]').disabled=!$('policy-user').value;
    $('user-policy-form').scrollIntoView({behavior:'smooth',block:'center'});
  }).catch(e=>toast(e.message));
});
resetRanking();
setInterval(refreshRankingIfDue,1000);
const adminTabs=[...document.querySelectorAll('[data-admin-tab]')];
adminTabs.forEach((tab,index)=>{
  tab.addEventListener('click',()=>setAdminTab(tab.dataset.adminTab));
  tab.addEventListener('keydown',event=>{let next;if(event.key==='ArrowRight')next=(index+1)%adminTabs.length;else if(event.key==='ArrowLeft')next=(index+adminTabs.length-1)%adminTabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=adminTabs.length-1;else return;event.preventDefault();setAdminTab(adminTabs[next].dataset.adminTab);adminTabs[next].focus();});
});
const modelResult={untested:'尚未调用',completed:'成功',authentication:'鉴权失败',rate_limit:'限流',unavailable:'服务异常',connection:'连接失败',balance:'余额或账单不足',model_not_found:'模型或接口不存在',timeout:'超时（未自动重试）',request_failed:'请求失败',invalid_response:'未返回有效文字',content_rejected:'内容被拒绝（未自动重试）',interrupted:'调用中断',started:'正在调用',failed:'失败'};
// Official destinations only; never attach API keys or platform account data.
const modelAccountLinks={
  deepseek:[['用量与余额','https://platform.deepseek.com/usage']],
  qwen:[['模型用量','https://bailian.console.aliyun.com/cn-beijing/costing-balance/usage-statistics'],['余额与账单','https://billing-cost.console.aliyun.com/']],
  qwen_intl:[['费用概览','https://modelstudio.console.alibabacloud.com/ap-southeast-1/costing-balance/overview'],['用量账单','https://usercenter2-intl.console.alibabacloud.com/finance/expense-report/expense-detail']],
  openai:[['API 用量','https://platform.openai.com/usage'],['余额与账单','https://platform.openai.com/account/billing/overview']]
};
function modelAccountLinksHtml(provider){
  const links=Object.hasOwn(modelAccountLinks,provider)?modelAccountLinks[provider]:[];
  return links.length?`<p class="small model-account-links">官方查询：${links.map(([label,url])=>`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${esc(label)} ↗<span class="sr-only">（新标签页打开）</span></a>`).join(' · ')}</p>`:'';
}
function newModelService(){
  const f=$('model-service-form');f.reset();f.elements.id.value='';f.elements.priority.value=Math.min(999,Math.max(0,...state.modelServices.map(row=>row.priority))+10);
}
async function loadModelServices(){
  const data=await api('/admin/model-services');state.modelServices=data.services;
  $('model-managed-state').textContent=data.managed?'已启用网页配置。已启用且不在冷却中的服务按优先级调用；全部停用后不会自动退回本机配置。':'目前沿用本机配置；保存第一条服务后，将切换为网页管理的配置。';
  $('model-service-list').innerHTML=data.services.length?data.services.map(row=>{
    const test=row.last_test,wait=Math.max(0,Math.ceil((row.next_test_at||0)-Date.now()/1000));
    const pending=test&&['queued','started'].includes(test.result);
    const testing=state.modelTesting?.id===row.id||pending;
    return `<div class="model-service-row"><strong>${esc(row.name)}</strong> <span class="badge${row.enabled?' green':''}">${row.enabled?'已启用':'已停用'}</span><p class="small muted">${esc(row.provider_label)} · ${esc(row.model)} · Key ${esc(row.key_hint)} · 优先级 ${number(row.priority)}</p>${modelAccountLinksHtml(row.provider)}${row.cooldown_seconds?`<p class="small muted">暂缓调用此服务 · 冷却剩余 ${number(row.cooldown_seconds)} 秒</p>`:''}<p class="small muted" data-model-test-result="${esc(row.id)}" role="status">${test?`最近连接测试：${esc(test.message)}<br>${date(test.finished||test.created)}${test.elapsed_ms==null?'':' · 耗时 '+esc((test.elapsed_ms/1000).toFixed(1))+' 秒'}${test.started?' · token '+esc(number(test.total_tokens)):''}`:'尚未测试当前配置'}</p><div class="actions compact"><button type="button" data-model-test="${esc(row.id)}" ${state.modelTesting||pending||wait?'disabled':''}>${testing?'测试中…':wait?wait+'秒后重测':'测试连接'}</button><button type="button" data-model-edit="${esc(row.id)}">编辑</button><button type="button" data-model-toggle="${esc(row.id)}">${row.enabled?'停用':'启用'}</button><button type="button" class="danger" data-model-delete="${esc(row.id)}">删除</button></div></div>`;
  }).join(''):'<p class="empty">尚未添加网页管理的模型服务</p>';
  $('model-attempts').innerHTML=data.attempts.length?data.attempts.map(row=>`<tr><td>${date(row.created)}</td><td>${esc(row.name)}</td><td>${esc(row.model)}</td><td>${row.source==='model_test'?'连接测试':'实际调用'}</td><td>${number(row.attempt)}</td><td>${esc(modelResult[row.error_kind||row.status]||'未知')}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无调用记录；保存配置不会调用模型</td></tr>';
  $('model-service-list').querySelectorAll('[data-model-test]').forEach(button=>button.addEventListener('click',()=>testModelService(button.dataset.modelTest)));
  $('model-service-list').querySelectorAll('[data-model-edit]').forEach(button=>button.addEventListener('click',()=>{const row=state.modelServices.find(x=>x.id===button.dataset.modelEdit);fill('model-service-form',{...row,api_key:''});$('model-service-form').scrollIntoView({behavior:'smooth',block:'start'});}));
  $('model-service-list').querySelectorAll('[data-model-toggle]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{await ensureRecent();const row=state.modelServices.find(x=>x.id===button.dataset.modelToggle);const {name,provider,model,priority}=row;await api('/admin/model-services/'+encodeURIComponent(row.id),'PUT',{name,provider,model,priority,enabled:!row.enabled});await loadModelServices();}).catch(e=>toast(e.message))));
  $('model-service-list').querySelectorAll('[data-model-delete]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{if(await confirmation('删除此模型服务？','新请求将不再使用该服务；已发出的请求不受影响。删除全部服务后 AI 将不可用。')){await ensureRecent();await api('/admin/model-services/'+encodeURIComponent(button.dataset.modelDelete),'DELETE');if($('model-service-form').elements.id.value===button.dataset.modelDelete)newModelService();await loadModelServices();}}).catch(e=>toast(e.message))));
}
async function testModelService(id){
  if(state.modelTesting)return;
  const ticket={id,uid:state.me.user_id};state.modelTesting=ticket;
  $('model-service-list').querySelectorAll('[data-model-test]').forEach(button=>{button.disabled=true;if(button.dataset.modelTest===id)button.textContent='测试中…';});
  try{const result=await api('/admin/model-services/'+encodeURIComponent(id)+'/test','POST',{});if(state.me?.user_id===ticket.uid)toast(result.message);}
  catch(e){if(state.me?.user_id===ticket.uid)toast(e.message);}
  finally{if(state.modelTesting===ticket){state.modelTesting=null;if(state.me?.user_id===ticket.uid)await loadModelServices().catch(e=>toast(e.message));}}
}
action('new-model-service',newModelService);
$('model-provider').addEventListener('change',()=>{$('service-model').value={deepseek:'deepseek-chat',qwen:'qwen-plus',qwen_intl:'qwen-plus',openai:'gpt-4o-mini'}[$('model-provider').value];});
form('model-service-form',async(data)=>{
  await ensureRecent();const id=data.get('id'),body={name:data.get('name'),provider:data.get('provider'),model:data.get('model'),priority:Number(data.get('priority')),enabled:data.has('enabled')};
  if(data.get('api_key').trim())body.api_key=data.get('api_key').trim();
  await api('/admin/model-services'+(id?'/'+encodeURIComponent(id):''),id?'PUT':'POST',body);await loadModelServices();newModelService();toast('模型服务已保存，可点击测试连接验证');
});
function speechSettingsExample(){
  const f=$('speech-settings-form');
  if(!f.checkValidity()){$('speech-settings-example').textContent='请按范围填写：每段10–100个字符，估算语速1–10字符／秒，安全余量0–10秒。';return;}
  const n=Number(f.elements.max_chunk_chars.value),cps=Number(f.elements.chars_per_second.value),margin=Number(f.elements.safety_margin_seconds.value);
  $('speech-settings-example').textContent=`示例：${n}个字符 ÷ ${cps}字符／秒 ＋ ${margin}秒 ≈ ${(n/cps+margin).toFixed(1)}秒后提交下一段。实际片段较短时，等待也相应缩短。`;
}
$('speech-settings-form').addEventListener('input',speechSettingsExample);
form('speech-settings-form',async(data)=>{
  const settings=await api('/admin/speech-settings','PUT',numeric(data,['max_chunk_chars','chars_per_second','safety_margin_seconds']));
  fill('speech-settings-form',settings);speechSettingsExample();toast('分段播报设置已保存，下一条回答生效');
});
function renderCapacity(data){
  metrics('capacity-metrics',[["正在生成",number(data.active),'当前模型请求','条'],['等待处理',number(data.waiting),'包含同一音箱的待答问题','条'],['繁忙拒绝',number(data.rejected),'等待队列或单设备队列已满','次'],['等待超时',number(data.expired),'未调用模型','次']]);
}
form('capacity-form',async(data)=>{const r=await api('/admin/capacity','PUT',numeric(data,['model_concurrency','queue_limit','queue_timeout']));fill('capacity-form',r.limits);renderCapacity(r);toast('并发与排队设置已生效');});
function fillUserPolicy(){const u=state.admin?.users.find(x=>x.id===$('policy-user').value);if(u)fill('user-policy-form',{...u.policy,active:u.active,inherit_memory:u.memory_inherited});$('policy-user-state').textContent=u?`${u.username} · 当前已保存状态：${u.active?'正常（平台账号已启用）':'已暂停（平台账号已禁用）'}`:'暂无普通用户，请先创建试用邀请并完成注册。';$('show-user-list').hidden=!u;toggleMemoryInputs();}
function showInvite(token){$('invite-result').hidden=false;$('invite-link').value=location.origin+'/#invite='+encodeURIComponent(token);$('invite-link').select();}
// Tokens are fragments, not request URLs, and are removed immediately from browser history.
const authState={mode:'login',challenge:null,reset:null,emailChange:null};
function authMode(mode){
  authState.mode=mode;authState.challenge=null;$('email-login-form').hidden=true;
  $('auth-error').textContent='';$('forgot-result').textContent='';
  $('login-form').hidden=!['login','register'].includes(mode);$('forgot-form').hidden=mode!=='forgot';$('reset-form').hidden=mode!=='reset';
  $('registration-email').hidden=mode!=='register';$('login-form').elements.email.required=mode==='register';
  $('forgot-password').hidden=mode!=='login';$('back-login').hidden=mode==='login';
  $('auth-title').textContent={login:'登录控制台',register:'注册平台账号',forgot:'找回平台账号和密码',reset:'重置平台密码'}[mode];
  $('auth-subtitle').textContent={login:'使用平台用户名和平台密码登录，小米账号在进入后绑定。',register:'设置平台密码并验证安全邮箱，完成后即可进入控制台。',forgot:'填写注册时已验证的安全邮箱，我们会发送包含平台账号的重置邮件。',reset:'设置至少12位新密码，完成后所有设备须重新登录。'}[mode];
  $('auth-submit').textContent=mode==='register'?'发送注册验证码':'登录';
  $('login-form').elements.password.minLength=mode==='register'?12:1;
  $('login-form').elements.password.autocomplete=mode==='register'?'new-password':'current-password';
}
const fragment=new URLSearchParams(location.hash.slice(1));
state.invite=fragment.get('invite');authState.reset=fragment.get('reset');
if(location.hash)history.replaceState(null,'',location.pathname);
authMode(authState.reset?'reset':state.invite?'register':'login');
window.addEventListener('hashchange',()=>{const params=new URLSearchParams(location.hash.slice(1));if(!params.get('reset')&&!params.get('invite'))return;state.me=null;showAuth();state.invite=params.get('invite');authState.reset=params.get('reset');history.replaceState(null,'',location.pathname);authMode(authState.reset?'reset':'register');});
action('forgot-password',()=>authMode('forgot'));
action('back-login',()=>{state.invite=null;authState.reset=null;authMode('login');});
action('email-login-back',()=>authMode(state.invite?'register':'login'));
form('login-form',async(data,f)=>{
  try{
    const body={username:data.get('username'),password:data.get('password'),remember:data.has('remember')};
    if(state.invite){body.token=state.invite;body.email=data.get('email');}
    const result=await api(state.invite?'/redeem':'/login','POST',body);
    f.elements.password.value='';$('auth-error').textContent='';
    if(result.challenge_id){authState.challenge=result.challenge_id;$('login-form').hidden=true;$('email-login-form').hidden=false;$('email-login-hint').textContent=`验证码已发往 ${result.email_hint}，10分钟有效。`;$('email-login-form').elements.code.focus();}
    else{state.invite=null;f.reset();await hydrate();}
  }catch(e){$('auth-error').textContent=e.message;}
});
form('email-login-form',async(data,f)=>{
  await api('/auth/verify','POST',{challenge_id:authState.challenge,code:data.get('code')});
  state.invite=null;authState.challenge=null;f.reset();$('login-form').reset();authMode('login');await hydrate();
});
form('forgot-form',async(data)=>{$('forgot-result').textContent=(await api('/password/forgot','POST',{email:data.get('email')})).message;});
form('reset-form',async(data,f)=>{
  if(data.get('password')!==data.get('confirm_password'))throw new Error('两次输入的密码不一致');
  const result=await api('/password/reset','POST',{token:authState.reset,password:data.get('password')});
  f.reset();authState.reset=null;state.invite=null;authMode('login');toast(result.message);
});
let securityChallenge=null,securityResolve=null;
async function ensureRecent(force=false){
  if(!force&&(await api('/security')).recently_verified)return;
  if(securityResolve)throw new Error('请先完成当前邮箱验证');
  const result=await api('/security/challenge','POST');securityChallenge=result.challenge_id;
  $('security-dialog-hint').textContent=`验证码已发送至 ${result.email_hint}，10分钟有效。`;
  $('security-verify-form').reset();$('security-dialog').showModal();
  return new Promise((resolve,reject)=>{securityResolve={resolve,reject};});
}
form('security-verify-form',async(data)=>{
  await api('/security/verify','POST',{challenge_id:securityChallenge,code:data.get('code')});
  securityResolve?.resolve();securityResolve=null;$('security-dialog').close();
});
$('security-dialog').addEventListener('close',()=>{securityResolve?.reject(new Error('已取消邮箱验证'));securityResolve=null;});
action('security-cancel',()=>{$('security-dialog').close();});
function browserLabel(agent){
  const browser=/Edg\//.test(agent)?'Edge':/Firefox\//.test(agent)?'Firefox':/Chrome\//.test(agent)?'Chrome':/Safari\//.test(agent)?'Safari':'浏览器';
  const os=/iPhone|iPad/.test(agent)?'iPhone / iPad':/Android/.test(agent)?'Android':/Macintosh/.test(agent)?'macOS':/Windows/.test(agent)?'Windows':/Linux/.test(agent)?'Linux':'';
  return browser+(os?' · '+os:'');
}
async function loadSecurity(){
  const info=await api('/security');$('security-email').textContent=info.email_hint;
  $('security-sessions').innerHTML=info.sessions.map(s=>`<p class="small">${esc(browserLabel(s.agent))} · ${esc(s.address)} · ${esc(date(s.created))}${s.current?' · 当前会话':''}</p>`).join('');
  $('security-trusted').innerHTML=info.trusted.length?info.trusted.map(d=>`<p class="small">${esc(browserLabel(d.agent))} · 最近登录 ${esc(date(d.last_login))} <button data-forget="${esc(d.id)}">不再信任</button></p>`).join(''):'<p class="muted small">没有已记住的设备。</p>';
  $('security-trusted').querySelectorAll('[data-forget]').forEach(b=>b.addEventListener('click',()=>busy(b,async()=>{await api('/security/trusted/'+encodeURIComponent(b.dataset.forget),'DELETE');await loadSecurity();}).catch(e=>toast(e.message))));
}
action('verify-security',async()=>{await ensureRecent();toast('邮箱身份已验证，10分钟内可进行敏感操作');});
action('revoke-sessions',async()=>{await api('/security/sessions','DELETE');await loadSecurity();toast('其他会话和全部可信设备已撤销');});
form('change-email-form',async(data)=>{await ensureRecent();const r=await api('/security/email','POST',{email:data.get('email')});authState.emailChange=r.challenge_id;$('change-email-code-form').hidden=false;toast('验证码已发送至新邮箱');});
form('change-email-code-form',async(data,f)=>{await ensureRecent();const r=await api('/security/email/verify','POST',{challenge_id:authState.emailChange,code:data.get('code')});f.reset();state.me=null;showAuth();authMode('login');toast(r.message);});
form('binding-form',async(data,f)=>{
  const mode=state.bindingMode;
  const payload={password:data.get('xiaomi_secret')};
  if(mode!=='reverify')payload.username=data.get('xiaomi_account');
  // Snapshot only what the request needs, then clear the live field BEFORE any
  // network wait. Never leave Xiaomi's password in a submitted page form.
  f.elements.xiaomi_secret.value='';f.elements.xiaomi_secret.readOnly=true;
  data.delete('xiaomi_secret');
  try{
    if(mode==='replace')await ensureRecent();
    state.login=await api(mode==='reverify'?'/xiaomi/reverify':mode==='replace'?'/xiaomi/replace':'/xiaomi/login','POST',payload);
    if(state.login.state==='already_valid'){resetBindingForm();state.me=await api('/me');toast(state.login.message);}
    else $('login-state').textContent=mode==='replace'?'正在验证新绑定账号，原绑定保持不变…':'正在重新绑定当前小米账号…';
    updateConnection();
  }finally{payload.password='';f.elements.xiaomi_secret.value='';f.elements.xiaomi_secret.readOnly=true;}
});
form('otp-form',async(data,f)=>{await api('/xiaomi/code','POST',{job_id:state.login.id,code:data.get('code')});f.reset();$('otp-form').hidden=true;$('login-state').textContent='正在校验验证码…';});
action('resume-reverify',async()=>{state.reverifyDismissed=false;state.bindingMode='reverify';state.bindingEditing=true;updateConnection();$('xiaomi-secret-input').focus();});
action('replace-account',async()=>{resetBindingForm();state.bindingMode='replace';state.bindingEditing=true;$('account-action-state').textContent='验证新绑定账号期间，现有绑定保持不变。';updateConnection();$('xiaomi-account-input').focus();});
async function cancelAccountOperation(){
  const active=['logging_in','verification_required','verifying','awaiting_confirmation','applying'].includes(state.login?.state);
  const mode=active?state.login.mode:state.bindingMode;
  const result=active?await api('/xiaomi/login','DELETE'):{state:'cancelled'};state.login=result.state==='completed'?result:null;
  resetBindingForm();if(mode==='reverify')state.reverifyDismissed=true;$('otp-form').reset();$('otp-form').hidden=true;
  const message=result.state==='completed'?'账号更新已完成，当前绑定已生效。':mode==='replace'?'已取消更换，原有绑定保持不变。':mode==='reverify'?'已暂缓重新绑定，原账号及设备选择已保留。':'已暂缓绑定小米账号。';
  state.me=await api('/me');if(result.state==='completed')await fetchDevices(true);
  $('account-action-state').textContent=message;updateConnection();toast(message);
}
action('cancel-login',cancelAccountOperation);
action('cancel-active-login',cancelAccountOperation);
action('cancel-switch',cancelAccountOperation);
action('confirm-switch',async()=>{await ensureRecent();state.login=await api('/xiaomi/replace/confirm','POST',{job_id:state.login.id});$('account-action-state').textContent='正在切换，等待旧账号当前回答结束…';updateConnection();});
action('unbind',async()=>{if(await confirmation('解绑小米账号？','将停止该账号所有设备的监听并删除保存的绑定凭据，历史记录会保留。')){await ensureRecent();await api('/xiaomi','DELETE');state.selected.clear();await hydrate();toast('已解绑小米账号');}});
action('refresh-devices',async()=>{await api('/devices/refresh','POST');await fetchDevices();toast('设备已刷新，保留当前选择；新增设备请手动勾选');});
action('select-all',async()=>{state.selected=new Set(state.devices.filter(d=>d.present&&d.supported).map(d=>d.id));renderDevices();});
action('select-none',async()=>{state.selected.clear();renderDevices();});
$('save-devices').addEventListener('click',async()=>{
  if($('save-devices').disabled)return;
  state.deviceSaving=true;updateDeviceSaveState();
  try{
    await api('/devices','PUT',{devices:[...state.selected]});await fetchDevices();await refreshUsage();toast('设备监听配置已更新');
  }catch(e){toast(e.message);}
  finally{state.deviceSaving=false;updateDeviceSaveState();}
});
form('dialogue-form',async(data)=>{const body={routing_mode:data.get('routing_mode')};if(body.routing_mode==='keyword')body.wake_words=data.get('wake_words').split('\n').map(w=>w.trim()).filter(Boolean);await api('/dialogue','PUT',body);state.me=await api('/me');renderDialogue();toast('调用方式已保存，对之后的新问题生效');});
form('prefix-form',async(data)=>{await api('/dialogue','PUT',{answer_prefix:data.get('answer_prefix').trim()});state.me=await api('/me');renderDialogue();toast('播报前缀已保存');});
form('clear-form',async(data)=>{await api('/dialogue/clear','POST',{device_id:data.get('device_id')});toast('已清空所选设备上下文');});
action('delete-all-history',async()=>{if(await confirmation('删除全部对话历史？','这些记录将无法恢复。当前内存中的对话上下文不会改变。')){await api('/history','DELETE');await loadHistory(true);toast('已删除全部历史');}});
action('more-history',async()=>loadHistory());
action('create-invite',async()=>showInvite((await api('/admin/invites','POST')).token));
action('copy-invite',async()=>{await navigator.clipboard.writeText($('invite-link').value);toast('链接已复制，请通过可信渠道发送给用户');});
function toggleMemoryInputs(){const form=$('user-policy-form');for(const key of ['ttl_seconds','max_turns'])form.elements[key].disabled=form.elements.inherit_memory.checked;}
$('inherit-memory').addEventListener('change',toggleMemoryInputs);
function renderFailureRules(){
  $('fallback-rule-list').innerHTML=state.admin.fallback_rules.map(r=>`<div class="rule-row"><div><strong>${esc(r.name)}</strong><span class="badge${r.enabled?' green':''}">${r.enabled?'已启用':'已停用'}</span><p class="small muted">${r.match_type==='exact'?'完整回复':'同时包含所有短语'}：${esc(r.content.split('\n').join(' / '))}</p></div><div class="actions"><button data-rule-edit="${esc(r.id)}">编辑</button><button data-rule-toggle="${esc(r.id)}">${r.enabled?'停用':'启用'}</button><button class="danger" data-rule-delete="${esc(r.id)}">删除</button></div></div>`).join('')||'<p class="muted">尚无话术，失败接管不会调用 AI。</p>';
  $('fallback-rule-list').querySelectorAll('[data-rule-edit]').forEach(b=>b.addEventListener('click',()=>{const rule=state.admin.fallback_rules.find(r=>r.id===b.dataset.ruleEdit);fill('fallback-rule-form',rule);$('fallback-rule-form').scrollIntoView({behavior:'smooth',block:'center'});}));
  $('fallback-rule-list').querySelectorAll('[data-rule-toggle]').forEach(b=>b.addEventListener('click',()=>busy(b,async()=>{const rule=state.admin.fallback_rules.find(r=>r.id===b.dataset.ruleToggle);const {id,...body}=rule;await api('/admin/fallback-rules/'+encodeURIComponent(id),'PUT',{...body,enabled:!rule.enabled});await loadAdmin();}).catch(e=>toast(e.message))));
  $('fallback-rule-list').querySelectorAll('[data-rule-delete]').forEach(b=>b.addEventListener('click',()=>busy(b,async()=>{if(await confirmation('删除这条话术？','删除后将不再按此规则自动接管。')){await api('/admin/fallback-rules/'+encodeURIComponent(b.dataset.ruleDelete),'DELETE');resetFallbackForm();await loadAdmin();}}).catch(e=>toast(e.message))));
}
function resetFallbackForm(){const f=$('fallback-rule-form');f.reset();f.elements.id.value='';}
action('new-fallback-rule',resetFallbackForm);
form('fallback-rule-form',async(data,f)=>{const id=data.get('id');await api('/admin/fallback-rules'+(id?'/'+encodeURIComponent(id):''),id?'PUT':'POST',{name:data.get('name'),match_type:data.get('match_type'),content:data.get('content'),enabled:data.has('enabled')});resetFallbackForm();await loadAdmin();toast('话术已保存');});
form('fallback-test-form',async(data)=>{const r=await api('/admin/fallback-rules/test','POST',{answer:data.get('answer')});$('fallback-test-result').textContent=(r.takeover?'匹配成功：':'未匹配：')+r.reason+(r.matched.length?'（'+r.matched.map(m=>m.name).join('、')+'）':'');});
$('policy-user').addEventListener('change',fillUserPolicy);
action('show-user-list',()=>{$('admin-user-list-heading').scrollIntoView({behavior:'smooth',block:'start'});});
form('user-policy-form',async(data)=>{const inherit=data.has('inherit_memory');await api('/admin/users/'+data.get('uid'),'PUT',{...numeric(data,['daily_limit','minute_limit','max_output_tokens']),...(!inherit?numeric(data,['ttl_seconds','max_turns']):{}),inherit_memory:inherit,active:data.has('active')});await loadAdmin();toast('用户规则已保存');});
form('defaults-form',async(data)=>{const values={...numeric(data,['daily_limit','minute_limit','max_output_tokens','retention_days','ttl_seconds','max_turns'])};if(values.retention_days<state.admin.defaults.retention_days&&!(await confirmation('缩短历史保存期限？','超出新期限的历史会在下一次清理时删除，无法恢复。')))return;await api('/admin/defaults','PUT',values);state.me=await api('/me');await loadAdmin();toast('平台默认规则已更新');});
form('password-form',async(data,f)=>{await ensureRecent();await api('/password','POST',{username:state.me.username,password:data.get('password')});f.reset();await hydrate();toast('密码已更新，其他会话已失效');});
action('logout',async()=>{await api('/logout','POST');location.reload();});
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.view).catch(e=>toast(e.message))));document.querySelectorAll('[data-go]').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.go).catch(e=>toast(e.message))));
async function tick(){if(!state.me||document.hidden||state.refreshing)return;state.refreshing=true;try{
  state.me=await api('/me');const job=await api('/xiaomi/login');state.login=job;updateConnection();
  if(state.view==='devices'){
    const recipient=job.recipient||(job.method==='短信'?'小米账号绑定的安全手机（可能不是你当前使用的手机号）':'小米账号绑定的安全邮箱');
    const otpMessage=`需要小米${job.method||''}验证码，请查看${recipient}收到的${job.method||'验证码'}，并在下方填写。这不是平台登录的邮箱验证码。`;
    const messages={logging_in:'正在绑定小米账号…',verification_required:otpMessage,verifying:'正在验证…',completed:job.message,awaiting_confirmation:job.message,applying:'正在应用账号更新，等待当前回答结束…',expired:job.message,failed:job.message,cancelled:'本次绑定已取消'};
    $('login-state').textContent=messages[job.state]||'';$('otp-form').hidden=job.state!=='verification_required';
  }
  if(job.state==='completed'&&state.loginCompleted!==job.id){state.loginCompleted=job.id;resetBindingForm();state.me=await api('/me');updateConnection();await fetchDevices(true);$('account-action-state').textContent=job.message;toast(job.message||'账号更新完成');}
  if(state.view==='dialogue'){const seconds=state.me.policy.ttl_seconds;$('memory-ttl').textContent=seconds%60===0?`${seconds/60} 分钟`:`${seconds} 秒`;$('memory-turns').textContent=`${state.me.policy.max_turns} 轮`;}
  if(state.view==='overview')await refreshUsage();
  if(state.view==='history')await refreshHistory();
  if(state.view==='admin'){renderCapacity(await api('/admin/capacity'));if(state.adminTab==='models')await loadModelServices();}
}catch(e){if(e.status!==401)console.info('状态更新暂不可用');}finally{state.refreshing=false;}}
displayWorkspace(false);
if(!state.invite&&!authState.reset)hydrate().catch(e=>{if(e.status!==401)toast(e.message);});
setInterval(tick,4000);

document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
