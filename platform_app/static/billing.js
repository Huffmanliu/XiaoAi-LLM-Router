'use strict';
const billingState={data:null,admin:null,page:1,settingsLoaded:false,plan:null,order:null,key:null,busy:false,uid:null,lastRefresh:0};
const cny=cents=>'¥'+(Number(cents)/100).toFixed(2);
const paymentNames={alipay:'支付宝',wechat:'微信支付'};
const paymentStates={creating:'下单中',pending:'待支付',uncertain:'结果待核对',paid:'已支付',closed:'已关闭',refunding:'退款处理中',refunded:'已退款',refund_review:'退款待核对'};
function orderState(order){return order.payment_expired?'二维码已到期，待核对':paymentStates[order.state]||'待核对';}
function resetBilling(){
  billingState.data=null;billingState.admin=null;billingState.uid=null;billingState.order=null;billingState.plan=null;billingState.key=null;billingState.settingsLoaded=false;
  if($('payment-dialog').open)$('payment-dialog').close();
  $('payment-qr').removeAttribute('src');$('billing-orders').replaceChildren();$('billing-admin-orders').replaceChildren();
  $('billing-settings-form').reset();newBillingPlan();
}
async function loadBilling(){
  const uid=state.me?.user_id;if(!uid)return;
  const data=await api('/billing');if(state.me?.user_id!==uid)return;
  billingState.data=data;billingState.uid=uid;billingState.lastRefresh=Date.now();
  $('billing-status').textContent=data.admin_exempt?'管理员账号，免付费限制。':data.current?'服务已开通':data.enabled?'尚无生效中的套餐，可先完成设备绑定后购买。':'当前未启用收费，继续按现有用户规则使用。';
  $('billing-current').innerHTML=data.current?`<strong>${esc(data.current.plan_name)}</strong><p>服务至 ${esc(date(data.current.expires))} · 套餐每日 ${number(data.current.daily_limit)} 次 AI 提问</p>`:'<p class="muted">暂无生效中的付费套餐。</p>';
  if(data.queued.length)$('billing-current').innerHTML+=data.queued.map(g=>`<p class="small muted">待生效：${esc(g.plan_name)} · ${esc(date(g.starts))} 至 ${esc(date(g.expires))}</p>`).join('');
  $('billing-terms').textContent=data.terms||'尚未发布购买与退款说明。';$('billing-support').textContent=data.support?'联系渠道：'+data.support:'';
  const available=data.enabled&&data.channels.some(c=>c.ready);
  $('billing-plans').innerHTML=data.plans.length?data.plans.map(p=>`<article class="card billing-plan"><h2>${esc(p.name)}</h2><div class="billing-price">${cny(p.price_cents)}<small> / ${p.days}天</small></div><p>每日 ${number(p.daily_limit)} 次 AI 提问</p><p class="small muted">输出长度、请求频率和对话记忆沿用当前账号规则；单独额度限制仍生效。续购顺延，不自动扣费。</p><button class="primary" type="button" data-buy-plan="${esc(p.id)}" ${available?'':'disabled'}>${available?'选择套餐':'暂未开放购买'}</button></article>`).join(''):'<article class="card"><p class="muted">暂时没有上架的套餐。</p></article>';
  $('billing-plans').querySelectorAll('[data-buy-plan]').forEach(button=>button.addEventListener('click',()=>openCheckout(button.dataset.buyPlan)));
  $('billing-orders').innerHTML=data.orders.length?data.orders.map(o=>`<tr><td>${esc(o.id)}<br><small>${esc(date(o.created))}</small></td><td>${esc(o.plan_name)}</td><td>${cny(o.amount_cents)}</td><td>${paymentNames[o.provider]||'—'}</td><td>${esc(orderState(o))}${o.note?`<br><small>${esc(o.note)}</small>`:''}</td><td><button type="button" data-open-order="${esc(o.id)}">查看 / 查询</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无订单</td></tr>';
  $('billing-orders').querySelectorAll('[data-open-order]').forEach(button=>button.addEventListener('click',()=>{const o=data.orders.find(o=>o.id===button.dataset.openOrder);billingState.order=o;billingState.plan=null;$('payment-title').textContent='订单详情';$('payment-checkout-form').hidden=true;renderPaymentOrder(o);$('payment-dialog').showModal();}));
}
function openCheckout(id){
  const data=billingState.data,plan=data?.plans.find(p=>p.id===id);if(!plan)return;
  billingState.plan=plan;billingState.termsVersion=data.terms_version;billingState.order=null;billingState.key=crypto.randomUUID();
  $('payment-title').textContent='购买 '+plan.name;$('payment-detail').textContent=`${cny(plan.price_cents)} · ${plan.days}天 · 每日${plan.daily_limit}次。已有服务到期后依次生效。`;
  $('payment-purchase-terms').textContent=data.terms;$('payment-checkout-form').reset();
  $('payment-provider').innerHTML=data.channels.map(c=>`<option value="${esc(c.id)}" ${c.ready?'':'disabled'}>${esc(c.name)}${c.ready?'':'（暂未开通）'}</option>`).join('');
  $('payment-provider').value=data.channels.find(c=>c.ready)?.id||'';
  $('payment-checkout-form').hidden=false;$('payment-order-area').hidden=true;$('payment-qr').hidden=true;$('payment-qr').removeAttribute('src');
  $('payment-dialog').showModal();
}
function renderPaymentOrder(order){
  $('payment-detail').textContent=`${order.id} · ${esc(paymentNames[order.provider]||'')} · ${cny(order.amount_cents)} · ${order.days}天 / 每日${order.daily_limit}次`;
  $('payment-order-area').hidden=false;$('payment-order-state').textContent=orderState(order)+(order.note?' · '+order.note:'');
  $('payment-qr').hidden=!order.qr_available;
  if(order.qr_available)$('payment-qr').src='/api/billing/orders/'+encodeURIComponent(order.id)+'/qr';else $('payment-qr').removeAttribute('src');
  $('payment-instructions').textContent=order.page_payment_available?'点击前往支付宝付款，在新页面完成支付。付款结果由服务端核验，此页面不会自动续费。':'请用所选支付应用扫码。二维码有效期15分钟，付款结果由服务端核验后自动更新；此页面不会发起自动续费。';
  $('payment-page-link').hidden=!order.page_payment_available;
  if(order.page_payment_available)$('payment-page-link').href='/api/billing/orders/'+encodeURIComponent(order.id)+'/pay';else $('payment-page-link').removeAttribute('href');
  $('payment-cancel-order').hidden=!order.page_payment_available;
  $('payment-query').hidden=['closed','refunded'].includes(order.state);
}
form('payment-checkout-form',async data=>{
  const plan=billingState.plan;if(!plan)return;
  const uid=state.me.user_id;
  const order=await api('/billing/orders','POST',{plan_id:plan.id,provider:data.get('provider'),client_key:billingState.key,accept_terms:data.has('accept_terms'),plan_version:plan.updated,terms_version:billingState.termsVersion});
  if(state.me?.user_id!==uid)return;
  billingState.order=order;$('payment-checkout-form').hidden=true;renderPaymentOrder(order);await loadBilling();
});
async function queryPayment(){
  const order=billingState.order,uid=state.me?.user_id;if(!order||!uid)return;
  const updated=await api('/billing/orders/'+encodeURIComponent(order.id)+'/refresh','POST');
  if(state.me?.user_id!==uid||billingState.order?.id!==order.id)return;
  billingState.order=updated;renderPaymentOrder(updated);await loadBilling();
}
action('payment-query',queryPayment);
action('payment-close',()=>{$('payment-dialog').close();});
$('payment-dialog').addEventListener('close',()=>{$('payment-qr').removeAttribute('src');billingState.order=null;billingState.plan=null;});
action('billing-refresh',loadBilling);
function newBillingPlan(){$('billing-plan-form').reset();$('billing-plan-form').elements.id.value='';}
async function loadAdminBilling(refreshSettings=false){
  if(state.me?.role!=='admin')return;const uid=state.me.user_id;
  const data=await api('/admin/billing?page='+billingState.page);if(state.me?.user_id!==uid)return;billingState.admin=data;
  $('billing-channels').innerHTML=data.channels.map(c=>`<p><strong>${esc(c.name)}</strong> · <span class="badge${c.ready?' green':''}">${c.ready?'参数已配置':'尚未就绪'}</span>${c.reason?` <small>${esc(c.reason)}</small>`:''}</p>`).join('');
  if(!billingState.settingsLoaded||refreshSettings){fill('billing-settings-form',data.settings);billingState.settingsLoaded=true;}
  $('billing-admin-plans').innerHTML=data.plans.map(p=>`<div class="rule-row"><div><strong>${esc(p.name)}</strong><p>${cny(p.price_cents)} / ${p.days}天 · 每日${number(p.daily_limit)}次 · ${p.enabled?'已上架':'未上架'}</p></div><button type="button" data-edit-plan="${esc(p.id)}">编辑</button></div>`).join('')||'<p class="muted">请先添加套餐，默认不代设正式售价。</p>';
  $('billing-admin-plans').querySelectorAll('[data-edit-plan]').forEach(button=>button.addEventListener('click',()=>{const p=data.plans.find(p=>p.id===button.dataset.editPlan);fill('billing-plan-form',{...p,price:(p.price_cents/100).toFixed(2)});}));
  metrics('billing-admin-metrics',[['订单数',number(data.summary.orders),'全部状态','笔'],['已支付订单金额',cny(data.summary.paid_cents),'不含退款处理中订单',''],['已确认退款',cny(data.summary.refunded_cents),'以已核验结果统计',''],['待核对',number(data.summary.needs_review),'下单不明或退款处理中','笔']]);
  $('billing-admin-orders').innerHTML=data.orders.length?data.orders.map(o=>`<tr><td>${esc(o.id)}<br><small>${esc(o.username)} · ${esc(date(o.created))}</small></td><td>${esc(o.plan_name)}</td><td>${cny(o.amount_cents)}</td><td>${paymentNames[o.provider]}</td><td>${esc(orderState(o))}${o.note?`<br><small>${esc(o.note)}</small>`:''}</td><td><button type="button" data-query-admin-order="${esc(o.id)}">核对</button>${o.state==='paid'?` <button type="button" class="danger" data-refund-order="${esc(o.id)}">全额退款</button>`:''}</td></tr>`).join(''):'<tr><td colspan="6" class="empty">暂无订单</td></tr>';
  $('billing-admin-orders').querySelectorAll('[data-query-admin-order]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{await api('/admin/billing/orders/'+encodeURIComponent(button.dataset.queryAdminOrder)+'/refresh','POST');await loadAdminBilling();}).catch(e=>toast(e.message))));
  $('billing-admin-orders').querySelectorAll('[data-refund-order]').forEach(button=>button.addEventListener('click',()=>busy(button,async()=>{
    const o=data.orders.find(o=>o.id===button.dataset.refundOrder);
    if(!await confirmation('确认全额退款？',`${o.username} 的订单 ${o.id}，退款金额 ${cny(o.amount_cents)}。提交后对应套餐权益暂停，真实退款会向原支付渠道发起，到账结果需核对。`))return;
    await ensureRecent();await api('/admin/billing/orders/'+encodeURIComponent(o.id)+'/refund','POST',{reason:'管理员审核确认全额退款'});await loadAdminBilling();toast('退款已发起，请查看订单结果');
  }).catch(e=>toast(e.message))));
  $('billing-admin-page').textContent=`第${data.page}/${data.pages}页 · 每页25笔`;$('billing-admin-prev').disabled=data.page<=1;$('billing-admin-next').disabled=data.page>=data.pages;
  $('billing-admin-sync').textContent='已更新 · '+date(Date.now()/1000)+' · 刷新不会覆盖未保存的设置';
}
form('billing-settings-form',async data=>{
  const enabled=data.has('enabled');if(enabled&&!billingState.admin?.settings.enabled&&!await confirmation('启用收费？','启用后，普通用户必须有有效套餐才能发起新的 AI 请求。尚未购买的现有用户也受此限制；管理员免付费限制。'))return;
  await ensureRecent();await api('/admin/billing/settings','PUT',{enabled,terms:data.get('terms'),support:data.get('support')});await loadAdminBilling(true);toast('收费设置已保存');
});
form('billing-plan-form',async data=>{
  const id=data.get('id'),body={name:data.get('name'),price_cents:Math.round(Number(data.get('price'))*100),days:Number(data.get('days')),daily_limit:Number(data.get('daily_limit')),enabled:data.has('enabled')};
  await ensureRecent();await api('/admin/billing/plans'+(id?'/'+encodeURIComponent(id):''),id?'PUT':'POST',body);newBillingPlan();await loadAdminBilling();toast('套餐已保存');
});
action('billing-new-plan',newBillingPlan);action('admin-billing-refresh',()=>loadAdminBilling());
action('billing-admin-prev',async()=>{billingState.page=Math.max(1,billingState.page-1);await loadAdminBilling();});
action('billing-admin-next',async()=>{billingState.page++;await loadAdminBilling();});
setInterval(async()=>{
 if(!state.me||document.hidden||billingState.busy)return;
 const watching=$('payment-dialog').open&&billingState.order&&['creating','pending','uncertain','refunding'].includes(billingState.order.state);
 if(!watching&&(state.view!=='billing'||Date.now()-billingState.lastRefresh<30000))return;
 billingState.busy=true;try{if(watching)await queryPayment();else await loadBilling();}catch(e){if(e.status!==401&&state.view==='billing')$('billing-status').textContent='更新暂时失败，保留上次结果，请稍后刷新。';}finally{billingState.busy=false;}
},4000);

action('payment-cancel-order',async()=>{const order=billingState.order;if(!order)return;if(!await confirmation('关闭未支付订单？','会先向支付宝核对付款状态；已付款订单保留，未付款订单确认关闭后不能继续付款。'))return;const updated=await api('/billing/orders/'+encodeURIComponent(order.id)+'/close','POST');if(billingState.order?.id===order.id){billingState.order=updated;renderPaymentOrder(updated);}await loadBilling();});
