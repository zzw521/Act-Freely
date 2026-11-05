'use strict';

const TYPES = [
  { id: 'skill', plural: 'skills', label: '技能' },
  { id: 'suite', plural: 'suites', label: '专家套件' },
  { id: 'mcp', plural: 'mcp', label: 'MCP 插件' },
];
const ROLE_LABELS = {
  admin: '平台管理员',
  operator: '网关运维',
  auditor: '审计员',
  user: '普通用户',
};
const STAFF_ROLES = new Set(['admin', 'operator', 'auditor']);
const ADMIN_TABS = ['items', 'users', 'gateway', 'audit', 'departments'];
const ADMIN_TAB_LABELS = {
  items: '市场条目',
  users: '用户',
  gateway: '模型网关',
  audit: '审计',
  departments: '组织与部门',
};
const STAFF_TABS = ['gateway', 'audit'];
const PAGE_SIZE = 12;
const ADMIN_PAGE_SIZE = 10;
const AUDIT_PAGE_SIZE = 100;
const TOKEN_KEY = 'tt.web.session';

const $ = (id) => document.getElementById(id);

const state = {
  tokens: loadSession(),
  profile: null,
  view: 'market',
  type: 'skill',
  q: '',
  category: '',
  sort: 'recommended',
  page: 1,
  categories: {},
  adminTab: 'items',
  adminType: 'skill',
  adminQ: '',
  adminPage: 1,
  userQ: '',
  userPage: 1,
  openItem: null,
  departments: [],
  deptQ: '',
  deptTarget: '',
  auditSource: 'gateway',
  auditAfter: 0,
  auditEntries: [],
};

let marketRequest = 0;
let adminItemsRequest = 0;
let adminUsersRequest = 0;
let refreshInFlight = null;
let authMode = 'login';

// ── 会话与请求 ──────────────────────────────────────────────

function loadSession() {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSession(data) {
  state.tokens = { accessToken: data.accessToken, refreshToken: data.refreshToken };
  sessionStorage.setItem(TOKEN_KEY, JSON.stringify(state.tokens));
  if (data.profile) state.profile = data.profile;
}

function clearSession() {
  state.tokens = null;
  state.profile = null;
  sessionStorage.removeItem(TOKEN_KEY);
}

function refreshSession() {
  if (!state.tokens || !state.tokens.refreshToken) return Promise.resolve(false);
  if (refreshInFlight) return refreshInFlight;
  const refreshToken = state.tokens.refreshToken;
  refreshInFlight = fetch('/api/v1/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ refreshToken }),
  })
    .then(async (response) => {
      if (!response.ok) return false;
      const data = await response.json();
      if (!data.ok) return false;
      saveSession(data);
      renderAccount();
      return true;
    })
    .catch(() => false)
    .finally(() => {
      refreshInFlight = null;
    });
  return refreshInFlight;
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json' };
  let body = options.body;
  if (body !== undefined && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  if (state.tokens && state.tokens.accessToken) {
    headers.Authorization = 'Bearer ' + state.tokens.accessToken;
  }
  const response = await fetch(path, { method: options.method || 'GET', headers, body });
  if (response.status === 401 && state.tokens && state.tokens.refreshToken && !options._retried) {
    if (await refreshSession()) return api(path, { ...options, _retried: true });
    clearSession();
    renderAccount();
  }
  const contentType = response.headers.get('content-type') || '';
  const data = contentType.includes('application/json')
    ? await response.json()
    : await response.text();
  if (
    !response.ok ||
    (!options.allowOkFalse && data && typeof data === 'object' && data.ok === false)
  ) {
    const error = new Error((data && data.message) || '请求失败（' + response.status + '）');
    error.status = response.status;
    error.code = data && data.code;
    throw error;
  }
  return data;
}

// ── 小工具 ──────────────────────────────────────────────────

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function pluralOf(type) {
  const spec = TYPES.find((item) => item.id === type);
  return spec ? spec.plural : type;
}

function labelOf(type) {
  const spec = TYPES.find((item) => item.id === type);
  return spec ? spec.label : '条目';
}

function roleOf() {
  return (state.profile && state.profile.role) || 'user';
}

function roleLabel(role) {
  return ROLE_LABELS[role] || role || ROLE_LABELS.user;
}

function isAdmin() {
  return roleOf() === 'admin';
}

function isStaff() {
  return STAFF_ROLES.has(roleOf());
}

function canWriteGateway() {
  return roleOf() === 'admin' || roleOf() === 'operator';
}

function allowedAdminTabs() {
  return isAdmin() ? ADMIN_TABS : STAFF_TABS;
}

function applyRoleVisibility() {
  const writable = canWriteGateway();
  document.querySelectorAll('[data-gateway-write]').forEach((node) => {
    node.hidden = !writable;
  });
  const hint = $('gateway-readonly-hint');
  if (hint) hint.hidden = writable;
}

function formatDate(ms) {
  if (!ms) return '';
  return new Date(ms).toLocaleDateString('zh-CN');
}

function formatInt(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

function formatMicros(value, currency) {
  const amount = (Number(value || 0) / 1_000_000).toFixed(6);
  return amount + ' ' + (currency || '');
}

function formatBytes(size) {
  if (!size) return '';
  if (size < 1024) return size + ' B';
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
  return (size / 1024 / 1024).toFixed(1) + ' MB';
}

function debounce(fn, wait) {
  let timer = null;
  return function debounced() {
    clearTimeout(timer);
    timer = setTimeout(fn, wait);
  };
}

function toast(message, kind) {
  const node = el('div', 'toast' + (kind === 'error' ? ' error' : ''), message);
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), 3200);
}

function downloadLink(url, text, className) {
  if (typeof url !== 'string' || !url.startsWith('/api/v1/')) return null;
  const link = el('a', className || 'btn', text);
  link.href = url;
  link.setAttribute('download', '');
  return link;
}

// ── 账号 ────────────────────────────────────────────────────

function renderAccount() {
  const box = $('account');
  box.replaceChildren();
  const adminNav = document.querySelector('.nav-link[data-view="admin"]');
  if (adminNav) adminNav.hidden = !isStaff();

  if (!state.profile) {
    const login = el('button', 'btn primary block', '登录 / 注册');
    login.type = 'button';
    login.addEventListener('click', () => openAuth('login'));
    box.appendChild(login);
    return;
  }

  const displayName = state.profile.nickname || state.profile.email || '';
  const info = el('div', 'account-info');
  info.appendChild(el('span', 'account-avatar', (displayName.trim().slice(0, 1) || 'U').toUpperCase()));
  const meta = el('div', 'account-meta');
  meta.appendChild(el('span', 'account-name', displayName));
  if (isStaff()) meta.appendChild(el('span', 'account-role', roleLabel(roleOf())));
  info.appendChild(meta);
  box.appendChild(info);

  const logoutBtn = el('button', 'account-logout', '退出');
  logoutBtn.type = 'button';
  logoutBtn.addEventListener('click', logout);
  box.appendChild(logoutBtn);
}

function openAuth(mode) {
  setAuthMode(mode || 'login');
  $('auth-error').hidden = true;
  $('auth-modal').hidden = false;
  $('auth-overlay').hidden = false;
  $('auth-email').focus();
}

function closeAuth() {
  $('auth-modal').hidden = true;
  $('auth-overlay').hidden = true;
  $('auth-error').hidden = true;
}

function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll('[data-auth-mode]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.authMode === mode);
  });
  const registering = mode === 'register';
  $('auth-title').textContent = registering ? '注册账号' : '登录';
  $('auth-submit').textContent = registering ? '注册并登录' : '登录';
  $('auth-nickname-field').hidden = !registering;
  $('auth-password').autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-error').hidden = true;
}

function showAuthError(message) {
  const box = $('auth-error');
  box.textContent = message;
  box.hidden = false;
}

async function submitAuth(event) {
  event.preventDefault();
  const email = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const nickname = $('auth-nickname').value.trim();
  if (!email || password.length < 8) {
    showAuthError('请填写邮箱，密码至少 8 位');
    return;
  }
  const submit = $('auth-submit');
  const original = submit.textContent;
  submit.disabled = true;
  submit.textContent = '处理中…';
  try {
    const registering = authMode === 'register';
    const payload = registering
      ? { email, password, ...(nickname ? { nickname } : {}) }
      : { email, password };
    const data = await api(registering ? '/api/v1/auth/register' : '/api/v1/auth/login', {
      method: 'POST',
      body: payload,
    });
    saveSession(data);
    renderAccount();
    closeAuth();
    toast(registering ? '注册成功，已登录' : '已登录');
    route();
  } catch (error) {
    showAuthError(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = original;
  }
}

async function logout() {
  const refreshToken = state.tokens && state.tokens.refreshToken;
  clearSession();
  renderAccount();
  if (refreshToken) {
    try {
      await api('/api/v1/auth/logout', { method: 'POST', body: { refreshToken } });
    } catch {
      /* 退出失败不阻塞本地登出 */
    }
  }
  if (location.hash === '#/admin') location.hash = '#/market';
  route();
  toast('已退出登录');
}

// ── 路由 ────────────────────────────────────────────────────

function route() {
  const view = location.hash.replace(/^#\/?/, '') === 'admin' ? 'admin' : 'market';
  if (view === 'admin' && !isStaff()) {
    location.hash = '#/market';
    return;
  }
  state.view = view;
  document.querySelectorAll('.nav-link').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  $('view-market').hidden = view !== 'market';
  $('view-admin').hidden = view !== 'admin';
  const title = view === 'admin' ? '管理控制台' : '技能市场';
  const crumb = $('crumb-current');
  if (crumb) crumb.textContent = title;
  document.title = title + ' · 通天办公';
  if (view === 'admin') loadAdmin();
  else loadMarket();
}

// ── 市场 ────────────────────────────────────────────────────

async function loadMarket() {
  renderMarketTypes();
  const seq = ++marketRequest;
  const grid = $('market-grid');
  grid.replaceChildren(el('p', 'loading', '加载中…'));
  $('market-count').textContent = '';

  try {
    const categories = await ensureCategories(state.type);
    if (seq !== marketRequest) return;
    renderCategoryChips(categories);
    const params = new URLSearchParams({
      q: state.q,
      category: state.category,
      sort: state.sort,
      page: String(state.page),
      pageSize: String(PAGE_SIZE),
    });
    const data = await api('/api/v1/' + pluralOf(state.type) + '?' + params.toString());
    if (seq !== marketRequest) return;
    renderCards(grid, data.items || []);
    $('market-count').textContent = '共 ' + data.total + ' 个' + labelOf(state.type);
    renderPager($('market-pager'), data, (page) => {
      state.page = page;
      loadMarket();
    });
  } catch (error) {
    if (seq !== marketRequest) return;
    grid.replaceChildren(el('p', 'empty', error.message));
    $('market-pager').replaceChildren();
  }
}

function renderMarketTypes() {
  const box = $('market-types');
  box.replaceChildren();
  TYPES.forEach((spec) => {
    const active = state.type === spec.id;
    const btn = el('button', 'type-tab' + (active ? ' active' : ''), spec.label);
    btn.type = 'button';
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', String(active));
    btn.addEventListener('click', () => {
      if (state.type === spec.id) return;
      state.type = spec.id;
      state.category = '';
      state.page = 1;
      loadMarket();
    });
    box.appendChild(btn);
  });
}

async function ensureCategories(type) {
  if (state.categories[type]) return state.categories[type];
  const data = await api('/api/v1/categories?type=' + type);
  state.categories[type] = data.categories || [];
  return state.categories[type];
}

function renderCategoryChips(categories) {
  const box = $('market-categories');
  box.replaceChildren();
  const total = categories.reduce((sum, item) => sum + (item.count || 0), 0);
  const addChip = (label, value, count) => {
    const active = state.category === value;
    const chip = el('button', 'chip' + (active ? ' active' : ''), label);
    chip.type = 'button';
    if (count || count === 0) {
      chip.appendChild(el('span', 'chip-count', String(count)));
    }
    chip.addEventListener('click', () => {
      state.category = value;
      state.page = 1;
      loadMarket();
    });
    box.appendChild(chip);
  };
  addChip('全部', '', total);
  categories.forEach((item) => addChip(item.name, item.name, item.count));
}

function renderCards(grid, items) {
  grid.replaceChildren();
  if (!items.length) {
    grid.appendChild(el('p', 'empty', '没有找到匹配的条目'));
    return;
  }
  items.forEach((item) => grid.appendChild(buildCard(item)));
}

function buildCard(item) {
  const card = el('article', 'card');
  card.tabIndex = 0;
  card.setAttribute('role', 'button');

  const head = el('div', 'card-head');
  head.appendChild(el('div', 'card-icon', item.icon || '📦'));
  const nameBox = el('div');
  const title = el('div', 'card-title', item.name);
  if (item.featured) title.appendChild(el('span', 'badge featured', '推荐'));
  if (item.revoked) title.appendChild(el('span', 'badge revoked', '已下架'));
  nameBox.appendChild(title);
  nameBox.appendChild(el('div', 'card-meta', item.author ? '发布者 ' + item.author.nickname : ''));
  head.appendChild(nameBox);
  card.appendChild(head);

  card.appendChild(el('p', 'card-summary', item.summary || '暂无简介'));

  const tags = el('div', 'card-tags');
  (item.tags || []).slice(0, 3).forEach((tag) => tags.appendChild(el('span', 'tag', tag)));
  if (tags.childNodes.length) card.appendChild(tags);

  const meta = el('div', 'card-meta');
  [
    item.category,
    item.latestVersion ? 'v' + item.latestVersion : '无可用版本',
    '⬇ ' + (item.downloads || 0),
    '★ ' + (item.stars || 0),
  ].forEach((text) => meta.appendChild(el('span', null, text)));
  card.appendChild(meta);

  const open = () => openDetail(item.type || state.type, item.id);
  card.addEventListener('click', open);
  card.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  });
  return card;
}

function renderPager(node, data, onChange) {
  node.replaceChildren();
  const pages = Math.max(1, Math.ceil(data.total / data.pageSize));
  const prev = el('button', 'btn small', '上一页');
  prev.type = 'button';
  prev.disabled = data.page <= 1;
  prev.addEventListener('click', () => onChange(data.page - 1));
  const next = el('button', 'btn small', '下一页');
  next.type = 'button';
  next.disabled = !data.hasMore;
  next.addEventListener('click', () => onChange(data.page + 1));
  node.append(prev, el('span', 'pager-info', '第 ' + data.page + ' / ' + pages + ' 页'), next);
}

// ── 详情抽屉 ────────────────────────────────────────────────

async function openDetail(type, id) {
  state.openItem = { type, id };
  $('drawer').hidden = false;
  $('drawer-overlay').hidden = false;
  const body = $('drawer-body');
  body.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api('/api/v1/' + pluralOf(type) + '/' + encodeURIComponent(id));
    renderDetail(data.item, body);
  } catch (error) {
    body.replaceChildren(el('p', 'empty', error.message));
  }
}

function closeDrawer() {
  $('drawer').hidden = true;
  $('drawer-overlay').hidden = true;
  state.openItem = null;
}

function renderDetail(item, container) {
  container.replaceChildren();

  const head = el('div', 'detail-head');
  head.appendChild(el('div', 'detail-icon', item.icon || '📦'));
  const info = el('div');
  const title = el('h2', 'detail-title', item.name);
  if (item.featured) title.appendChild(el('span', 'badge featured', '推荐'));
  if (item.revoked) title.appendChild(el('span', 'badge revoked', '已下架'));
  info.appendChild(title);
  const stats = el('div', 'detail-sub');
  const parts = [
    labelOf(item.type),
    item.category,
    '发布者 ' + (item.author ? item.author.nickname : '未知'),
  ];
  if (item.latestVersion) parts.push('v' + item.latestVersion);
  stats.appendChild(el('span', null, parts.filter(Boolean).join(' · ')));
  stats.appendChild(el('span', 'mono', '  ⬇ ' + (item.downloads || 0) + '  '));
  const starStat = el('span', null, '★ ' + (item.stars || 0));
  starStat.dataset.stat = 'stars';
  stats.appendChild(starStat);
  info.appendChild(stats);
  head.appendChild(info);
  container.appendChild(head);

  const actions = el('div', 'detail-actions');
  const starBtn = el('button', 'btn', item.starred ? '★ 已收藏' : '☆ 收藏');
  starBtn.type = 'button';
  starBtn.addEventListener('click', async () => {
    if (!state.profile) {
      openAuth('login');
      return;
    }
    try {
      const data = await api(
        '/api/v1/' + pluralOf(item.type) + '/' + encodeURIComponent(item.id) + '/star',
        { method: 'POST' }
      );
      item.starred = data.starred;
      item.stars = data.stars;
      starBtn.textContent = data.starred ? '★ 已收藏' : '☆ 收藏';
      starStat.textContent = '★ ' + data.stars;
      toast(data.starred ? '已加入收藏' : '已取消收藏');
    } catch (error) {
      if (error.status === 401) openAuth('login');
      else toast(error.message, 'error');
    }
  });
  actions.appendChild(starBtn);

  if (item.type !== 'mcp' && item.downloadUrl) {
    const link = downloadLink(item.downloadUrl, '下载最新版', 'btn primary');
    if (link) actions.appendChild(link);
  }
  if (item.type === 'mcp') {
    const configBtn = el('button', 'btn', '查看配置');
    configBtn.type = 'button';
    configBtn.addEventListener('click', async () => {
      if (container.querySelector('.mcp-config')) return;
      try {
        const data = await api('/api/v1/mcp/' + encodeURIComponent(item.id) + '/config');
        renderMcpConfig(container, data);
      } catch (error) {
        toast(error.message, 'error');
      }
    });
    actions.appendChild(configBtn);
  }
  container.appendChild(actions);

  if (item.description) {
    const section = el('section', 'section');
    section.appendChild(el('h3', null, '简介'));
    section.appendChild(el('p', 'prose', item.description));
    container.appendChild(section);
  }

  if (item.readme) {
    const section = el('section', 'section');
    section.appendChild(el('h3', null, '说明'));
    const pre = el('pre', 'readme prose', item.readme);
    section.appendChild(pre);
    container.appendChild(section);
  }

  const tags = item.tags || [];
  if (tags.length) {
    const section = el('section', 'section');
    section.appendChild(el('h3', null, '标签'));
    const box = el('div', 'card-tags');
    tags.forEach((tag) => box.appendChild(el('span', 'tag', tag)));
    section.appendChild(box);
    container.appendChild(section);
  }

  const versions = item.versions || [];
  const versionSection = el('section', 'section');
  versionSection.appendChild(el('h3', null, '版本（' + versions.length + '）'));
  const list = el('div', 'version-list');
  if (!versions.length) {
    list.appendChild(el('p', 'empty', '还没有发布版本'));
  }
  versions.forEach((version) => {
    list.appendChild(buildVersionRow(item, version));
  });
  versionSection.appendChild(list);
  container.appendChild(versionSection);

  if (item.install) {
    const section = el('section', 'section');
    section.appendChild(el('h3', null, '安装说明'));
    const install = item.install;
    const text =
      install.kind === 'mcp'
        ? '该插件以配置形式分发，调用 ' + install.configEndpoint + ' 获取配置。'
        : '解压后安装到 ' + install.target + '（入口 ' + install.entry + '）。';
    section.appendChild(el('p', 'prose', text));
    container.appendChild(section);
  }
}

function buildVersionRow(item, version) {
  const row = el('div', 'version');
  const main = el('div', 'version-main');
  main.appendChild(el('span', 'version-no', 'v' + version.version));
  if (version.revoked) main.appendChild(el('span', 'badge revoked', '已撤销'));
  const meta = [formatDate(version.publishedAtMs), formatBytes(version.sizeBytes), '⬇ ' + version.downloads]
    .filter(Boolean)
    .join(' · ');
  main.appendChild(el('span', 'version-meta', meta));
  row.appendChild(main);

  const actions = el('div', 'version-actions');
  if (version.downloadUrl && !version.revoked) {
    const link = downloadLink(version.downloadUrl, '下载', 'btn small');
    if (link) actions.appendChild(link);
  }
  if (isAdmin()) {
    const toggle = el('button', 'btn small' + (version.revoked ? '' : ' danger'), version.revoked ? '恢复' : '撤销');
    toggle.type = 'button';
    toggle.addEventListener('click', () => adminSetRevoked(item, version.version, !version.revoked));
    actions.appendChild(toggle);
  }
  row.appendChild(actions);

  if (version.revokedReason) {
    const reason = el('div', 'version-meta', '撤销原因：' + version.revokedReason);
    row.appendChild(reason);
  }
  return row;
}

function renderMcpConfig(container, data) {
  const section = el('section', 'section mcp-config');
  section.appendChild(el('h3', null, 'MCP 配置（v' + data.version + '）'));
  const pre = el('pre', 'readme prose', JSON.stringify(data.mcp, null, 2));
  section.appendChild(pre);
  const copy = el('button', 'btn small', '复制配置');
  copy.type = 'button';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(data.mcp, null, 2));
      toast('配置已复制');
    } catch {
      toast('复制失败，请手动选择文本', 'error');
    }
  });
  section.appendChild(copy);
  container.appendChild(section);
}

async function adminSetRevoked(item, version, revoked) {
  let body;
  if (revoked) {
    const reason = window.prompt('请输入撤销原因（会展示给所有用户）', '');
    if (reason === null) return;
    if (!reason.trim()) {
      toast('撤销原因不能为空', 'error');
      return;
    }
    body = { reason: reason.trim() };
  }
  const path =
    '/api/v1/admin/items/' +
    item.type +
    '/' +
    encodeURIComponent(item.id) +
    '/versions/' +
    encodeURIComponent(version) +
    (revoked ? '/revoke' : '/unrevoke');
  try {
    await api(path, { method: 'POST', body });
    toast(revoked ? '已撤销该版本' : '已恢复该版本');
    if (state.openItem) openDetail(state.openItem.type, state.openItem.id);
    if (state.view === 'admin') loadAdmin();
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── 管理控制台 ──────────────────────────────────────────────

function loadAdmin() {
  if (!isStaff()) return;
  const allowed = allowedAdminTabs();
  if (!allowed.includes(state.adminTab)) state.adminTab = allowed[0];
  const panels = {
    items: 'admin-items-panel',
    users: 'admin-users-panel',
    gateway: 'admin-gateway-panel',
    audit: 'admin-audit-panel',
    departments: 'admin-departments-panel',
  };
  Object.entries(panels).forEach(([key, id]) => {
    $(id).hidden = state.adminTab !== key;
  });
  document.querySelectorAll('[data-admin-tab]').forEach((btn) => {
    const visible = allowed.includes(btn.dataset.adminTab);
    btn.hidden = !visible;
    btn.classList.toggle('active', btn.dataset.adminTab === state.adminTab);
  });
  const tabLabel = ADMIN_TAB_LABELS[state.adminTab] || '管理控制台';
  const crumb = $('crumb-current');
  if (crumb) crumb.textContent = '管理控制台 / ' + tabLabel;
  document.title = tabLabel + ' · 管理控制台 · 通天办公';
  applyRoleVisibility();
  if (state.adminTab === 'items') loadAdminItems();
  else if (state.adminTab === 'users') loadAdminUsers();
  else if (state.adminTab === 'gateway') loadGatewayPanel();
  else if (state.adminTab === 'audit') initAuditPanel();
  else loadDepartmentsPanel();
}

function renderAdminTypes() {
  const box = $('admin-types');
  box.replaceChildren();
  TYPES.forEach((spec) => {
    const active = state.adminType === spec.id;
    const btn = el('button', 'type-tab' + (active ? ' active' : ''), spec.label);
    btn.type = 'button';
    btn.addEventListener('click', () => {
      if (state.adminType === spec.id) return;
      state.adminType = spec.id;
      state.adminPage = 1;
      loadAdminItems();
    });
    box.appendChild(btn);
  });
}

async function loadAdminItems() {
  renderAdminTypes();
  const seq = ++adminItemsRequest;
  const box = $('admin-items');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const params = new URLSearchParams({
      q: state.adminQ,
      sort: 'latest',
      page: String(state.adminPage),
      pageSize: String(ADMIN_PAGE_SIZE),
    });
    const data = await api('/api/v1/' + pluralOf(state.adminType) + '?' + params.toString());
    if (seq !== adminItemsRequest) return;
    $('admin-items-count').textContent = '共 ' + data.total + ' 个' + labelOf(state.adminType);
    if (!data.items.length) {
      box.replaceChildren(el('p', 'empty', '没有条目'));
    } else {
      const rows = data.items.map((item) => buildAdminItemRow(item));
      box.replaceChildren(buildTable(['条目', '分类', '最新版本', '推荐', '下载 / 收藏', '操作'], rows));
    }
    renderPager($('admin-items-pager'), data, (page) => {
      state.adminPage = page;
      loadAdminItems();
    });
  } catch (error) {
    if (seq !== adminItemsRequest) return;
    box.replaceChildren(el('p', 'empty', error.message));
    $('admin-items-pager').replaceChildren();
  }
}

function buildAdminItemRow(item) {
  const row = el('tr');

  const name = el('td', 'name-cell');
  name.appendChild(el('span', null, item.name));
  name.appendChild(el('span', 'sub mono', item.id));
  row.appendChild(name);

  row.appendChild(el('td', null, item.category || '—'));

  const versionCell = el('td');
  versionCell.appendChild(el('span', null, item.latestVersion ? 'v' + item.latestVersion : '—'));
  if (item.revoked) versionCell.appendChild(el('span', 'badge revoked', '已下架'));
  row.appendChild(versionCell);

  const featuredCell = el('td');
  featuredCell.appendChild(
    el('span', 'badge ' + (item.featured ? 'featured' : 'muted'), item.featured ? '推荐' : '普通')
  );
  row.appendChild(featuredCell);

  row.appendChild(el('td', null, (item.downloads || 0) + ' / ' + (item.stars || 0)));

  const actions = el('div', 'row-actions');
  const featureBtn = el('button', 'btn small', item.featured ? '取消推荐' : '设为推荐');
  featureBtn.type = 'button';
  featureBtn.addEventListener('click', async () => {
    try {
      await api('/api/v1/admin/items/' + item.type + '/' + encodeURIComponent(item.id) + '/feature', {
        method: 'POST',
        body: { featured: !item.featured },
      });
      toast(item.featured ? '已取消推荐' : '已设为推荐');
      loadAdminItems();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  actions.appendChild(featureBtn);

  const detailBtn = el('button', 'btn small', '版本管理');
  detailBtn.type = 'button';
  detailBtn.addEventListener('click', () => openDetail(item.type, item.id));
  actions.appendChild(detailBtn);

  const cell = el('td');
  cell.appendChild(actions);
  row.appendChild(cell);
  return row;
}

async function loadAdminUsers() {
  const seq = ++adminUsersRequest;
  const box = $('admin-users');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const params = new URLSearchParams({
      q: state.userQ,
      page: String(state.userPage),
      pageSize: String(ADMIN_PAGE_SIZE),
    });
    const data = await api('/api/v1/admin/users?' + params.toString());
    if (seq !== adminUsersRequest) return;
    $('admin-users-count').textContent = '共 ' + data.total + ' 个账号';
    if (!data.users.length) {
      box.replaceChildren(el('p', 'empty', '没有账号'));
    } else {
      const rows = data.users.map((user) => buildAdminUserRow(user));
      box.replaceChildren(
        buildTable(['昵称', '邮箱', '角色', '套餐', '组织', '注册时间', '操作'], rows)
      );
    }
    renderPager($('admin-users-pager'), data, (page) => {
      state.userPage = page;
      loadAdminUsers();
    });
  } catch (error) {
    if (seq !== adminUsersRequest) return;
    box.replaceChildren(el('p', 'empty', error.message));
    $('admin-users-pager').replaceChildren();
  }
}

function buildAdminUserRow(user) {
  const row = el('tr');
  row.appendChild(el('td', 'name-cell', user.nickname || '—'));
  row.appendChild(el('td', 'mono', user.email));
  const roleCell = el('td');
  const self = Boolean(state.profile && state.profile.uid === user.uid);
  const select = el('select', 'role-select');
  select.setAttribute('aria-label', '角色');
  Object.entries(ROLE_LABELS).forEach(([value, label]) => {
    const option = el('option', null, label);
    option.value = value;
    select.appendChild(option);
  });
  select.value = user.role || 'user';
  select.disabled = self;
  if (self) select.title = '不能修改自己的角色，请让其他管理员操作';
  select.addEventListener('change', () => changeUserRole(user, select));
  roleCell.appendChild(select);
  row.appendChild(roleCell);
  row.appendChild(el('td', null, user.plan || '—'));
  row.appendChild(el('td', 'mono', user.org || '—'));
  row.appendChild(el('td', null, formatDate(user.createdAtMs)));

  const actions = el('div', 'row-actions');
  const revoke = el('button', 'btn small danger', '强制下线');
  revoke.type = 'button';
  revoke.addEventListener('click', async () => {
    const self = state.profile && state.profile.uid === user.uid;
    const message = self
      ? '这会注销你当前账号的所有会话，包括本页面，确定继续？'
      : '确定注销 ' + user.email + ' 的全部会话？';
    if (!window.confirm(message)) return;
    try {
      const data = await api('/api/v1/admin/users/' + encodeURIComponent(user.uid) + '/revoke-sessions', {
        method: 'POST',
      });
      toast('已注销 ' + data.revoked + ' 个会话');
      if (self) {
        clearSession();
        renderAccount();
        location.hash = '#/market';
        route();
      }
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  actions.appendChild(revoke);
  const cell = el('td');
  cell.appendChild(actions);
  row.appendChild(cell);
  return row;
}

async function changeUserRole(user, select) {
  const role = select.value;
  if (!role || role === user.role) return;
  if (
    !window.confirm(
      '把 ' + user.email + ' 的角色改为「' + roleLabel(role) + '」？该账号的全部会话会被注销。'
    )
  ) {
    select.value = user.role || 'user';
    return;
  }
  select.disabled = true;
  try {
    const data = await api('/api/v1/admin/users/' + encodeURIComponent(user.uid), {
      method: 'PATCH',
      body: { role },
    });
    toast(
      '角色已更新为「' + roleLabel(role) + '」' +
        (data.sessionsRevoked ? '，已注销 ' + data.sessionsRevoked + ' 个会话' : '')
    );
    loadAdminUsers();
  } catch (error) {
    toast(error.message, 'error');
    select.value = user.role || 'user';
    select.disabled = false;
  }
}

function buildTable(headers, rows) {
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  headers.forEach((header) => headRow.appendChild(el('th', null, header)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  rows.forEach((row) => tbody.appendChild(row));
  table.appendChild(tbody);
  return table;
}

// ── 模型网关（M1.4 T4.11） ──────────────────────────────────

function gatewayPath(path) {
  return '/api/v1/admin/gateway' + path;
}

async function loadGatewayPanel() {
  applyRoleVisibility();
  const ready = await loadGatewayHealth();
  ['gateway-providers-card', 'gateway-apikey-card', 'gateway-quota-card', 'gateway-usage-card'].forEach(
    (id) => {
      const card = $(id);
      if (card) card.hidden = !ready;
    }
  );
  if (!ready) return;
  loadGatewayProviders();
  loadGatewayApiKeys();
  loadGatewayQuotas();
  loadGatewayUsage();
}

function gatewayUnavailable(error) {
  return /TT_GATEWAY_ADMIN_URL|TT_GATEWAY_ADMIN_TOKEN|网关管理地址|网关管理令牌/.test(
    String((error && error.message) || '')
  );
}

function showPanelError(box, error) {
  box.replaceChildren(
    el('p', 'empty', gatewayUnavailable(error) ? '模型网关未连接' : error.message)
  );
}

// ── 审计（M4.3 哈希链） ─────────────────────────────────────

function auditBase(path) {
  const prefix = state.auditSource === 'gateway' ? gatewayPath('/audit') : '/api/v1/admin/audit';
  return prefix + path;
}

function initAuditPanel() {
  $('audit-source').value = state.auditSource;
  $('audit-verify-result').replaceChildren();
  state.auditAfter = 0;
  state.auditEntries = [];
  loadAuditPage(false);
}

async function loadAuditPage(append) {
  const box = $('audit-entries');
  if (!append) {
    box.replaceChildren(el('p', 'loading', '加载中…'));
    state.auditAfter = 0;
    state.auditEntries = [];
  }
  try {
    const data = await api(
      auditBase('') + '?after=' + state.auditAfter + '&limit=' + AUDIT_PAGE_SIZE
    );
    const entries = data.entries || [];
    state.auditEntries = append ? state.auditEntries.concat(entries) : entries;
    state.auditAfter = Number(data.nextAfter ?? state.auditAfter);
    renderAuditEntries();
    $('audit-more').hidden = entries.length < AUDIT_PAGE_SIZE;
  } catch (error) {
    showPanelError(box, error);
  }
}

function renderAuditEntries() {
  const box = $('audit-entries');
  if (!state.auditEntries.length) {
    box.replaceChildren(el('p', 'empty', '暂无审计记录'));
    return;
  }
  const rows = state.auditEntries.map((entry) => {
    const row = el('tr');
    row.appendChild(el('td', 'mono', new Date(entry.atMs).toLocaleString('zh-CN')));

    const actor = el('td');
    actor.appendChild(el('span', null, entry.actor || '—'));
    actor.appendChild(el('span', 'sub', ' · ' + roleLabel(entry.actorRole)));
    row.appendChild(actor);

    row.appendChild(el('td', 'mono', entry.action || '—'));

    const object = el('td', 'mono', entry.object || '—');
    object.title = entry.object || '';
    row.appendChild(object);

    const status = el('td');
    status.appendChild(
      el(
        'span',
        'badge ' + (Number(entry.status) < 400 ? 'ok' : 'muted'),
        String(entry.status ?? '—')
      )
    );
    row.appendChild(status);

    const change = el('td');
    const hasBefore = entry.before && Object.keys(entry.before).length;
    const hasAfter = entry.after && Object.keys(entry.after).length;
    if (hasBefore || hasAfter) {
      const details = el('details');
      details.appendChild(el('summary', null, '查看'));
      details.appendChild(
        el(
          'pre',
          'mono',
          JSON.stringify({ before: entry.before || {}, after: entry.after || {} }, null, 2)
        )
      );
      change.appendChild(details);
    } else {
      change.appendChild(el('span', 'muted', '—'));
    }
    row.appendChild(change);

    const hash = el('td', 'mono', String(entry.entryHash || '').slice(0, 12) || '—');
    hash.title = entry.entryHash || '';
    row.appendChild(hash);
    return row;
  });
  box.replaceChildren(
    buildTable(['时间', '主体', '动作', '对象', '状态', '变更', '哈希'], rows)
  );
}

async function verifyAudit() {
  const line = $('audit-verify-result');
  line.textContent = '校验中…';
  try {
    const data = await api(auditBase('/verify'), { allowOkFalse: true });
    const ok = Boolean(data.ok);
    const badge = el(
      'span',
      'badge ' + (ok ? 'ok' : 'revoked'),
      ok ? '哈希链完整' : '哈希链断裂'
    );
    const detail = ok
      ? ' · ' +
        formatInt(data.entries) +
        ' 条 · head ' +
        String(data.headHash || '').slice(0, 12)
      : ' · 首个异常 #' +
        data.brokenAt +
        '（此前 ' +
        formatInt(data.entries) +
        ' 条已核验）';
    line.replaceChildren(badge, el('span', null, detail));
  } catch (error) {
    line.textContent = '校验失败：' + error.message;
  }
}

async function exportAudit() {
  try {
    const headers = { Accept: 'application/x-ndjson' };
    if (state.tokens && state.tokens.accessToken) {
      headers.Authorization = 'Bearer ' + state.tokens.accessToken;
    }
    const response = await fetch(auditBase('/export') + '?after=0&limit=1000', {
      headers,
    });
    if (!response.ok) throw new Error('导出失败（' + response.status + '）');
    const blob = await response.blob();
    const next = Number(response.headers.get('X-TT-Audit-Next') || 0);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'audit-' + state.auditSource + '-' + Date.now() + '.ndjson';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(next > 0 ? '已导出前 1000 条，可使用 after 继续' : '审计已导出');
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderGatewayOffline(box) {
  const wrap = el('div', 'offline-state');
  wrap.appendChild(el('span', 'offline-icon', '!'));
  wrap.appendChild(el('h3', 'offline-title', '模型网关未连接'));
  wrap.appendChild(
    el(
      'p',
      'offline-desc',
      '无法读取网关状态。请确认 tongtian-gateway 已启动，且服务端已配置网关管理地址与访问令牌，然后重新检测。'
    )
  );
  const retry = el('button', 'btn small', '重新检测');
  retry.type = 'button';
  retry.addEventListener('click', () => loadGatewayPanel());
  wrap.appendChild(retry);
  box.replaceChildren(wrap);
}

async function loadGatewayHealth() {
  const box = $('gateway-health');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api(gatewayPath('/health'));
    const upload = data.usageUpload || {};
    const webhook = data.auditWebhook || {};
    const quotaResetAt = data.quotaResetAt ? Date.parse(data.quotaResetAt) : 0;
    const rows = [
      ['网关', (data.gatewayId || '—') + ' · v' + (data.gatewayVersion || '—')],
      ['环境', data.environment || '—'],
      ['策略版本', (data.policyVersion || '—') + (data.policyStale ? '（已过期）' : '')],
      ['已配置 provider', String(data.providersConfigured ?? '—')],
      [
        '配额主体',
        String(data.quotaPrincipals ?? '—') +
          ' · 重置 ' +
          (quotaResetAt ? formatDateTime(quotaResetAt) : data.quotaResetAt || '—'),
      ],
      [
        '用量上报',
        (upload.enabled ? '已启用' : '未启用') +
          ' · 待上报 ' + formatInt(upload.pending) +
          (upload.consecutiveFailures
            ? ' · 连续失败 ' + upload.consecutiveFailures + ' 次（' + (upload.lastError || '') + '）'
            : ''),
      ],
      [
        '审计外送',
        (webhook.enabled ? '已启用' : '未启用') +
          ' · 待投递 ' + formatInt(webhook.pending) +
          (webhook.consecutiveFailures
            ? ' · 连续失败 ' + webhook.consecutiveFailures + ' 次（' + (webhook.lastError || '') + '）'
            : '') +
          (webhook.lastDeliveredAt
            ? ' · 最近成功 ' + formatDateTime(webhook.lastDeliveredAt)
            : ''),
      ],
    ];
    const table = el('table');
    const tbody = el('tbody');
    rows.forEach(([label, value]) => {
      const row = el('tr');
      row.appendChild(el('th', null, label));
      row.appendChild(el('td', null, value));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    box.replaceChildren(table);
    return true;
  } catch (error) {
    if (gatewayUnavailable(error)) {
      renderGatewayOffline(box);
    } else {
      showPanelError(box, error);
    }
    return false;
  }
}

async function loadGatewayProviders() {
  const box = $('gateway-providers');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api(gatewayPath('/providers'));
    const providers = data.providers || [];
    if (!providers.length) {
      box.replaceChildren(el('p', 'empty', '还没有配置 provider'));
      return;
    }
    const rows = providers.map(buildProviderRow);
    box.replaceChildren(
      buildTable(['Provider', '上游地址', '状态', '密钥（只显示 last4）', '操作'], rows)
    );
  } catch (error) {
    showPanelError(box, error);
  }
}

function buildProviderRow(provider) {
  const row = el('tr');

  const name = el('td', 'name-cell');
  name.appendChild(el('span', null, provider.label || provider.id));
  name.appendChild(el('span', 'sub mono', provider.id));
  row.appendChild(name);

  row.appendChild(el('td', 'mono', provider.baseUrl || '—'));

  const status = el('td');
  status.appendChild(
    el('span', 'badge ' + (provider.enabled ? 'ok' : 'muted'), provider.enabled ? '启用' : '停用')
  );
  if (!provider.configured) status.appendChild(el('span', 'badge muted', '无密钥'));
  row.appendChild(status);

  const keysCell = el('td');
  const keys = provider.keys || [];
  if (!keys.length) keysCell.appendChild(el('span', 'muted', '—'));
  keys.forEach((key) => {
    const line = el('div', 'key-line');
    line.appendChild(el('span', 'mono', (key.label || '密钥') + ' · ****' + key.last4));
    if (key.disabledAt) {
      line.appendChild(el('span', 'badge muted', '已停用'));
    } else if (canWriteGateway()) {
      const rotate = el('button', 'btn small', '轮换');
      rotate.type = 'button';
      rotate.addEventListener('click', () => rotateProviderKey(provider, key));
      const remove = el('button', 'btn small danger', '停用');
      remove.type = 'button';
      remove.addEventListener('click', () => disableProviderKey(provider, key));
      line.append(rotate, remove);
    }
    keysCell.appendChild(line);
  });
  row.appendChild(keysCell);

  const cell = el('td');
  if (canWriteGateway()) {
    const actions = el('div', 'row-actions');
    const addKey = el('button', 'btn small', '新增密钥');
    addKey.type = 'button';
    addKey.addEventListener('click', () => createProviderKey(provider));
    actions.appendChild(addKey);
    const toggle = el('button', 'btn small' + (provider.enabled ? ' danger' : ''), provider.enabled ? '停用' : '启用');
    toggle.type = 'button';
    toggle.addEventListener('click', async () => {
      try {
        await api(gatewayPath('/providers/' + encodeURIComponent(provider.id)), {
          method: 'PATCH',
          body: { enabled: !provider.enabled },
        });
        toast(provider.enabled ? '已停用 provider' : '已启用 provider');
        loadGatewayProviders();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
    actions.appendChild(toggle);
    cell.appendChild(actions);
  } else {
    cell.appendChild(el('span', 'muted', '只读'));
  }
  row.appendChild(cell);
  return row;
}

function showSecretOnce(secret) {
  const box = $('gateway-secret');
  $('gateway-secret-text').textContent = secret;
  box.hidden = false;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function createProviderKey(provider) {
  const label = window.prompt('密钥标签（可留空）', '');
  if (label === null) return;
  const secret = window.prompt('粘贴密钥明文（只保存在网关，不会回显）', '');
  if (secret === null) return;
  if (!secret.trim()) {
    toast('密钥不能为空', 'error');
    return;
  }
  try {
    const data = await api(gatewayPath('/providers/' + encodeURIComponent(provider.id) + '/keys'), {
      method: 'POST',
      body: { label: label.trim(), secret: secret.trim() },
    });
    showSecretOnce(data.key.secret);
    toast('密钥已创建，明文只显示这一次');
    loadGatewayProviders();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function rotateProviderKey(provider, key) {
  const secret = window.prompt('粘贴新密钥明文（旧密钥立即停用）', '');
  if (secret === null) return;
  if (!secret.trim()) {
    toast('密钥不能为空', 'error');
    return;
  }
  try {
    const data = await api(
      gatewayPath(
        '/providers/' + encodeURIComponent(provider.id) + '/keys/' + encodeURIComponent(key.id) + '/rotate'
      ),
      { method: 'POST', body: { secret: secret.trim() } }
    );
    showSecretOnce(data.key.secret);
    toast('已轮换，旧密钥立即停用');
    loadGatewayProviders();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function disableProviderKey(provider, key) {
  if (!window.confirm('确定停用密钥 ' + (key.label || key.id) + '（****' + key.last4 + '）？')) return;
  try {
    await api(
      gatewayPath(
        '/providers/' + encodeURIComponent(provider.id) + '/keys/' + encodeURIComponent(key.id)
      ),
      { method: 'DELETE' }
    );
    toast('已停用该密钥');
    loadGatewayProviders();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function submitProvider(event) {
  event.preventDefault();
  const id = $('gw-provider-id').value.trim();
  const label = $('gw-provider-label').value.trim();
  const baseUrl = $('gw-provider-url').value.trim();
  if (!id || !baseUrl) {
    toast('provider id 与上游地址必填', 'error');
    return;
  }
  try {
    await api(gatewayPath('/providers'), {
      method: 'POST',
      body: {
        id,
        label: label || id,
        baseUrl,
        allowPrivate: $('gw-provider-private').checked,
      },
    });
    toast('provider 已新增，请继续添加密钥');
    $('gw-provider-id').value = '';
    $('gw-provider-label').value = '';
    $('gw-provider-url').value = '';
    $('gw-provider-private').checked = false;
    loadGatewayProviders();
    loadGatewayHealth();
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── CLI API Key（M3） ───────────────────────────────────────

function formatDateTime(ms) {
  return ms ? new Date(ms).toLocaleString('zh-CN') : '';
}

async function loadGatewayApiKeys() {
  const box = $('gateway-apikeys');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api(gatewayPath('/api-keys'));
    const keys = data.keys || [];
    if (!keys.length) {
      box.replaceChildren(el('p', 'empty', '还没有签发 CLI API Key'));
      return;
    }
    box.replaceChildren(
      buildTable(
        ['名称 / ID', '绑定主体', '权限范围', '模型白名单', '限流 / 月配额', '当月已用', '状态 / 过期', '操作'],
        keys.map(buildApiKeyRow)
      )
    );
  } catch (error) {
    showPanelError(box, error);
  }
}

function buildApiKeyRow(item) {
  const row = el('tr');

  const name = el('td', 'name-cell');
  name.appendChild(el('span', null, item.label || '未命名'));
  name.appendChild(el('span', 'sub mono', item.id));
  if (item.rotatedAt) {
    name.appendChild(el('span', 'sub mono', '轮换于 ' + formatDateTime(item.rotatedAt)));
  } else if (item.createdAt) {
    name.appendChild(el('span', 'sub', '创建于 ' + formatDateTime(item.createdAt)));
  }

  row.appendChild(name);
  row.appendChild(el('td', 'mono', item.tenant + '/' + item.dept + '/' + item.sub));
  row.appendChild(el('td', 'mono', (item.scopes || []).join(', ')));

  const models = el(
    'td',
    'mono',
    (item.modelAllowlist || []).length ? item.modelAllowlist.join(', ') : '按部门策略'
  );
  if ((item.modelAllowlist || []).length) models.title = item.modelAllowlist.join(', ');
  row.appendChild(models);

  const limits = el('td', 'mono');
  limits.appendChild(el('span', null, item.rateLimitRpm ? formatInt(item.rateLimitRpm) + ' RPM' : '不限'));
  limits.appendChild(
    el(
      'span',
      'sub',
      '月度 ' + (item.quotaMonthlyTokens ? formatInt(item.quotaMonthlyTokens) : '不限')
    )
  );
  row.appendChild(limits);

  row.appendChild(el('td', 'mono', formatInt(item.usedTokens)));

  const revoked = Boolean(item.revokedAt);
  const expired = Boolean(item.expiresAt && item.expiresAt <= Date.now());
  const status = el('td');
  status.appendChild(
    el(
      'span',
      'badge ' + (revoked || expired ? 'revoked' : 'ok'),
      revoked ? '已吊销' : expired ? '已过期' : '有效'
    )
  );
  status.appendChild(
    el('span', 'sub', item.expiresAt ? '至 ' + formatDateTime(item.expiresAt) : '永不过期')
  );
  row.appendChild(status);

  const actions = el('td', 'actions');
  if (canWriteGateway()) {
    const rotate = el('button', 'btn small', '轮换');
    rotate.disabled = revoked;
    rotate.addEventListener('click', () => rotateApiKey(item));
    actions.appendChild(rotate);
    const button = el('button', 'btn small danger', '吊销');
    button.disabled = revoked;
    button.addEventListener('click', () => revokeApiKey(item));
    actions.appendChild(button);
  } else {
    actions.appendChild(el('span', 'muted', '只读'));
  }
  row.appendChild(actions);
  return row;
}

async function submitApiKey(event) {
  event.preventDefault();
  const tenant = $('gw-apikey-tenant').value.trim();
  const dept = $('gw-apikey-dept').value.trim();
  const sub = $('gw-apikey-sub').value.trim();
  const scopes = Array.from(
    document.querySelectorAll('input[name="gw-scope"]:checked')
  ).map((input) => input.value);
  if (!tenant || !dept || !sub) {
    toast('租户、部门、用户必填', 'error');
    return;
  }
  if (!scopes.length) {
    toast('至少选择一个 scope', 'error');
    return;
  }
  const modelsRaw = $('gw-apikey-models').value.trim();
  const modelAllowlist = modelsRaw
    ? modelsRaw.split(',').map((item) => item.trim()).filter(Boolean)
    : [];
  const expiresRaw = $('gw-apikey-expires').value;
  const expiresAt = expiresRaw ? new Date(expiresRaw).getTime() : null;
  if (expiresAt !== null && !Number.isFinite(expiresAt)) {
    toast('过期时间格式无效', 'error');
    return;
  }
  const rpmRaw = $('gw-apikey-rpm').value.trim();
  const quotaRaw = $('gw-apikey-quota').value.trim();
  const rateLimitRpm = rpmRaw ? Number(rpmRaw) : 0;
  const quotaMonthlyTokens = quotaRaw ? Number(quotaRaw) : 0;
  if (
    !Number.isInteger(rateLimitRpm) ||
    rateLimitRpm < 0 ||
    rateLimitRpm > 10000
  ) {
    toast('RPM 必须是 0–10000 的整数', 'error');
    return;
  }
  if (!Number.isInteger(quotaMonthlyTokens) || quotaMonthlyTokens < 0) {
    toast('月度 token 配额必须是非负整数', 'error');
    return;
  }
  try {
    const data = await api(gatewayPath('/api-keys'), {
      method: 'POST',
      body: {
        label: $('gw-apikey-label').value.trim(),
        tenant,
        dept,
        sub,
        scopes,
        modelAllowlist,
        rateLimitRpm,
        quotaMonthlyTokens,
        expiresAt,
      },
    });
    $('apikey-secret-text').textContent = data.key.secret;
    $('apikey-secret').hidden = false;
    toast('Key 已签发，明文只显示一次');
    $('gw-apikey-label').value = '';
    $('gw-apikey-models').value = '';
    $('gw-apikey-rpm').value = '';
    $('gw-apikey-quota').value = '';
    $('gw-apikey-expires').value = '';
    loadGatewayApiKeys();
    loadGatewayHealth();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function rotateApiKey(item) {
  if (
    !confirm(
      '轮换 API Key「' +
        (item.label || item.id) +
        '」？新明文只显示一次，旧 secret 立即失效。'
    )
  ) {
    return;
  }
  try {
    const data = await api(
      gatewayPath('/api-keys/' + encodeURIComponent(item.id) + '/rotate'),
      { method: 'POST' }
    );
    $('apikey-secret-text').textContent = data.key.secret;
    $('apikey-secret').hidden = false;
    toast('已轮换，旧 secret 立即失效');
    loadGatewayApiKeys();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function revokeApiKey(item) {
  if (!confirm('吊销 API Key「' + (item.label || item.id) + '」？吊销后 CLI 立即失效。')) {
    return;
  }
  try {
    await api(gatewayPath('/api-keys/' + encodeURIComponent(item.id)), {
      method: 'DELETE',
    });
    toast('已吊销 API Key');
    loadGatewayApiKeys();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadGatewayQuotas() {
  const box = $('gateway-quotas');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api(gatewayPath('/quotas'));
    $('gateway-quota-meta').textContent =
      '周期 ' + data.period + ' · 下次重置 ' + data.resetAt;
    const overrides = new Map(
      (data.overrides || []).map((item) => [item.principal + '|' + item.period, item])
    );
    const quotas = data.quotas || [];
    if (!quotas.length && !overrides.size) {
      box.replaceChildren(el('p', 'empty', '本月还没有用量或额度覆盖'));
      return;
    }
    const rows = quotas.map((item) => {
      const override = overrides.get(item.principal + '|' + item.period);
      const row = el('tr');
      row.appendChild(el('td', 'mono', item.principal));
      row.appendChild(el('td', 'mono', item.period));
      row.appendChild(el('td', null, formatInt(item.committedTokens)));
      row.appendChild(el('td', null, formatInt(item.reservedTokens)));
      row.appendChild(el('td', null, formatInt(item.activeRequests)));
      row.appendChild(
        el('td', null, override ? formatInt(override.monthlyTokens) : '—')
      );
      row.appendChild(el('td', 'mono', override ? override.updatedBy || '—' : '—'));
      return row;
    });
    overrides.forEach((override, key) => {
      if (quotas.some((item) => item.principal + '|' + item.period === key)) return;
      const row = el('tr');
      row.appendChild(el('td', 'mono', override.principal));
      row.appendChild(el('td', 'mono', override.period));
      row.appendChild(el('td', null, '0'));
      row.appendChild(el('td', null, '0'));
      row.appendChild(el('td', null, '0'));
      row.appendChild(el('td', null, formatInt(override.monthlyTokens)));
      row.appendChild(el('td', 'mono', override.updatedBy || '—'));
      rows.push(row);
    });
    box.replaceChildren(
      buildTable(['主体', '周期', '已结算', '在途预留', '在途请求', '覆盖额度', '更新人'], rows)
    );
  } catch (error) {
    showPanelError(box, error);
  }
}

async function submitQuota(event) {
  event.preventDefault();
  const principal = $('gw-quota-principal').value.trim();
  const monthlyRaw = $('gw-quota-monthly').value.trim();
  const period = $('gw-quota-period').value.trim();
  if (!principal) {
    toast('请填写配额主体', 'error');
    return;
  }
  let monthly = null;
  if (monthlyRaw) {
    monthly = Number(monthlyRaw);
    if (!Number.isInteger(monthly) || monthly < 0) {
      toast('额度必须是非负整数，留空表示恢复策略值', 'error');
      return;
    }
  }
  const body = { monthlyTokens: monthly };
  if (period) body.period = period;
  try {
    const data = await api(gatewayPath('/quotas/' + encodeURIComponent(principal)), {
      method: 'PATCH',
      body,
    });
    toast(
      '已调整：生效额度 ' +
        (data.effectiveMonthlyTokens === null ? '不限' : formatInt(data.effectiveMonthlyTokens)) +
        '，已用 ' + formatInt(data.usedTokens)
    );
    loadGatewayQuotas();
    loadGatewayHealth();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadGatewayUsage() {
  const box = $('gateway-usage');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  const params = new URLSearchParams({
    period: $('gw-usage-period').value,
    tenant: $('gw-usage-tenant').value.trim(),
    dept: $('gw-usage-dept').value.trim(),
    sub: $('gw-usage-sub').value.trim(),
    model: $('gw-usage-model').value.trim(),
    limit: '200',
  });
  try {
    const data = await api(gatewayPath('/usage?' + params.toString()));
    const totals = data.totals || {};
    const costs = (totals.costs || [])
      .map((item) => formatMicros(item.micros, item.currency))
      .join(' / ');
    $('gateway-usage-meta').textContent =
      '共 ' + formatInt(data.rowCount) + ' 行 · ' +
      formatInt(totals.calls) + ' 次调用 · ' +
      formatInt(totals.totalTokens) + ' tokens · ' +
      '平均耗时 ' + (totals.avgLatencyMs === null ? '—' : formatInt(totals.avgLatencyMs) + ' ms') +
      ' · 成本 ' + (costs || '未计价');
    const rows = (data.rows || []).map((item) => {
      const row = el('tr');
      row.appendChild(el('td', 'mono', item.tenantId || '—'));
      row.appendChild(el('td', 'mono', item.departmentId || '—'));
      row.appendChild(el('td', 'mono', item.userId || '—'));
      row.appendChild(el('td', null, item.model));
      row.appendChild(el('td', null, item.provider || '—'));
      row.appendChild(el('td', 'mono', item.day));
      row.appendChild(el('td', null, formatInt(item.calls)));
      row.appendChild(el('td', null, formatInt(item.totalTokens)));
      row.appendChild(el('td', 'mono', formatMicros(item.costMicros, item.currency)));
      row.appendChild(el('td', null, formatInt(item.avgLatencyMs) + ' ms'));
      return row;
    });
    if (!rows.length) {
      box.replaceChildren(el('p', 'empty', '没有符合条件的网关用量'));
      return;
    }
    box.replaceChildren(
      buildTable(['租户', '部门', '用户', '模型', 'Provider', '日期', '调用', 'Tokens', '成本', '平均耗时'], rows)
    );
  } catch (error) {
    showPanelError(box, error);
  }
}

// ── 组织与部门（M1.4 T4.12） ─────────────────────────────────

async function loadDepartmentsPanel() {
  await loadDepartments();
  loadDeptMembers();
}

async function loadDepartments() {
  const box = $('dept-table');
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const data = await api('/api/v1/admin/departments');
    state.departments = data.departments || [];
    $('dept-meta').textContent = '共 ' + state.departments.length + ' 个部门';
    if (!state.departments.length) {
      box.replaceChildren(el('p', 'empty', '还没有部门，先在左上方新增'));
    } else {
      const rows = state.departments.map(buildDepartmentRow);
      box.replaceChildren(buildTable(['部门', '组织', '标识', '成员', '操作'], rows));
    }
    renderDepartmentTarget();
  } catch (error) {
    showPanelError(box, error);
  }
}

function buildDepartmentRow(dept) {
  const row = el('tr');
  const name = el('td', 'name-cell', dept.name || dept.id);
  row.appendChild(name);
  row.appendChild(el('td', 'mono', dept.org));
  row.appendChild(el('td', 'mono', dept.id));
  row.appendChild(el('td', null, formatInt(dept.members)));
  const actions = el('div', 'row-actions');
  const rename = el('button', 'btn small', '重命名');
  rename.type = 'button';
  rename.addEventListener('click', async () => {
    const name = window.prompt('新的部门名称', dept.name || dept.id);
    if (name === null) return;
    if (!name.trim()) {
      toast('部门名称不能为空', 'error');
      return;
    }
    try {
      await api(
        '/api/v1/admin/departments/' +
          encodeURIComponent(dept.org) +
          '/' +
          encodeURIComponent(dept.id),
        { method: 'PATCH', body: { name: name.trim() } }
      );
      toast('部门名称已更新');
      await loadDepartments();
      loadDeptMembers();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  actions.appendChild(rename);
  const cell = el('td');
  cell.appendChild(actions);
  row.appendChild(cell);
  return row;
}

function renderDepartmentTarget() {
  const select = $('dept-member-target');
  const previous = state.deptTarget;
  select.replaceChildren();
  state.departments.forEach((dept) => {
    const value = dept.org + '/' + dept.id;
    const option = el('option', null, dept.org + ' / ' + (dept.name || dept.id) + '（' + dept.id + '）');
    option.value = value;
    select.appendChild(option);
  });
  if (!state.departments.length) {
    state.deptTarget = '';
    return;
  }
  state.deptTarget = state.departments.some((dept) => dept.org + '/' + dept.id === previous)
    ? previous
    : state.departments[0].org + '/' + state.departments[0].id;
  select.value = state.deptTarget;
}

async function loadDeptMembers() {
  const box = $('dept-members');
  const hint = $('dept-member-hint');
  if (!state.deptTarget) {
    box.replaceChildren(el('p', 'empty', '创建部门后即可分配成员'));
    hint.textContent = '还没有部门，创建后再分配成员。';
    return;
  }
  const [org, dept] = state.deptTarget.split('/');
  const deptInfo = state.departments.find((item) => item.org === org && item.id === dept);
  hint.textContent =
    '正在查看「' + (deptInfo ? deptInfo.name : dept) + '」的成员；变更后成员需重新登录，新 claims 才会生效。';
  box.replaceChildren(el('p', 'loading', '加载中…'));
  try {
    const params = new URLSearchParams({
      q: state.deptQ,
      page: '1',
      pageSize: '50',
    });
    const data = await api('/api/v1/admin/users?' + params.toString());
    const rows = (data.users || []).map((user) => buildMemberRow(user, org, dept));
    if (!rows.length) {
      box.replaceChildren(el('p', 'empty', '没有匹配的账号'));
      return;
    }
    box.replaceChildren(buildTable(['昵称', '邮箱', '组织', '当前部门', '操作'], rows));
  } catch (error) {
    showPanelError(box, error);
  }
}

function buildMemberRow(user, org, dept) {
  const row = el('tr');
  row.appendChild(el('td', 'name-cell', user.nickname || '—'));
  row.appendChild(el('td', 'mono', user.email));
  row.appendChild(el('td', 'mono', user.org || '—'));
  const depts = user.depts || [];
  const deptCell = el('td', 'mono', depts.map((item) => item.id).join('、') || '—');
  row.appendChild(deptCell);

  const actions = el('div', 'row-actions');
  const member = depts.some((item) => item.id === dept);
  if (user.org !== org) {
    const note = el('span', 'badge muted', '组织不匹配');
    note.title = '请先在用户页把该账号切换到 ' + org;
    actions.appendChild(note);
  } else {
    const toggle = el('button', 'btn small' + (member ? ' danger' : ''), member ? '移出部门' : '加入部门');
    toggle.type = 'button';
    toggle.addEventListener('click', () => setMembership(user, org, dept, !member));
    actions.appendChild(toggle);
  }
  const cell = el('td');
  cell.appendChild(actions);
  row.appendChild(cell);
  return row;
}

async function setMembership(user, org, dept, join) {
  const current = (user.depts || []).map((item) => item.id);
  const wanted = join ? [...current, dept] : current.filter((id) => id !== dept);
  let primary = user.primaryDept || '';
  if (!join && primary === dept) primary = wanted[0] || '';
  try {
    const data = await api('/api/v1/admin/users/' + encodeURIComponent(user.uid), {
      method: 'PATCH',
      body: { org, depts: wanted, primaryDept: primary },
    });
    toast(
      join
        ? '已加入 ' + dept + '；旧会话已注销，重新登录后生效'
        : '已移出 ' + dept + '；旧会话已注销，重新登录后生效'
    );
    if (typeof data.sessionsRevoked === 'number' && data.sessionsRevoked > 0) {
      toast('已注销 ' + data.sessionsRevoked + ' 个旧会话');
    }
    loadDepartments();
    loadDeptMembers();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function submitDepartment(event) {
  event.preventDefault();
  const org = $('dept-org').value.trim();
  const id = $('dept-id').value.trim();
  const name = $('dept-name').value.trim();
  if (!org || !id) {
    toast('组织与部门标识必填', 'error');
    return;
  }
  try {
    await api('/api/v1/admin/departments', {
      method: 'POST',
      body: { org, id, name: name || id },
    });
    toast('部门已创建');
    $('dept-id').value = '';
    $('dept-name').value = '';
    await loadDepartments();
    loadDeptMembers();
  } catch (error) {
    toast(error.message, 'error');
  }
}

// ── 事件与启动 ──────────────────────────────────────────────

function bindEvents() {
  document.querySelectorAll('.nav-link').forEach((btn) => {
    btn.addEventListener('click', () => {
      location.hash = '#/' + btn.dataset.view;
    });
  });

  $('market-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.q = $('market-q').value.trim();
    state.page = 1;
    loadMarket();
  });
  $('market-q').addEventListener(
    'input',
    debounce(() => {
      const value = $('market-q').value.trim();
      if (value === state.q) return;
      state.q = value;
      state.page = 1;
      loadMarket();
    }, 350)
  );
  $('market-sort').addEventListener('change', () => {
    state.sort = $('market-sort').value;
    state.page = 1;
    loadMarket();
  });

  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-overlay').addEventListener('click', closeDrawer);

  $('auth-close').addEventListener('click', closeAuth);
  $('auth-overlay').addEventListener('click', closeAuth);
  $('auth-form').addEventListener('submit', submitAuth);
  document.querySelectorAll('[data-auth-mode]').forEach((btn) => {
    btn.addEventListener('click', () => setAuthMode(btn.dataset.authMode));
  });

  document.querySelectorAll('[data-admin-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.adminTab = btn.dataset.adminTab;
      loadAdmin();
    });
  });

  $('gateway-provider-form').addEventListener('submit', submitProvider);
  $('gateway-quota-form').addEventListener('submit', submitQuota);
  $('gateway-usage-form').addEventListener('submit', (event) => {
    event.preventDefault();
    loadGatewayUsage();
  });
  $('gateway-secret-close').addEventListener('click', () => {
    $('gateway-secret').hidden = true;
    $('gateway-secret-text').textContent = '';
  });
  $('gateway-secret-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('gateway-secret-text').textContent);
      toast('密钥已复制');
    } catch {
      toast('复制失败，请手动选择文本', 'error');
    }
  });

  $('gateway-apikey-form').addEventListener('submit', submitApiKey);
  $('apikey-secret-close').addEventListener('click', () => {
    $('apikey-secret').hidden = true;
    $('apikey-secret-text').textContent = '';
  });
  $('apikey-secret-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('apikey-secret-text').textContent);
      toast('API Key 已复制');
    } catch {
      toast('复制失败，请手动选择文本', 'error');
    }
  });

  $('audit-source').addEventListener('change', () => {
    state.auditSource = $('audit-source').value;
    initAuditPanel();
  });
  $('audit-refresh').addEventListener('click', () => initAuditPanel());
  $('audit-verify').addEventListener('click', verifyAudit);
  $('audit-export').addEventListener('click', exportAudit);
  $('audit-more').addEventListener('click', () => loadAuditPage(true));

  $('dept-form').addEventListener('submit', submitDepartment);
  $('dept-member-target').addEventListener('change', () => {
    state.deptTarget = $('dept-member-target').value;
    loadDeptMembers();
  });
  $('dept-member-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.deptQ = $('dept-member-q').value.trim();
    loadDeptMembers();
  });
  $('dept-member-q').addEventListener(
    'input',
    debounce(() => {
      const value = $('dept-member-q').value.trim();
      if (value === state.deptQ) return;
      state.deptQ = value;
      loadDeptMembers();
    }, 350)
  );

  $('admin-item-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.adminQ = $('admin-item-q').value.trim();
    state.adminPage = 1;
    loadAdminItems();
  });
  $('admin-item-q').addEventListener(
    'input',
    debounce(() => {
      const value = $('admin-item-q').value.trim();
      if (value === state.adminQ) return;
      state.adminQ = value;
      state.adminPage = 1;
      loadAdminItems();
    }, 350)
  );

  $('admin-user-search').addEventListener('submit', (event) => {
    event.preventDefault();
    state.userQ = $('admin-user-q').value.trim();
    state.userPage = 1;
    loadAdminUsers();
  });
  $('admin-user-q').addEventListener(
    'input',
    debounce(() => {
      const value = $('admin-user-q').value.trim();
      if (value === state.userQ) return;
      state.userQ = value;
      state.userPage = 1;
      loadAdminUsers();
    }, 350)
  );

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeDrawer();
      closeAuth();
    }
  });

  window.addEventListener('hashchange', route);
}

async function boot() {
  document.querySelectorAll('input[placeholder]:not([title])').forEach((input) => {
    input.title = input.placeholder;
  });
  bindEvents();
  renderAccount();
  if (state.tokens && state.tokens.accessToken) {
    try {
      const data = await api('/api/v1/auth/me');
      state.profile = data.profile;
      renderAccount();
    } catch {
      clearSession();
      renderAccount();
    }
  }
  route();
}

boot();
