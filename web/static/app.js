// TradingAgents mobile SPA. No build step — pure vanilla JS using template
// strings + manual render(). State is a single object; render() diffs by
// replacing innerHTML (good enough for the data sizes we have).

const $ = (sel, root = document) => root.querySelector(sel);
const root = $('#root');
let state = { view: 'login', me: null, tasks: [], selected: null, devices: [] };
let pollHandle = null;
let toastTimer = null;

// ----- API ----------------------------------------------------------------

// Base path the SPA is mounted under. Read from <base href> in index.html
// (e.g. "/trading-agents/") so all API calls automatically work whether
// the app is served at the domain root or behind a sub-path. When no
// <base> is present, fall back to the origin root.
const BASE = (document.querySelector('base')?.getAttribute('href') || '/').replace(/\/?$/, '/');
const _api = (path) => `${BASE}api${path}`;

const api = {
  async login(password) {
    const r = await fetch(_api('/login'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (!r.ok) throw new Error('Wrong password');
    return r.json();
  },
  async logout() {
    await fetch(_api('/logout'), { method: 'POST' });
  },
  async me() {
    const r = await fetch(_api('/me'));
    if (r.status === 401) return null;
    return r.json();
  },
  async listTasks() {
    const r = await fetch(_api('/tasks'));
    if (!r.ok) throw new Error('list failed');
    return r.json();
  },
  async getTask(id) {
    const r = await fetch(_api(`/tasks/${id}`));
    if (!r.ok) throw new Error('not found');
    return r.json();
  },
  async submitTask(body) {
    const r = await fetch(_api('/tasks'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || 'submit failed');
    }
    return r.json();
  },
  async listDevices() {
    const r = await fetch(_api('/devices'));
    return r.json();
  },
  async listModels() {
    const r = await fetch(_api('/models'));
    if (!r.ok) return { models: ['minimax-m3:cloud'], source: 'fallback' };
    return r.json();
  },
  async deleteDevice(fp) {
    return fetch(_api(`/devices/${fp}`), { method: 'DELETE' });
  },
};

// ----- Helpers ------------------------------------------------------------

function toast(msg) {
  let t = $('#toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 2200);
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// Render markdown to HTML. marked.js is loaded as a global <script> before
// this file. We sanitize the output — agent reports are LLM-generated and
// may contain raw HTML or stray <script> tags that should not execute in
// a logged-in session.
function md(text) {
  if (text == null || text === '') return '<em>(no content)</em>';
  const raw = window.marked.parse(String(text), { breaks: true, gfm: true });
  return sanitize(raw);
}

// Minimal HTML sanitizer: strip <script>, <style>, <iframe>, event handlers,
// and javascript: URLs. Good enough for trusted-source markdown (LLM output
// of an authenticated app) without pulling in DOMPurify's full weight.
function sanitize(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const FORBIDDEN = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK']);
  const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_ELEMENT);
  const drop = [];
  for (let n = tpl.content.firstElementChild; n; ) {
    if (FORBIDDEN.has(n.tagName)) {
      drop.push(n); n = n.nextElementSibling || nextInWalker(walker, n);
    } else {
      n = n.nextElementSibling || nextInWalker(walker, n);
    }
  }
  drop.forEach(el => el.remove());
  tpl.content.querySelectorAll('*').forEach(el => {
    [...el.attributes].forEach(a => {
      if (/^on/i.test(a.name)) el.removeAttribute(a.name);
      if ((a.name === 'href' || a.name === 'src') &&
          /^\s*javascript:/i.test(a.value)) el.removeAttribute(a.name);
    });
    if (FORBIDDEN.has(el.tagName)) el.remove();
  });
  return tpl.innerHTML;
}
function nextInWalker(walker, fallback) {
  const next = walker.nextNode();
  return next || (fallback && fallback.nextElementSibling);
}

function fmtDate(iso) {
  if (!iso) return '';
  // Show as local short date.
  const d = new Date(iso);
  return d.toLocaleString();
}

const RATING_CLASS = {
  buy: 'buy', overweight: 'overweight',
  hold: 'hold',
  underweight: 'underweight', sell: 'sell',
};

function statusBadge(s, rating) {
  if (s === 'running') return '<span class="badge running"><span class="spinner"></span> running</span>';
  if (s === 'pending') return '<span class="badge pending">queued</span>';
  if (s === 'failed')  return '<span class="badge failed">failed</span>';
  if (rating) {
    const cls = RATING_CLASS[rating.toLowerCase()] || '';
    return `<span class="badge ${cls}">${esc(rating)}</span>`;
  }
  return '<span class="badge completed">done</span>';
}

const REPORT_TABS = [
  ['market',        'Market'],
  ['sentiment',     'Sentiment'],
  ['news',          'News'],
  ['fundamentals',  'Fundamentals'],
  ['research_bull', 'Bull'],
  ['research_bear', 'Bear'],
  ['research_judge','Research'],
  ['trader',        'Trader'],
  ['risk_aggressive','Risk ↑'],
  ['risk_conservative','Risk ↓'],
  ['risk_neutral',  'Risk ='],
  ['risk_judge',    'Risk Mgr'],
];

// ----- Views --------------------------------------------------------------

function viewLogin() {
  return `
    <div class="login-card">
      <h2>TradingAgents</h2>
      <p>Enter the shared password. Your device will be registered on first login.</p>
      <form id="loginForm">
        <input class="field" id="password" type="password" autocomplete="current-password"
               placeholder="Password" autofocus
               style="width:100%;height:44px;padding:0 12px;font-size:16px;
                      background:var(--bg-soft);color:var(--fg);
                      border:1px solid var(--border);border-radius:8px" />
        <button class="primary" type="submit" style="margin-top:14px">Sign in</button>
      </form>
      <div class="err" id="loginErr" hidden></div>
      <div class="disclaimer">Research tool. Not financial advice.</div>
    </div>
  `;
}

function viewList() {
  const items = state.tasks.map(t => `
    <a class="task" data-id="${t.id}">
      <div class="line1">
        <span class="ticker">${esc(t.ticker)}</span>
        <span class="date">${esc(t.trade_date)}</span>
        <span style="margin-left:auto">${statusBadge(t.status, t.rating)}</span>
      </div>
      <div class="line2">
        <span>${esc(t.asset_type)}</span>
        <span class="right">${fmtDate(t.created_at)}</span>
      </div>
    </a>
  `).join('');

  return `
    <div class="app">
      <header class="topbar">
        <h1>TradingAgents</h1>
        <span class="spacer"></span>
        <button class="icon" id="settingsBtn" aria-label="Settings">⚙</button>
        <button class="icon" id="logoutBtn" aria-label="Sign out">⎋</button>
      </header>
      <main>
        <div class="disclaimer">Research tool. Not financial advice.</div>
        <div class="task-list">
          ${items || '<div class="empty">No tasks yet. Tap + to start one.</div>'}
        </div>
      </main>
      <button class="fab" id="newTaskBtn" aria-label="New analysis">+</button>
    </div>
  `;
}

function viewNewTask() {
  const today = new Date().toISOString().slice(0, 10);
  return `
    <div class="app">
      <header class="topbar">
        <button class="icon" id="backBtn" aria-label="Back" style="margin-left:-12px">‹</button>
        <h1>New Analysis</h1>
      </header>
      <main>
        <form id="taskForm">
          <div class="field">
            <label>Ticker</label>
            <input id="ticker" placeholder="AAPL, 0700.HK, BTC-USD" required
                   autocapitalize="characters" />
          </div>
          <div class="row">
            <div class="field">
              <label>Trade date</label>
              <input id="trade_date" type="date" value="${today}" required />
            </div>
            <div class="field">
              <label>Asset</label>
              <select id="asset_type">
                <option value="stock">Stock</option>
                <option value="crypto">Crypto</option>
              </select>
            </div>
          </div>
          <div class="field" style="display:none">
            <label>LLM provider</label>
            <select id="llm_provider">
              <option value="ollama">Ollama (local)</option>
            </select>
          </div>
          <div class="row">
            <div class="field">
              <label>Deep model</label>
              <select id="deep_think_llm">
                <option value="minimax-m3:cloud">minimax-m3:cloud</option>
              </select>
            </div>
            <div class="field">
              <label>Quick model</label>
              <select id="quick_think_llm">
                <option value="minimax-m3:cloud">minimax-m3:cloud</option>
              </select>
            </div>
          </div>
          <div class="row">
            <div class="field">
              <label>Debate rounds</label>
              <select id="max_debate_rounds">
                <option>1</option><option>2</option><option>3</option>
              </select>
            </div>
            <div class="field">
              <label>Report language</label>
              <select id="output_language">
                <option>English</option>
                <option>中文</option>
                <option>日本語</option>
                <option>Español</option>
              </select>
            </div>
          </div>
          <button class="primary" type="submit" id="submitBtn">Start analysis</button>
          <button class="secondary" type="button" id="cancelBtn" style="margin-top:8px">Cancel</button>
        </form>
      </main>
    </div>
  `;
}

function viewDetail() {
  const t = state.selected;
  if (!t) return viewList();
  const reports = t.reports || {};
  const tabs = REPORT_TABS.map(([k, label]) => `
    <button data-tab="${k}" class="${state.activeTab === k ? 'active' : ''}">${label}</button>
  `).join('');
  const report = reports[state.activeTab || 'market'] || '(no content)';
  const decision = t.final_decision || '(no decision yet)';

  return `
    <div class="app">
      <header class="topbar">
        <button class="back" id="backBtn">‹ Back</button>
      </header>
      <main>
        <div class="detail-head">
          <div class="ticker">${esc(t.ticker)}</div>
          <div class="meta">${esc(t.trade_date)} · ${esc(t.asset_type)}
            · ${statusBadge(t.status, t.rating)}</div>
          <div class="decision">${md(decision)}</div>
        </div>
        <div class="tabs">${tabs}</div>
        <div class="report md-body">${md(report)}</div>
      </main>
    </div>
  `;
}

function viewSettings() {
  const items = Object.entries(state.devices).map(([fp, d]) => `
    <div class="device-item">
      <div><strong>${esc(d.label || '(no label)')}</strong>
        <span class="fp"> · ${esc(fp)}</span>
      </div>
      <div class="ua">${esc(d.user_agent || '')}</div>
      <div class="ua">since ${esc(d.first_seen || '')}</div>
      <div style="margin-top:6px">
        <button class="secondary" data-rm="${esc(fp)}"
                style="width:auto;height:32px;padding:0 12px;font-size:13px">Remove</button>
      </div>
    </div>
  `).join('');

  return `
    <div class="drawer-mask" id="drawerMask">
      <div class="drawer">
        <h3>Devices</h3>
        ${items || '<div class="empty">No devices</div>'}
        <button class="secondary" id="closeDrawer" style="margin-top:16px">Close</button>
      </div>
    </div>
  `;
}

// ----- Render -------------------------------------------------------------

function render() {
  // Preserve scroll position when re-rendering the *same* view (e.g. a
  // status badge tick or a tab re-render); reset when navigating between
  // views, where a fresh page should start at the top. Without this,
  // any in-place re-render drops the user back to the top of the page
  // and resets the .tabs horizontal scroll container too.
  const prevView = render._lastView;
  const savedY = (prevView === state.view) ? window.scrollY : 0;
  const savedX = (prevView === state.view) ? window.scrollX : 0;
  const tabsEl = document.querySelector('.tabs');
  const savedTabX = (prevView === state.view && tabsEl) ? tabsEl.scrollLeft : 0;

  let html;
  if (state.view === 'login') html = viewLogin();
  else if (state.view === 'list') html = viewList();
  else if (state.view === 'new')  html = viewNewTask();
  else if (state.view === 'detail') html = viewDetail();
  else html = viewList();

  if (state.view === 'settings') {
    root.innerHTML = (state.view === 'login' ? viewLogin() : viewList()) + viewSettings();
  } else {
    root.innerHTML = html;
  }
  bind();

  // Restore after the new DOM is in place. Browsers fire this lazily,
  // so we hop to a microtask — the .tabs element may not exist yet on
  // a same-view re-render that swapped the detail body, so guard it.
  requestAnimationFrame(() => {
    window.scrollTo(savedX, savedY);
    const t = document.querySelector('.tabs');
    if (t && savedTabX) t.scrollLeft = savedTabX;
  });
  render._lastView = state.view;
}

function bind() {
  // Login
  const lf = $('#loginForm');
  if (lf) {
    lf.addEventListener('submit', async e => {
      e.preventDefault();
      const pw = $('#password').value;
      const err = $('#loginErr');
      err.hidden = true;
      try {
        await api.login(pw);
        await enterApp();
      } catch {
        err.textContent = 'Wrong password';
        err.hidden = false;
      }
    });
  }

  // List
  $('#newTaskBtn')?.addEventListener('click', async () => {
    state.view = 'new';
    // render() calls bind() at the end; we just need the model options
    // populated into the new <select> elements. populateModelOptions()
    // mutates the existing DOM (no re-render needed) and bind() does not
    // depend on the option list, so no second bind() call here.
    render();
    await populateModelOptions();
  });
  $('#logoutBtn')?.addEventListener('click', async () => {
    await api.logout();
    stopPolling();
    state = { view: 'login', me: null, tasks: [], selected: null, devices: [] };
    render();
  });
  $('#settingsBtn')?.addEventListener('click', async () => {
    state.devices = await api.listDevices();
    state.view = 'settings';
    render();
  });
  document.querySelectorAll('.task').forEach(el => {
    el.addEventListener('click', () => openTask(parseInt(el.dataset.id, 10)));
  });

  // New task
  const tf = $('#taskForm');
  if (tf) {
    tf.addEventListener('submit', async e => {
      e.preventDefault();
      const btn = $('#submitBtn');
      btn.disabled = true; btn.textContent = 'Submitting…';
      try {
        const body = {
          ticker: $('#ticker').value.trim(),
          trade_date: $('#trade_date').value,
          asset_type: $('#asset_type').value,
          llm_provider: $('#llm_provider').value,
          deep_think_llm: $('#deep_think_llm').value.trim(),
          quick_think_llm: $('#quick_think_llm').value.trim(),
          max_debate_rounds: parseInt($('#max_debate_rounds').value, 10),
          output_language: $('#output_language').value,
        };
        await api.submitTask(body);
        state.view = 'list';
        startPolling();
        await refreshTasks();
        render();
        toast('Task queued');
      } catch (err) {
        toast(err.message);
        btn.disabled = false; btn.textContent = 'Start analysis';
      }
    });
  }
  $('#cancelBtn')?.addEventListener('click', () => { state.view = 'list'; render(); });
  $('#backBtn')?.addEventListener('click', () => {
    if (state.view === 'detail') { state.view = 'list'; state.selected = null; render(); }
    else { state.view = 'list'; render(); }
  });

  // Detail tabs
  //
  // The naive approach (state.activeTab = ...; render()) drops the page's
  // scroll position and resets the .tabs horizontal scroll back to 0, so
  // tapping a tab on the right side looks like nothing happened. Instead,
  // we patch only the two elements that actually change: the active class
  // on the tab buttons and the rendered report HTML. The .tabs container
  // keeps its scroll position, and the page keeps its vertical scroll.
  document.querySelectorAll('.tabs button').forEach(b => {
    b.addEventListener('click', () => {
      const tab = b.dataset.tab;
      if (tab === state.activeTab) return;
      state.activeTab = tab;
      document.querySelectorAll('.tabs button').forEach(x => {
        x.classList.toggle('active', x.dataset.tab === tab);
      });
      const reportEl = document.querySelector('.report');
      if (reportEl) {
        const r = (state.selected?.reports || {})[tab] || '(no content)';
        // md() returns a sanitized HTML string; assigning to innerHTML
        // is safe (sanitize() strips <script>, event handlers, etc.).
        reportEl.innerHTML = md(r);
      }
      // Make sure the newly active tab is visible in the (horizontally
      // scrollable) .tabs row — otherwise tapping a right-edge tab
      // appears to do nothing because the bar scrolls itself back.
      b.scrollIntoView({ block: 'nearest', inline: 'center' });
    });
  });

  // Settings drawer
  $('#drawerMask')?.addEventListener('click', e => {
    if (e.target.id === 'drawerMask') { state.view = 'list'; render(); }
  });
  $('#closeDrawer')?.addEventListener('click', () => { state.view = 'list'; render(); });
  document.querySelectorAll('[data-rm]').forEach(b => {
    b.addEventListener('click', async () => {
      await api.deleteDevice(b.dataset.rm);
      state.devices = await api.listDevices();
      render();
      toast('Device removed');
    });
  });
}

// ----- Polling ------------------------------------------------------------

function startPolling() {
  stopPolling();
  pollHandle = setInterval(refreshTasks, 3000);
}
function stopPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}

async function refreshTasks() {
  try {
    state.tasks = await api.listTasks();
    if (state.view === 'list') render();
  } catch (e) {
    // Session probably expired.
    stopPolling();
    state = { view: 'login', me: null, tasks: [], selected: null, devices: [] };
    render();
  }
}

async function populateModelOptions() {
  const deep = $('#deep_think_llm');
  const quick = $('#quick_think_llm');
  if (!deep || !quick) return;

  // Server returns {name, size}[]; never empty (fallback always included).
  let models = [{ name: 'minimax-m3:cloud', size: '' }];
  let source = 'fallback';
  try {
    const r = await api.listModels();
    if (r && Array.isArray(r.models) && r.models.length) {
      models = r.models;
      source = r.source || 'fallback';
    }
  } catch {
    // keep the fallback
  }

  for (const sel of [deep, quick]) {
    const current = sel.value;
    sel.innerHTML = buildModelOptions(models, source);
    // Preserve prior selection when the model is still in the list.
    const names = models.map(m => m.name);
    if (names.includes(current)) sel.value = current;
  }
}

// Build <option> markup with two groups: "Recommended" (no size, e.g. the
// fallback) and "Local (Ollama)" (with size suffix). If ollama returned
// zero models we still render a flat list so the dropdown is usable.
function buildModelOptions(models, source) {
  const fmt = m => m.size ? `${m.name} · ${m.size}` : m.name;
  const local = models.filter(m => m.size);
  const recommended = models.filter(m => !m.size);
  const groups = [];
  if (recommended.length) {
    groups.push(
      `<optgroup label="Recommended">${recommended
        .map(m => `<option value="${esc(m.name)}">${esc(fmt(m))}</option>`)
        .join('')}</optgroup>`,
    );
  }
  if (local.length) {
    groups.push(
      `<optgroup label="Local (Ollama)">${local
        .map(m => `<option value="${esc(m.name)}">${esc(fmt(m))}</option>`)
        .join('')}</optgroup>`,
    );
  }
  if (!groups.length) {
    return models
      .map(m => `<option value="${esc(m.name)}">${esc(fmt(m))}</option>`)
      .join('');
  }
  return groups.join('');
}

async function openTask(id) {
  state.selected = await api.getTask(id);
  state.activeTab = 'market';
  state.view = 'detail';
  render();
}

async function enterApp() {
  state.me = await api.me();
  state.tasks = await api.listTasks();
  state.view = 'list';
  render();
  startPolling();
}

// ----- Boot ---------------------------------------------------------------

(async function boot() {
  try {
    const me = await api.me();
    if (me) {
      await enterApp();
    } else {
      render();
    }
  } catch {
    render();
  }
})();
