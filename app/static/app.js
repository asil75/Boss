const tg = window.Telegram?.WebApp;
let appConfig = null;
tg?.ready();
tg?.expand();

const state = {
  user: null,
  orders: [],
  stats: null,
  payments: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (tg?.initData && !headers['x-telegram-init-data']) {
    headers['x-telegram-init-data'] = tg.initData;
  }
  if (options.body && !(options.body instanceof FormData)) {
    headers['content-type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function init() {
  try {
    if (!appConfig) appConfig = await api('/api/config'); Object.assign(window, appConfig);
    state.user = await api('/api/me');
    $('title').textContent = `Салом, ${state.user.first_name || state.user.username || state.user.tg_id}`;
    $('roleBox').classList.toggle('hidden', !!state.user.role);
    $('phoneBox').classList.toggle('hidden', !!state.user.phone);
    $('tabs').classList.toggle('hidden', !state.user.role);
    $('orderForm').classList.toggle('hidden', state.user.role !== 'shop');
    $('payAll').classList.toggle('hidden', state.user.role !== 'shop');
    $('admin').classList.toggle('hidden', state.user.role !== 'owner');
    await refresh();
  } catch (err) {
    document.body.innerHTML = `<main class="app"><div class="card"><h1>Ишга тушириш хатоси</h1><p>${escapeHtml(err.message)}</p><p class="meta">Telegram Mini App орқали очинг ёки BOTIM_DEV_MODE=true да X-User-Id header ишлатинг.</p></div></main>`;
  }
}

async function refresh() {
  if (!state.user) return;
  const filter = $('orderFilter').value;
  state.orders = await api(`/api/orders?status=${filter && !filter.startsWith('__') ? filter : ''}`);
  renderOrders();
  state.stats = await api('/api/stats');
  $('statsBox').textContent = JSON.stringify(state.stats, null, 2);
  if (state.user.role) {
    state.payments = await api('/api/payments/summary');
    $('paymentSummary').textContent = JSON.stringify(state.payments, null, 2);
  }
  if (state.user.role === 'owner') {
    const users = await api('/api/users');
    renderUsers(users);
  }
}

function renderOrders() {
  const list = $('ordersList');
  list.innerHTML = '';
  let orders = state.orders;
  const filter = $('orderFilter').value;
  if (filter === '__unpaid') orders = orders.filter(o => o.paid_to_courier < 2);
  if (filter === '__completed') orders = orders.filter(o => ['delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return'].includes(o.status));
  if (!orders.length) {
    list.innerHTML = '<div class="card meta">Заказлар топилмади.</div>';
    return;
  }
  for (const order of orders) {
    const card = document.createElement('div');
    card.className = 'order-card card';
    card.innerHTML = `
      <strong>#${order.id} — ${order.status}</strong>
      <div class="meta">
        ${escapeHtml(order.from_address || '')} → ${escapeHtml(order.to_address || '')}<br>
        Клиент: ${escapeHtml(order.client_name || '')} ${escapeHtml(order.client_phone || '')}<br>
        Нарх: ${order.price || 0} ₽ · Тўлов: ${paymentText(order.paid_to_courier)}
      </div>
      <div class="order-actions">${orderButtons(order)}</div>
    `;
    list.appendChild(card);
  }
}

function orderButtons(order) {
  const buttons = [];
  if (state.user.role === 'courier' && order.status === 'new') {
    buttons.push(btn('Олиш', 'take', order.id));
  }
  if (state.user.role === 'courier' && order.courier_tg_id === state.user.tg_id) {
    if (order.status === 'taken') buttons.push(btn('Дўконга етдим', 'pickup_shop', order.id));
    if (order.status === 'at_shop') buttons.push(btn('Йўлга чиқдим', 'on_delivery', order.id));
    if (order.status === 'on_delivery') buttons.push(btn('Клиентга етдим', 'arrive_client', order.id));
    if (order.status === 'at_client') {
      buttons.push(btn('Тасдиқлаш', 'finish', order.id, 'ok'));
      buttons.push(btn('Клиент йўқ', 'client_not_home', order.id));
    }
    if (['taken', 'at_shop', 'on_delivery', 'at_client'].includes(order.status)) {
      buttons.push(btn('Бекор қилиш', 'cancel', order.id, 'danger'));
    }
  }
  if (state.user.role === 'shop' && order.shop_tg_id === state.user.tg_id) {
    if (order.status === 'taken') buttons.push(btn('Бекор қилиш', 'cancel', order.id, 'danger'));
    if (['delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return'].includes(order.status) && order.paid_to_courier < 1) {
      buttons.push(btn('Тўланган деб белгилаш', `mark-paid:${order.id}`, order.id));
    }
    if (order.paid_to_courier === 1) {
      buttons.push(btn('Тасдиқ кутиляпти', '', order.id));
    }
  }
  return buttons.map(b => `<button class="${b.className}" data-action="${b.action}" data-id="${b.id}">${b.label}</button>`).join('');
}

function btn(label, action, id, className = '') {
  return { label, action, id, className };
}

function renderUsers(users) {
  const list = $('usersList');
  list.innerHTML = users.map(u => `
    <div class="user-card card">
      <strong>@${escapeHtml(u.username || '')} / ${u.tg_id}</strong>
      <div class="meta">${u.role || 'роль йўқ'} · ${u.phone || 'телефон йўқ'} · ${u.is_blocked ? 'блок' : 'актив'}</div>
      <div class="order-actions">
        <button data-user-role="${u.tg_id}:shop">Магазин</button>
        <button data-user-role="${u.tg_id}:courier">Курьер</button>
        <button class="danger" data-user-block="${u.tg_id}:${u.is_blocked ? 0 : 1}">${u.is_blocked ? 'Разблок' : 'Блок'}</button>
      </div>
    </div>
  `).join('');
}

function paymentText(value) {
  return value === 2 ? 'тасдиқланган' : value === 1 ? 'кутяпти' : 'тўланмаган';
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]));
}

document.addEventListener('click', async (e) => {
  const target = e.target.closest('button');
  if (!target) return;
  try {
    if (target.dataset.role) {
      await api('/api/users/me/role', { method: 'POST', body: { role: target.dataset.role } });
      await init();
    } else if (target.dataset.action) {
      const [action, id] = target.dataset.action.split(':');
      if (action === 'take') await api(`/api/orders/${id}/take`, { method: 'PATCH' });
      else if (action === 'mark-paid') await api(`/api/payments/${id}/mark-paid`, { method: 'POST' });
      else await api(`/api/orders/${target.dataset.id}/status`, { method: 'PATCH', body: { action } });
      await refresh();
    } else if (target.dataset.userRole) {
      const [tgId, role] = target.dataset.userRole.split(':');
      await api(`/api/users/${tgId}/role`, { method: 'POST', body: { role } });
      await refresh();
    } else if (target.dataset.userBlock) {
      const [tgId, blocked] = target.dataset.userBlock.split(':');
      await api(`/api/users/${tgId}/block`, { method: 'POST', body: { is_blocked: blocked === '1' } });
      await refresh();
    } else if (target.id === 'payAll') {
      await api('/api/payments/pay-all', { method: 'POST' });
      await refresh();
    }
  } catch (err) {
    tg?.showAlert(err.message);
  }
});

document.addEventListener('submit', async (e) => {
  if (e.target.id !== 'orderForm') return;
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target).entries());
  body.price = Number(body.price);
  await api('/api/orders', { method: 'POST', body });
  e.target.reset();
  await refresh();
});

$('orderFilter').addEventListener('change', refresh);
$('refreshOrders').addEventListener('click', refresh);
$('savePhone').addEventListener('click', async () => {
  await api(`/api/users/me/phone?phone=${encodeURIComponent($('phoneInput').value)}`, { method: 'POST' });
  await init();
});
$('themeBtn').addEventListener('click', () => document.body.classList.toggle('dark'));

init();
