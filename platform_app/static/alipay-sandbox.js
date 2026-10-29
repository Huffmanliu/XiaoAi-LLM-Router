'use strict';
let sandboxOrder = sessionStorage.getItem('alipay-sandbox-order');
const byId = id => document.getElementById(id);
function renderSandbox(order) {
  sandboxOrder = order.id; sessionStorage.setItem('alipay-sandbox-order', order.id);
  const label = {pending:'待支付',uncertain:'结果未知，请核对原订单，勿重复付款',paid:'已核实付款成功',closed:'已关闭',refunding:'退款待核对',refunded:'已确认全额退款',refund_review:'退款待核对'};
  byId('sandbox-state').textContent = order.id + ' · ' + (label[order.state] || '结果待核对');
  byId('sandbox-pay').hidden = order.state !== 'pending'; byId('sandbox-pay').href = '/pay/' + encodeURIComponent(order.id);
  byId('sandbox-query').hidden = false; byId('sandbox-close').hidden = order.state !== 'pending'; byId('sandbox-refund').hidden = order.state !== 'paid';
}
async function sandboxCall(path) {
  for (const button of document.querySelectorAll('button')) button.disabled = true;
  try {
    const response = await fetch(path, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:'{}'});
    const data = await response.json(); if (!response.ok) throw new Error(data.error || '操作暂未完成，请核对原订单');
    renderSandbox(data);
  } catch (error) { byId('sandbox-state').textContent = error.message; }
  finally { for (const button of document.querySelectorAll('button')) button.disabled = false; }
}
byId('sandbox-create').onclick = () => sandboxCall('/api/orders');
byId('sandbox-query').onclick = () => sandboxOrder && sandboxCall('/api/orders/' + encodeURIComponent(sandboxOrder) + '/query');
byId('sandbox-close').onclick = () => sandboxOrder && confirm('关闭这笔未支付沙箱订单？') && sandboxCall('/api/orders/' + encodeURIComponent(sandboxOrder) + '/close');
byId('sandbox-refund').onclick = () => sandboxOrder && confirm('确认退还本笔全部沙箱资金？不会涉及真实资金。') && sandboxCall('/api/orders/' + encodeURIComponent(sandboxOrder) + '/refund');
if (sandboxOrder) { byId('sandbox-query').hidden = false; byId('sandbox-state').textContent = sandboxOrder + ' · 点击查询核对原订单'; }
