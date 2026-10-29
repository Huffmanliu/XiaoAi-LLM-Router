'use strict';
function initSupport(root, mode){
  const help=mode!=='admin',embedded=mode==='embedded';
  const elements=new Map([...root.querySelectorAll('[id]')].map(item=>[item.id,item]));
  const el=id=>elements.get(id);
  let disposed=false;
  const categories = {problem:'使用问题',suggestion:'功能建议',device:'机型支持申请',other:'其他'};
  const statuses = {pending:'待处理',processing:'处理中',replied:'已回复',closed:'已关闭'};
  const stamp = value => new Date(value * 1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const nonce = () => crypto.randomUUID().replaceAll('-','');
  let member = null, memberOwner = null, mineEpoch = 0, memberCheck = 0, minePage = 1, minePages = 1, submission = null;
  let adminEpoch = 0, adminPage = 1, adminPages = 1;
  let memoryEpoch = 0;
  const drafts = new Map();
  const adminOwner = () => typeof state !== 'undefined' ? state.me?.user_id : null;
  async function request(path, method='GET', data, csrf='') {
    const response = await fetch('/api'+path,{method,credentials:'same-origin',headers: data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(data===undefined?{}:{body:JSON.stringify(data)})});
    const value = await response.json();
    if(!response.ok){
      if(response.status===401){
        if(help)hideMember('请先登录平台，再返回这里提交或查看反馈。');
        else if(typeof showAuth==='function'){state.me=null;showAuth();}
      }
      const error=new Error(value.error||'操作未完成，请稍后重试');error.status=response.status;throw error;
    }
    return value;
  }
  function ticket(row, administrator=false) {
    const article=node('article',undefined,'feedback-ticket');
    const meta=node('div',undefined,'feedback-meta');
    meta.append(node('strong',categories[row.category]),node('span',statuses[row.status],'badge'),node('span',stamp(row.created)));
    if(administrator)meta.append(node('span','提交者：'+row.username));
    article.append(meta,node('p',row.content,'feedback-content'));
    if(row.device)article.append(node('p','关联设备：'+row.device.name+' · '+row.device.hardware,'small muted'));
    if(row.reply){const reply=node('div',undefined,'feedback-reply');reply.append(node('strong','管理员回复'),node('p',row.reply,'feedback-content'));article.append(reply);}
    article.append(node('p','更新于 '+stamp(row.updated),'small muted'));
    return article;
  }
  function pagination(prefix, data){
    el(prefix+'-page').textContent=`第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 条`;
    el(prefix+'-prev').disabled=data.page<=1;el(prefix+'-next').disabled=data.page>=data.pages;
  }
  function renderMemorySettings(values, personal=false){
    const {ttl_seconds:seconds,max_turns:turns,retention_days:days}=values||{};
    if(![seconds,turns,days].every(value=>Number.isInteger(value)&&value>0))throw new Error('当前设置暂不可用');
    const duration=seconds%60===0?`${seconds/60} 分钟`:`${Math.floor(seconds/60)?Math.floor(seconds/60)+' 分 ':''}${seconds%60} 秒`;
    const source=personal?'你的当前生效设置':'平台当前默认设置（账号的生效值请登录后查看）';
    el('faq-memory-settings').textContent=`${source}：上下文闲置 ${duration} 后过期，最多保留最近 ${turns} 轮问答；对话历史保留 ${days} 天。`;
  }
  async function loadDefaultMemory(){
    const epoch=++memoryEpoch;
    try{
      const response=await fetch('/api/help/settings',{credentials:'same-origin'});
      if(!response.ok)throw new Error('当前设置暂不可用');
      const values=await response.json();
      if(epoch===memoryEpoch)renderMemorySettings(values);
    }catch(error){if(epoch===memoryEpoch)el('faq-memory-settings').textContent='暂时无法读取当前设置，请点击“更新设置”重试；登录后也可在“对话设置”查看记忆时长和轮数。';}
  }
  function hideMember(message){
    if(disposed)return;
    member=null;mineEpoch++;memberCheck++;
    el('feedback-member').hidden=true;el('feedback-login-note').hidden=false;
    el('feedback-auth-message').textContent=message;el('feedback-list').replaceChildren();
    void loadDefaultMemory();
  }
  async function identify(){
    const check=++memberCheck;
    const me=await request('/me');
    if(check!==memberCheck)return null;
    if(memberOwner&&memberOwner!==me.user_id){
      el('feedback-form').reset();el('feedback-submit-result').textContent='';submission=null;minePage=1;
      el('feedback-list').replaceChildren();mineEpoch++;
    }
    memberOwner=me.user_id;member=me;
    memoryEpoch++;
    try{renderMemorySettings({...me.policy,retention_days:me.retention_days},true);}
    catch(error){el('faq-memory-settings').textContent='暂时无法读取当前设置，请点击“更新设置”重试。';}
    el('feedback-account').textContent='当前平台账号：'+me.username;
    el('feedback-member').hidden=false;el('feedback-login-note').hidden=true;
    return me;
  }
  async function loadDevices(){
    const owner=member?.user_id;if(!owner)return;
    try{
      const rows=await request('/devices');if(member?.user_id!==owner)return;
      const select=el('feedback-device'),selected=select.value;
      select.replaceChildren(new Option('不关联设备',''));
      rows.filter(row=>row.present).forEach(row=>select.add(new Option(row.name+' · '+row.hardware,row.id)));
      if([...select.options].some(option=>option.value===selected))select.value=selected;
      select.disabled=false;
    }catch(error){if(member?.user_id===owner){el('feedback-device').replaceChildren(new Option('设备暂不可用，可先提交文字反馈',''));el('feedback-device').disabled=true;}}
  }
  async function loadMine(){
    const me=await identify();if(!me)return;
    const epoch=++mineEpoch;
    el('feedback-list-message').textContent='正在加载…';
    try{
      const data=await request('/feedback?page='+minePage);
      if(epoch!==mineEpoch||member?.user_id!==me.user_id)return;
      minePage=data.page;minePages=data.pages;
      const list=el('feedback-list');list.replaceChildren();
      if(!data.items.length)list.append(node('p','还没有反馈记录。','empty'));
      data.items.forEach(row=>list.append(ticket(row)));
      pagination('feedback',data);el('feedback-list-message').textContent='记录已更新，回复时间以实际处理为准。';
    }catch(error){if(epoch===mineEpoch)el('feedback-list-message').textContent=error.message;}
  }
  function helpTab(key, changeHash=true){
    key=key==='feedback'?'feedback':'faq';
    root.querySelectorAll('[data-help-tab]').forEach(tab=>{const selected=tab.dataset.helpTab===key;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;el(tab.getAttribute('aria-controls')).hidden=!selected;});
    if(changeHash&&!embedded)history.replaceState(null,'','#'+key);
  }
  if(help){
    root.querySelectorAll('[data-help-tab]').forEach(tab=>{
      tab.addEventListener('click',()=>helpTab(tab.dataset.helpTab));
      tab.addEventListener('keydown',event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        event.preventDefault();const key=event.key==='Home'?'faq':event.key==='End'?'feedback':tab.dataset.helpTab==='faq'?'feedback':'faq';helpTab(key);el('help-tab-'+key).focus();
      });
    });
    root.querySelector('[data-help-open-feedback]').addEventListener('click',()=>{helpTab('feedback');el('help-tab-feedback').focus();});
    const applyHash=()=>{const id=location.hash.slice(1);helpTab(id,false);if(id.startsWith('faq-')){const detail=el(id);if(detail?.tagName==='DETAILS'){detail.open=true;detail.scrollIntoView();}}};
    if(!embedded){window.addEventListener('hashchange',applyHash);applyHash();}
    async function refreshMember(){
      try{await loadMine();await loadDevices();}
      catch(error){if(error.status!==401)hideMember('暂时无法检查登录状态，请稍后重新检查。');}
    }
    el('feedback-check-login').addEventListener('click',refreshMember);
    el('feedback-refresh').addEventListener('click',refreshMember);
    el('faq-memory-refresh').addEventListener('click',refreshMember);
    for(const direction of ['prev','next'])el('feedback-'+direction).addEventListener('click',()=>{minePage=Math.max(1,Math.min(minePages,minePage+(direction==='next'?1:-1)));refreshMember();});
    const form=el('feedback-form');
    form.addEventListener('input',()=>{submission=null;});form.addEventListener('change',()=>{submission=null;});
    form.addEventListener('submit',async event=>{
      event.preventDefault();const button=form.querySelector('button[type=submit]');if(button.disabled)return;
      const owner=member?.user_id;
      const body={submission_id:submission||nonce(),category:form.elements.category.value,content:form.elements.content.value.trim(),device_id:form.elements.device_id.value||null};
      if(!body.content){el('feedback-submit-result').textContent='请填写反馈内容';return;}
      submission=body.submission_id;
      const controls=[...form.elements],disabled=controls.map(control=>control.disabled);controls.forEach(control=>control.disabled=true);
      el('feedback-submit-result').textContent='正在提交…';
      try{
        const me=await identify();if(!me)return;
        if(me.user_id!==owner)throw new Error('登录账号已变化，请重新填写反馈。');
        await request('/feedback','POST',body,me.csrf);
        if(member?.user_id!==owner)return;
        form.reset();submission=null;minePage=1;
        el('feedback-submit-result').textContent='反馈已提交。请在“我的反馈”查看处理状态和管理员回复。';
        try{await loadMine();}
        catch(error){if(member?.user_id===owner)el('feedback-list-message').textContent='反馈已提交，记录暂时无法刷新，请稍后点击“刷新记录”。';}
      }catch(error){if(!member||member.user_id===owner)el('feedback-submit-result').textContent=error.message+'；未成功提交的输入会保留。';}
      finally{controls.forEach((control,index)=>control.disabled=disabled[index]);}
    });
    const ready=refreshMember();
    return {ready,refresh:refreshMember,dispose:()=>{
      disposed=true;mineEpoch++;memberCheck++;memoryEpoch++;member=null;
      form.reset();el('feedback-list').replaceChildren();
    }};
  } else {
    window.resetAdminFeedback=()=>{
      adminEpoch++;drafts.clear();adminPage=1;adminPages=1;
      el('admin-feedback-list').replaceChildren();el('admin-feedback-message').textContent='';el('admin-feedback-page').textContent='';
      el('admin-feedback-filter').value='';el('admin-feedback-prev').disabled=true;el('admin-feedback-next').disabled=true;
    };
    window.loadAdminFeedback=async()=>{
      const owner=adminOwner();if(!owner)return;
      const epoch=++adminEpoch;
      el('admin-feedback-message').textContent='正在加载…';
      const query=new URLSearchParams({page:adminPage});if(el('admin-feedback-filter').value)query.set('status',el('admin-feedback-filter').value);
      try{
        const data=await request('/admin/feedback?'+query);
        if(epoch!==adminEpoch||adminOwner()!==owner)return;
        adminPage=data.page;adminPages=data.pages;
        const list=el('admin-feedback-list');list.replaceChildren();
        if(!data.items.length)list.append(node('p','当前条件下没有反馈。','empty'));
        for(const row of data.items){
          const article=ticket(row,true),form=node('form');form.className='feedback-handle';
          const saved=drafts.get(row.id);if(saved)saved.version=row.version;
          const draft=saved||{status:row.status,reply:row.reply,version:row.version};
          const statusLabel=node('label','处理状态'),select=node('select');select.name='status';
          Object.entries(statuses).forEach(([key,label])=>select.add(new Option(label,key)));select.value=draft.status;statusLabel.append(select);
          const replyLabel=node('label','回复用户'),reply=node('textarea');reply.name='reply';reply.maxLength=2000;reply.rows=3;reply.value=draft.reply;reply.placeholder='回复对提交者可见，请勿填写密码、验证码或内部凭据。';replyLabel.append(reply);
          const button=node('button','保存处理结果','primary');button.type='submit';const result=node('p',saved?'已保留未保存输入，请核对上方最新状态和已保存回复。':'','small');result.setAttribute('role','status');result.setAttribute('aria-live','polite');
          form.append(statusLabel,replyLabel,button,result);article.append(form);list.append(article);
          form.addEventListener('input',()=>drafts.set(row.id,{status:select.value,reply:reply.value,version:draft.version}));
          form.addEventListener('change',()=>drafts.set(row.id,{status:select.value,reply:reply.value,version:draft.version}));
          form.addEventListener('submit',async event=>{
            event.preventDefault();if(button.disabled)return;
            const current={status:select.value,reply:reply.value,version:draft.version};drafts.set(row.id,current);
            [select,reply,button].forEach(control=>control.disabled=true);result.textContent='正在保存…';
            try{
              await request('/admin/feedback/'+encodeURIComponent(row.id),'PUT',current,state.csrf);
              if(adminOwner()!==owner)return;
              if(drafts.get(row.id)===current)drafts.delete(row.id);
              await window.loadAdminFeedback();
            }catch(error){if(adminOwner()===owner)result.textContent=error.message;}
            finally{[select,reply,button].forEach(control=>control.disabled=false);}
          });
        }
        pagination('admin-feedback',data);el('admin-feedback-message').textContent='列表已更新。刷新或切换分类会保留未保存的回复，退出登录后清除。';
      }catch(error){if(epoch===adminEpoch&&adminOwner()===owner)el('admin-feedback-message').textContent=error.message;}
    };
    el('admin-feedback-refresh').addEventListener('click',()=>window.loadAdminFeedback());
    el('admin-feedback-filter').addEventListener('change',()=>{adminPage=1;window.loadAdminFeedback();});
    for(const direction of ['prev','next'])el('admin-feedback-'+direction).addEventListener('click',()=>{adminPage=Math.max(1,Math.min(adminPages,adminPage+(direction==='next'?1:-1)));window.loadAdminFeedback();});
  }
}

if(document.body.dataset.supportPage==='true'){
  initSupport(document.querySelector('.help-shell'),'public');
}else{
  initSupport($('view-admin'),'admin');
  let instance=null,loading=null,generation=0;
  window.resetSupportHelp=()=>{
    generation++;instance?.dispose();instance=null;loading=null;
    $('view-help').replaceChildren();
  };
  window.loadSupportHelp=async()=>{
    if(instance)return instance.refresh();
    if(loading)return loading;
    const owner=state.me?.user_id,epoch=generation,view=$('view-help');
    if(!owner)return;
    view.textContent='正在加载帮助与反馈…';
    const task=(async()=>{
      const response=await fetch('/help',{credentials:'same-origin'});
      if(!response.ok)throw new Error('帮助页面暂时无法加载，请再次点击菜单重试。');
      const documentText=await response.text();
      const parsed=new DOMParser().parseFromString(documentText,'text/html');
      const content=parsed.querySelector('[data-help-content]');
      if(!content)throw new Error('帮助页面暂时无法加载，请刷新后重试。');
      if(epoch!==generation||state.me?.user_id!==owner)return;
      view.replaceChildren(document.importNode(content,true));
      instance=initSupport(view,'embedded');
      await instance.ready;
    })();
    loading=task;
    try{await task;}
    catch(error){const message='帮助页面暂时无法加载，请再次点击菜单重试。';if(epoch===generation)view.textContent=message;throw new Error(message);}
    finally{if(loading===task)loading=null;}
  };
}
