'use strict';
(async () => {
  const result = document.getElementById('payment-result');
  if (!location.search) return;
  result.textContent = '正在确认支付结果，请稍候……';
  try {
    const response = await fetch('/api/billing/alipay-return' + location.search, {credentials: 'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '暂时无法核对，请返回订单列表查询。');
    result.textContent = ({paid: '已核实付款成功，请返回订单页面查看详情。', pending: '订单仍待支付，请稍后在订单列表核对。',
      closed: '订单已关闭。', refunded: '订单已确认全额退款。', refunding: '退款正在核对中。'})[data.state]
      || '支付结果尚未确认，请返回订单列表继续核对，勿重复付款。';
  } catch (error) { result.textContent = error.message; }
  // Avoid retaining signed payment parameters in the browser history.
  history.replaceState(null, '', location.pathname);
})();
