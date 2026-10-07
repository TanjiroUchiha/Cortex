/* ─────────────────────────────────────────────────────────────
   Cortex frontend — API client only.
   Routing, retrieval, merging and verification all run in the
   backend (M1 → domain skills → M2 → V1); this file renders what
   POST /query/stream reports. With no API the UI stays offline
   and no answer is generated.
   ───────────────────────────────────────────────────────────── */

'use strict';

/* ── domain metadata — docs arrive via GET /corpus ───────────── */
const DOMAINS = {
  /* UI metadata only — documents arrive via GET /corpus; nothing is bundled. */
  it:         { title:'IT help',                        color:'var(--it)',         docs:[] },
  hr:         { title:'HR & Admissions',                color:'var(--hr)',         docs:[] },
  fees:       { title:'Fees & Finance',                 color:'var(--fees)',       docs:[] },
  facilities: { title:'Facilities, Security & Transport', color:'var(--facilities)', docs:[] },
  general:    { title:'General, Library & Services',    color:'var(--general)',    docs:[] },
  academics:  { title:'Academics & Research',           color:'var(--academics)',  docs:[] },
};

const SUGGESTIONS = [
  { domain:'it', title:'Get back into your account', detail:'Passwords, access & connectivity', query:'How do I reset my password?' },
  { domain:'fees', title:'Make sense of your fees', detail:'Payments, deadlines & refunds', query:'When is the semester fee deadline?' },
  { domain:'hr', title:'Plan your next day off', detail:'Leave, payroll & people policies', query:'How do I apply for annual leave?' },
  { domain:'facilities', title:'Get your space sorted', detail:'Rooms, repairs & equipment', query:'The projector keeps flickering' },
];
const DOMAIN_QUERIES = { it:'How do I reset my password?', hr:'How do I apply for annual leave?', fees:'When is the semester fee deadline?', facilities:'How do I book a meeting room?', general:'How do I renew a borrowed library book?', academics:'How do I book lab equipment?' };
const STAGES = ['m1','skills','m2','v1'];
const STAGE_DETAILS = ['Find the right knowledge domains','Search each department’s sources','Bring every part into one response','Match citations with retrieved evidence'];

/* ── helpers ─────────────────────────────────────────────────── */
const $ = s => document.querySelector(s);
/* Missing-element-safe wiring: a stale cached page must degrade, not deaden
   every handler — guards keep core controls working if markup lags script. */
const on = (sel, ev, fn, opts) => $(sel)?.addEventListener(ev, fn, opts);
const bind = (sel, prop, fn) => { const el = $(sel); if (el) el[prop] = fn; };
const setText = (sel, text) => { const el = $(sel); if (el) el.textContent = text; };
const BASE_TITLE = document.title;
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uid = () => Math.random().toString(36).slice(2, 10);
const KNOWN_ICONS = new Set(['it','hr','fees','facilities','general','academics']);
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${esc(KNOWN_ICONS.has(name) ? name : 'doc')}"/></svg>`;
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
/* '' = same origin: when the page is served by api.py the backend is already
   here. ?api=http://127.0.0.1:8000 overrides for a separate server. */
const API = (typeof location !== 'undefined' ? new URLSearchParams(location.search).get('api') : null) || '';
const AUTH_KEY = 'cortex.jwt';
const rawFetch = typeof fetch === 'function' ? fetch.bind(window) : null;
/* Every backend call carries the session JWT. A 401 means the token is gone or
   dead — drop it and send the user back to sign in. */
const apiFetch = !rawFetch ? null : async (path, opts = {}) => {
  const headers = { ...(opts.headers || {}) };
  const token = sessionStorage.getItem(AUTH_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await rawFetch(path, { ...opts, headers });
  if (res.status === 401 && !location.pathname.startsWith('/login')) {
    sessionStorage.removeItem(AUTH_KEY);
    location.replace('/login');
  }
  return res;
};
const announce = text => { $('#liveStatus').textContent = text; };
let toastTimer;
function toast(text) {
  clearTimeout(toastTimer);
  $('#toast').textContent = text;
  $('#toast').classList.add('on');
  toastTimer = setTimeout(() => $('#toast').classList.remove('on'), 3500);
}

/* ── state ───────────────────────────────────────────────────── */
const LS_KEY = 'cortex.sessions.v1';
/* Conversation cache is namespaced per account (JWT sub — a cache key only,
   never trusted for auth; the server verifies the token). Without this a
   signed-out user's localStorage rows could flash into the next account's UI
   before /conversations replaces them. Guest tokens get a unique sub per
   issuance, so guest history is ephemeral per session by construction. */
const cacheOwner = () => {
  try {
    const t = sessionStorage.getItem(AUTH_KEY);
    if (!t) return null;
    const p = JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return typeof p.sub === 'string' ? p.sub : null;
  } catch { return null; }
};
const lsKey = () => `${LS_KEY}.${cacheOwner() || 'anon'}`;
const lsActiveKey = () => `cortex.activeSession.${cacheOwner() || 'anon'}`;
const state = {
  sessions: [],            // {id, title, messages:[descriptor], clarifyAttempts, pendingQuery}
  activeId: null,
  available: new Set(Object.keys(DOMAINS)),
  lastTrace: {},           // per-stage JSON for node popovers
  stageStates: {},
  evidence: [],
  overlay: null,
  collapsed: { sidebar: false, inspector: false },
  returnFocus: null,
  docReturn: null,
  corpusMode: null,          // 'doc' | 'list' | 'results'
  searchQ: '',
  historyQ: '',
  conversationEdit: null,
  live: false,               // set by probeBackend() when the API answers /health
  apiModels: null,           // GET /models payload, cached
  user: null,                // /auth/me result — role gates admin-only UI
};

const session = () => state.sessions.find(s => s.id === state.activeId);

function persistLocal() {
  try {
    localStorage.setItem(lsKey(), JSON.stringify(state.sessions));
    localStorage.setItem(lsActiveKey(), state.activeId);
  } catch { /* storage full or blocked — sessions just won't persist */
    if (!state.storageWarning) {
      state.storageWarning = true;
      toast('Browser storage is unavailable. This conversation will not survive a reload.');
    }
  }
}

/* Signed-in accounts keep history server-side (per user_id) — localStorage is
   just the offline cache. Guests stay ephemeral: nothing is persisted at all. */
const syncTimers = new Map();
const remoteHistory = () => !!state.user && state.user.role !== 'guest' && !!apiFetch;
function queueSync(id) {
  if (!remoteHistory()) return;
  const s = state.sessions.find(x => x.id === id);
  if (!s) return;
  clearTimeout(syncTimers.get(id));
  syncTimers.set(id, setTimeout(() => {
    apiFetch(`${API}/conversations/${encodeURIComponent(id)}`, {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ session_id:id, title:s.title, draft:s.draft || '', data:s }),
    }).catch(() => {});
    syncTimers.delete(id);
  }, 600));   // trailing debounce — typing a draft doesn't fire a PUT per keystroke
}
function remoteDelete(id) {
  if (!remoteHistory()) return;
  clearTimeout(syncTimers.get(id));
  apiFetch(`${API}/conversations/${encodeURIComponent(id)}`, { method:'DELETE' }).catch(() => {});
}

/* Server is the source of truth once signed in. LocalStorage is shared across
   accounts on this browser, so it is NEVER pushed up — an empty remote means a
   fresh history, and stale local rows get replaced, not migrated (otherwise a
   previous user's chats would leak into the next account's database). */
async function loadRemoteSessions() {
  if (!remoteHistory()) return;
  try {
    const { sessions } = await apiFetch(`${API}/conversations`).then(r => r.json());
    state.sessions = Array.isArray(sessions)
      ? sessions.filter(s => s && typeof s.id === 'string' && Array.isArray(s.messages))
      : [];
    state.activeId = state.sessions[0]?.id || null;
    persistLocal();
    renderChatList();
    renderMessages();
  } catch { /* offline — localStorage copy stays authoritative until next load */ }
}

function saveSessions(syncId) {
  persistLocal();
  queueSync(syncId || state.activeId);
}
function loadSessions() {
  try {
    const raw = JSON.parse(localStorage.getItem(lsKey()) || '[]');
    if (Array.isArray(raw)) state.sessions = raw.filter(s => s && typeof s.id === 'string' && Array.isArray(s.messages)).map(s => ({ ...s,
      title:typeof s.title === 'string' ? s.title : null,
      draft:typeof s.draft === 'string' ? s.draft.slice(0, 4000) : '',
      updatedAt:Number.isFinite(s.updatedAt) ? s.updatedAt : null,
    }));
    const activeId = localStorage.getItem(lsActiveKey());
    state.activeId = state.sessions.find(s => s.id === activeId)?.id || state.sessions[0]?.id || null;
  } catch { /* corrupted storage — start fresh */ }
}

/* ── pipeline panel ──────────────────────────────────────────── */
function resetPipeline() {
  closePop();
  const idle = $('#inspectorIdle'), content = $('#inspectorContent');
  if (idle) idle.hidden = false;
  if (content) content.hidden = true;
  selectInspector('sources');
  state.lastTrace = {};
  state.stageStates = {};
  STAGES.forEach((name, i) => stage(name, STAGE_DETAILS[i], 'idle'));
  document.querySelectorAll('.pipe-link').forEach(l => l.classList.remove('lit'));
  const runStatus = $('#runStatus'), runTime = $('#runTime'), progress = $('#pipelineProgress');
  if (runStatus) runStatus.textContent = 'Ready when you are';
  if (runTime) runTime.textContent = '—';
  if (progress) progress.style.width = '0%';
  showEvidence([]);
}
function stage(name, detail, cls) {
  const node = document.querySelector(`.pipe-node[data-stage="${name}"]`);
  state.stageStates[name] = { detail, cls };
  if (!node) return;
  node.classList.remove('active','done','fail','idle','waiting','skipped');
  node.classList.add(cls);
  node.querySelector('[data-detail]').textContent = detail;
  node.querySelector('.node-state span').textContent = { idle:'Waiting', active:'In progress', done:'Complete', fail:'Needs attention', waiting:'Your turn', skipped:'Not needed' }[cls];
  if (cls === 'active') {
    const runStatus = $('#runStatus');
    if (runStatus) runStatus.textContent = ['Understanding your question','Searching the knowledge base','Connecting your answers','Checking the evidence'][STAGES.indexOf(name)];
    const words = $('.wl-track');
    if (words) words.style.setProperty('--step', STAGES.indexOf(name));
    announce($('#runStatus')?.textContent || '');
  }
  litLinks();
}
function litLinks() {
  const nodes = [...document.querySelectorAll('.pipe-node')];
  const links = [...document.querySelectorAll('.pipe-link')];
  links.forEach((l,i) => l.classList.toggle('lit', !!nodes[i]?.classList.contains('done')));
  const complete = nodes.filter(n => n.classList.contains('done')).length;
  const progress = $('#pipelineProgress');
  if (progress) progress.style.width = `${complete / STAGES.length * 100}%`;
}
function markHot(domains, confidence) {
  const selected = [...new Set(domains)].filter(d => DOMAINS[d]);
  document.querySelectorAll('.domain-item').forEach(el => {
    el.classList.toggle('hot', selected.includes(el.dataset.domain));
  });
  const ad = $('#activeDomains');
  if (ad) ad.innerHTML = selected.length ? selected.map(d =>
    `<span class="routing-tag" style="--c:${DOMAINS[d].color}">${icon(d)}${esc(DOMAINS[d].title)}</span>`).join('')
    : '<span class="routing-empty">Your question sets the direction.</span>';
  const score = $('#routeConfidence');
  if (score) {
    score.hidden = !Number.isFinite(confidence);
    if (Number.isFinite(confidence)) {
      const tier = confidence >= 0.6 ? 'Strong' : confidence >= 0.3 ? 'Moderate' : 'Partial';
      score.textContent = `Evidence match · ${tier}`;
      score.title = `How much of your question's wording points at the routed department(s) — ${(confidence * 100).toFixed(0)}% of detected signals. This is not a measure of answer correctness.`;
    }
  }
  $('#chatMain')?.style.setProperty('--route-accent', selected.length === 1 ? DOMAINS[selected[0]].color : 'var(--accent)');
}

/* ── evidence panel ──────────────────────────────────────────── */
function showEvidence(skills) {
  state.evidence = skills;
  const list = $('#evidenceList');
  if (!list) return;
  list.innerHTML = '';
  const items = [], seen = new Set();
  for (const s of skills) for (const ev of s.evidence || []) {
    if (!seen.has(ev.doc_id)) { items.push({ domain:s.domain, ...ev }); seen.add(ev.doc_id); }
  }
  const sourceCount = $('#sourceCount');
  if (sourceCount) sourceCount.textContent = items.length;
  if (!items.length) {
    list.innerHTML = `<div class="evidence-empty">${icon('doc')}<strong>No sources to display yet.</strong><p>A clarification or an unsupported question may not have citations. Any supporting documents for this answer will appear here.</p></div>`;
    return;
  }
  for (const ev of items) {
    const domain = DOMAINS[ev.domain];
    if (!domain) continue;
    const el = document.createElement('details');
    el.className = 'ev-item';
    el.id = `ev-${ev.doc_id}`;
    el.style.setProperty('--dc', domain.color);
    const title = domain.docs.find(d => d.id === ev.doc_id)?.title || ev.doc_id;
    el.innerHTML = `<summary><span class="ev-id">${esc(ev.doc_id)}</span><span class="ev-title">${esc(title)}</span></summary>
      <div class="ev-chunk">${esc(ev.chunk)}</div>
      <div class="ev-score">${Number.isFinite(ev.score) ? `Retrieval score ${ev.score} · ` : ''}retrieved document</div>`;
    list.appendChild(el);
  }
}
function selectInspector(tab, focus=false) {
  for (const name of ['activity','sources']) {
    const selected = name === tab;
    const button = $(`#${name}Tab`);
    if (button) {
      button.classList.toggle('on', selected);
      button.setAttribute('aria-selected', selected);
      button.tabIndex = selected ? 0 : -1;
    }
    const panel = $(`#${name}Panel`);
    if (panel) panel.hidden = !selected;
  }
  if (focus) $(`#${tab}Tab`).focus();
  closePop();
}
function restoreInspector(message) {
  resetPipeline();
  markHot(message?.trace?.domains || [], message?.trace?.evidence_share ?? message?.trace?.confidence);
  if (!message) return;
  const idle = $('#inspectorIdle'), content = $('#inspectorContent');
  if (idle) idle.hidden = true;
  if (content) content.hidden = false;
  state.lastTrace = message.pipeline || (message.trace ? { m1:message.trace } : {});
  for (const [name, saved] of Object.entries(message.stages || {})) {
    if (STAGES.includes(name)) stage(name, saved.detail, saved.cls);
  }
  const evidence = message.evidence || Object.entries(DOMAINS).map(([domain, d]) => ({ domain,
    evidence:d.docs.filter(doc => message.citations?.some(c => c.doc_id === doc.id)).map(doc => ({ doc_id:doc.id, chunk:doc.content })) }));
  showEvidence(evidence);
  setText('#runStatus', message.trace?.action === 'clarify' ? 'Waiting for clarification' : 'Result saved');
  setText('#runTime', message.trace?.elapsed != null ? (message.trace.elapsed / 1000).toFixed(1) + 's' : '—');
}

/* ── message descriptors → DOM ───────────────────────────────── */
function formatAnswer(text) {
  const inline = value => esc(value).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`\n]+)`/g, '<code>$1</code>');
  return String(text ?? '').split(/\n\s*\n/).filter(block => block.trim()).map(block => {
    if (/^(If that does not solve it:|Contact:|Help\s?desk:)/i.test(block.trim()))
      return `<p class="answer-contact">${inline(block)}</p>`;
    const lines = block.split('\n');
    let html = '', paragraph = [], list = [], kind = '';
    const flush = () => {
      if (paragraph.length) { html += `<p>${inline(paragraph.join('\n'))}</p>`; paragraph = []; }
      if (list.length) {
        html += kind === 'ol' ? `<ol start="${list[0].number}">` : '<ul>';
        html += list.map(item => `<li${kind === 'ol' ? ` value="${item.number}"` : ''}>${inline(item.text)}</li>`).join('') + `</${kind}>`;
        list = [];
      }
    };
    for (const line of lines) {
      const ordered = line.match(/^\s*(\d{1,5})[.)]\s+(.+)$/);
      const bullet = line.match(/^\s*[-*•]\s+(.+)$/);
      const heading = line.match(/^#{1,4}\s+(.+)$/);
      const type = ordered ? 'ol' : bullet ? 'ul' : '';
      if (heading) { flush(); html += `<p class="answer-heading"><strong>${inline(heading[1])}</strong></p>`; }
      else if (type) {
        if (paragraph.length || (list.length && kind !== type)) flush();
        kind = type;
        list.push({ number:ordered ? Number(ordered[1]) : null, text:ordered ? ordered[2] : bullet[1] });
      } else { if (list.length) flush(); paragraph.push(line); }
    }
    flush();
    return html;
  }).join('');
}
function renderMsg(m) {
  const el = document.createElement('article');
  if (m.role === 'user') {
    el.className = 'msg user';
    el.setAttribute('aria-label', 'Your message');
    el.innerHTML = `<div class="bubble">${esc(m.text)}</div>`;
  } else {
    el.className = 'msg bot';
    el.setAttribute('aria-label', 'Cortex response');
    let html = `<div class="avatar" aria-hidden="true">${icon('cortex')}</div><div class="col"><div class="assistant-label">Cortex</div><div class="bubble">`;
    if (m.handoff) html += '<div class="handoff-tag">HANDOFF · CONTACT THE HELP DESK</div>';
    if (m.badge || m.copyable !== false) {
      html += '<div class="bubble-tools">';
      if (m.badge === 'verified')
        html += `<span class="v-badge ok" title="Citation IDs were matched against the retrieved evidence.">${icon('shield')}Sources checked</span>`;
      else if (m.badge === 'review')
        html += '<span class="v-badge warn">Needs review</span>';
      if (m.copyable !== false) html += `<button class="copy-btn" data-copy aria-label="Copy answer">${icon('copy')}Copy</button>`;
      html += '</div>';
    }
    for (const sec of (m.sections || [])) {
      const domain = DOMAINS[sec.domain];
      html += `<div class="sec"${domain ? ` style="--dc:${domain.color}"` : ''}>`;
      if (domain) html += `<span class="sec-label">${icon(sec.domain)}${esc(domain.title)}</span>`;
      html += `<div class="sec-body">${formatAnswer(sec.text)}</div>`;
      html += '</div>';
    }
    const sourceTitles = [...new Set((m.citations || [])
      .map(c => typeof c.title === 'string' ? c.title.trim() : '')
      .filter(Boolean))];
    if (sourceTitles.length)
      html += `<details class="answer-sources"><summary>Sources used</summary><ul>${sourceTitles.map(title => `<li>${esc(title)}</li>`).join('')}</ul></details>`;
    if (m.options?.length) {
      const latest = session()?.messages.filter(msg => msg.role === 'bot').at(-1);
      const disabled = !session()?.pendingQuery || latest?.id !== m.id;
      html += '<div class="options" role="group" aria-label="Choose a department">' + m.options.filter(d => DOMAINS[d]).map(d =>
        `<button class="opt-chip" data-domain="${d}" style="--c:${DOMAINS[d].color}" ${disabled?'disabled':''}><span class="cdot"></span>${esc(DOMAINS[d].title)}</button>`).join('') + '</div>';
    }
    if (m.retry) html += `<button class="retry-chip" data-retry="${esc(m.retry)}">Try again</button>`;
    if (m.statusLine) html += `<div class="status-line ${['good','warn','bad'].includes(m.statusCls) ? m.statusCls : ''}"><span class="s-dot" style="background:currentColor"></span>${esc(m.statusLine)}</div>`;
    html += '</div>';
    if (m.feedback && state.user?.role !== 'guest') html += `<div class="feedback"><span class="feedback-label">Did this help?</span><button class="fb-btn${m.fbChosen===1?' done-y':''}" data-fb="1" ${m.fbChosen!=null?'disabled':''}>Resolved</button><button class="fb-btn${m.fbChosen===0?' done-n':''}" data-fb="0" ${m.fbChosen!=null?'disabled':''}>Not quite</button></div>`;
    html += '</div>';
    el.innerHTML = html;
    if (m.handoff) el.querySelector('.bubble').classList.add('handoff');
    el.dataset.msgId = m.id;
  }
  $('#messages').appendChild(el);
}

function jsonColor(obj) {
  return JSON.stringify(obj, null, 2).replace(/"(?:\\.|[^"\\])*"(\s*:)?|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]/g, (token, key) => {
    const cls = key ? 'jk' : token.startsWith('"') ? 'js' : /^[{}\[\],:]$/.test(token) ? 'jp' : 'jn';
    return `<span class="${cls}">${esc(token)}</span>`;
  });
}
function codeCard(file, obj) {
  return `<div class="cc-head"><span class="cc-lights" aria-hidden="true"><i class="cc-dot r"></i><i class="cc-dot y"></i><i class="cc-dot g"></i></span><span class="cc-file">${esc(file)}</span></div>` +
         `<pre class="cc-body" tabindex="0">${jsonColor(obj)}</pre>`;
}
function pushMsg(m) {
  const s = session();
  if (m.role === 'bot') {
    m.pipeline = structuredClone(state.lastTrace);
    m.stages = structuredClone(state.stageStates);
    m.evidence = structuredClone(state.evidence);
  }
  s.messages.push(m);
  s.updatedAt = Date.now();
  renderMsg(m);
  renderChatList();
  saveSessions();
  renderDrawer();
  scrollDown(true);   // a new message always lands in view — user + bot alike
  if (m.role === 'bot') announce((m.sections || []).map(sec => sec.text).join(' '));
}
/* Auto-scroll only when already near the bottom (or forced by the user's own
   message / a fresh conversation) so new content never yanks a reader mid-scroll. */
const nearBottom = () => { const m = $('#messages'); return m.scrollHeight - m.scrollTop - m.clientHeight < 120; };
function scrollDown(force) {
  const m = $('#messages');
  if (force || nearBottom()) m.scrollTo({ top:m.scrollHeight, behavior:'instant' });
  syncJump();
}
function syncJump() {
  const btn = $('#jumpLatest');
  const show = !nearBottom();
  btn.classList.toggle('on', show);
  btn.inert = !show;
}

/* ── metrics — every counter comes from GET /metrics; nothing is tallied
   locally. Durable server-side via data/metrics.json. */
async function renderDrawer() {
  const put = (el, entries) => { for (const [k,v] of entries) el.insertAdjacentHTML('beforeend', `<div class="d-counter"><div class="k">${esc(k.replaceAll('_', ' '))}</div><div class="v">${esc(v)}</div></div>`); };
  const c = $('#drawerCounters'), f = $('#drawerFeedback');
  c.innerHTML = ''; f.innerHTML = '';
  if (state.live && apiFetch && state.user?.role === 'admin') {
    try {
      const metrics = await apiFetch(`${API}/metrics`).then(r => r.json());
      const counters = metrics.counters || {}, fb = metrics.feedback || {};
      const answered = (counters.completed||0) + (counters.partial||0) +
                       (counters.needs_review||0) + (counters.demo||0);
      const total = (fb.resolved||0) + (fb.unresolved||0);
      $('#mRequests').textContent = counters.requests ?? 0;
      $('#mRouted').textContent = answered;
      $('#mClarified').textContent = counters.clarify ?? 0;
      $('#mRes').textContent = total ? Math.round(100*fb.resolved/total)+'%' : 'No feedback yet';
      $('#resFill').style.width = total ? (100*fb.resolved/total)+'%' : '0';
      put(c, [['requests', counters.requests ?? 0], ['answered', answered],
              ['clarified', counters.clarify ?? 0], ['handoffs', counters.handoff ?? 0]]);
      put(f, [['resolved', fb.resolved ?? 0], ['unresolved', fb.unresolved ?? 0],
              ['user-reported resolution', fb.resolution_rate != null ? Math.round(fb.resolution_rate*100)+'%' : 'No feedback yet']]);
    } catch { put(c, [['status', 'metrics unavailable']]); }
  } else put(c, [['status', 'offline']]);
  const d = $('#drawerDomains');
  d.innerHTML = '';
  for (const [id, dom] of Object.entries(DOMAINS))
    d.insertAdjacentHTML('beforeend',
      `<div class="d-dom" style="--c:${dom.color}"><span class="cdot"></span>${esc(dom.title)}<span class="cnt">${dom.docs.length} docs</span></div>`);
  refreshSvcPanel();
}

/* Server-side view: which model (or deterministic fallback) powers each stage,
   plus the raw counter snapshot — only when the API is live. */
async function refreshSvcPanel() {
  const panel = $('#svcPanel');
  if (!panel) return;
  if (!state.live || !apiFetch) { panel.hidden = true; return; }
  try {
    const [metrics, models] = await Promise.all([
      apiFetch(`${API}/metrics`).then(r => r.json()),
      state.apiModels ? Promise.resolve(state.apiModels) : apiFetch(`${API}/models`).then(r => r.json()),
    ]);
    state.apiModels = models;
    const c = $('#svcCounters');
    c.innerHTML = '';
    const fb = metrics.feedback || {};
    const total = (fb.resolved || 0) + (fb.unresolved || 0);
    for (const [k, v] of Object.entries({
        requests: metrics.counters?.requests ?? '—',
        clarifications: metrics.counters?.clarify ?? '—',
        resolved: fb.resolved ?? 0,
        unresolved: fb.unresolved ?? 0,
        'user-reported resolution': total ? `${Math.round(100 * fb.resolved / total)}%` : 'No feedback yet' }))
      c.insertAdjacentHTML('beforeend', `<div class="d-counter"><div class="k">${k}</div><div class="v">${v}</div></div>`);
    const m = models.models || {};
    $('#engineList').innerHTML = [
      ['Router', m.m1_router], ['Retrieval', m.retrieval], ['Merger', m.m2_merger], ['Verifier', m.v1_verifier],
    ].map(([k, v]) => `<div class="engine-row"><dt>${k}</dt><dd>${esc(v || '—')}</dd></div>`).join('');
    $('#svcNote').textContent = 'Counters persist across restarts (data/metrics.json).';
    panel.hidden = false;
  } catch { panel.hidden = true; }
}

/* sidebar domain list + composer scope chips — (re)rendered when /domains adds one */
function renderDomainList() {
  const list = $('#domainList');
  if (!list) return;
  list.innerHTML = '';
  for (const [id, d] of Object.entries(DOMAINS)) {
    if (id === 'facilities') continue; // hidden from the sidebar — backend still routes it
    const el = document.createElement('button');
    el.className = 'domain-item';
    el.dataset.domain = id;
    el.style.setProperty('--c', d.color);
    el.setAttribute('aria-label', `Ask ${d.title} · ${d.docs.length} documents`);
    el.innerHTML = `<span class="domain-icon">${icon(id)}</span>${esc(d.title)}<span class="count">${String(d.docs.length).padStart(2,'0')}</span>`;
    el.onclick = () => {
      if (running) return;
      state.available = new Set([id]);
      updateScope();
      closePanel(false);
      if (DOMAIN_QUERIES[id] && !$('#input').value.trim()) $('#input').value = DOMAIN_QUERIES[id];
      updateComposer();
      rememberDraft();
      $('#input').focus();
      toast(`Search scoped to ${d.title}. Edit the question or send it.`);
    };
    list.appendChild(el);
  }
}
function renderFilterChips() {
  const fr = $('#filterRow');
  if (!fr) return;
  fr.innerHTML = '';
  for (const [id, d] of Object.entries(DOMAINS)) {
    const b = document.createElement('button');
    b.className = `f-chip${state.available.has(id) ? ' on' : ''}`;
    b.dataset.domain = id;
    b.style.setProperty('--c', d.color);
    b.setAttribute('aria-pressed', String(state.available.has(id)));
    b.innerHTML = `<span class="cdot"></span>${esc(d.title)}`;
    b.title = `${d.title} — toggle to scope routing`;
    b.onclick = () => {
      if (running) return;
      if (state.available.has(id)) {
        if (state.available.size === 1) { toast('Keep at least one department selected.'); return; }      // keep ≥1 domain
        state.available.delete(id);
      } else { state.available.add(id); }
      updateScope();
    };
    fr.appendChild(b);
  }
}

/* ── sessions / chat list ────────────────────────────────────── */
function newSession(focus=false) {
  if (running) {
    if (focus) toast('Let this response finish before starting a new conversation.');
    return;
  }
  rememberDraft();
  state.historyQ = '';
  const hs = $('#historySearch');
  if (hs) hs.value = '';
  const empty = state.sessions.find(s => !s.messages.length && !s.draft && !s.title);
  const s = empty || { id:uid(), title:null, messages:[], draft:'', updatedAt:Date.now(), clarifyAttempts:0, pendingQuery:null };
  if (!empty) state.sessions.unshift(s);
  switchSession(s.id);
  state.available = new Set(Object.keys(DOMAINS));
  updateScope();
  if (focus) {
    $('#input').focus();
    announce('New conversation ready. Type your question.');
    toast('New conversation ready.');
  }
}
function switchSession(id) {
  if (running || !state.sessions.some(s => s.id === id)) return;
  if (state.activeId !== id) rememberDraft();
  state.activeId = id;
  closePanel();
  const sp = $('#scopePicker');
  if (sp) sp.open = false;
  $('#input').value = session().draft || '';
  const scope = Array.isArray(session().scope) ? session().scope.filter(d => DOMAINS[d]) : [];
  state.available = new Set(scope.length ? scope : Object.keys(DOMAINS));
  updateScope();
  updateComposer();
  renderChatList(); renderMessages();
  restoreInspector(session().messages.filter(m => m.role === 'bot').at(-1));
  saveSessions();
  const convTitle = $('#conversationTitle');
  if (convTitle) convTitle.textContent = session().title || 'A little clarity, for everything.';
}
function rememberDraft() {
  const s = session();
  if (!s || s.draft === $('#input').value) return;
  s.draft = $('#input').value;
  s.updatedAt = Date.now();
  saveSessions();
}
function sessionGroup(s, now = new Date()) {
  if (!Number.isFinite(s.updatedAt)) return 'Earlier';
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime();
  return s.updatedAt >= today ? 'Today' : s.updatedAt >= yesterday ? 'Yesterday' : 'Earlier';
}
function renderChatList() {
  const el = $('#chatList');
  if (!el) return;
  el.innerHTML = '';
  const count = $('#chatCount');
  if (count) count.textContent = state.sessions.filter(s => s.messages.length).length;
  const query = state.historyQ.toLocaleLowerCase();
  const matches = state.sessions.filter(s => !query || [s.title, s.draft,
    ...s.messages.flatMap(m => [m.text, ...(m.sections || []).map(sec => sec.text)])]
    .some(text => typeof text === 'string' && text.toLocaleLowerCase().includes(query)))
    .sort((a,b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  if (!matches.length) {
    el.innerHTML = `<div class="chat-empty">${query ? 'No matching conversations.' : 'Your next conversation starts here.'}</div>`;
    return;
  }
  for (const group of ['Today','Yesterday','Earlier']) {
    const sessions = matches.filter(s => sessionGroup(s) === group);
    if (!sessions.length) continue;
    const heading = document.createElement('h3');
    heading.className = 'history-date';
    heading.textContent = group;
    el.appendChild(heading);
    for (const s of sessions) {
      const row = document.createElement('div');
      row.className = 'chat-row' + (s.id === state.activeId ? ' on' : '');
      const title = s.title || (s.draft ? s.draft.trim().slice(0,54) || 'Draft conversation' : 'New conversation');
      row.innerHTML = `<button class="chat-item${s.id === state.activeId ? ' on' : ''}" data-id="${esc(s.id)}" title="${esc(title)}"${s.id === state.activeId ? ' aria-current="page"' : ''}>${icon('chat')}<span class="ci-t">${esc(title)}</span>${s.draft ? '<span class="draft-tag">Draft</span>' : ''}</button><button class="chat-manage" data-manage="${esc(s.id)}" aria-label="Manage conversation: ${esc(title)}" title="Rename or delete conversation">⋯</button>`;
      row.querySelector('.chat-item').onclick = () => { if (!running) switchSession(s.id); else toast('Let this answer finish before switching conversations.'); };
      row.querySelector('.chat-manage').onclick = e => openConversationSettings(s.id, e.currentTarget);
      el.appendChild(row);
    }
  }
}
function openConversationSettings(id, opener) {
  if (running) { toast('Let this answer finish before managing conversations.'); return; }
  const s = state.sessions.find(s => s.id === id);
  if (!s || !$('#conversationDialog')) return;
  state.conversationEdit = { id, deleting:false, opener };
  $('#conversationDialogTitle').textContent = 'Conversation settings';
  $('#conversationDialogNote').textContent = 'Give this conversation a name you can find later. Changes stay in this browser.';
  $('#conversationNameField').hidden = false;
  $('#conversationName').disabled = false;
  $('#conversationName').value = s.title || '';
  $('#conversationName').setCustomValidity('');
  $('#conversationDelete').hidden = false;
  $('#conversationSave').textContent = 'Save name';
  $('#conversationSave').classList.remove('danger');
  $('#conversationDialog').showModal();
  $('#conversationName').focus();
}
function saveConversationSettings(e) {
  e.preventDefault();
  const edit = state.conversationEdit;
  const s = state.sessions.find(s => s.id === edit?.id);
  if (!s || running || !$('#conversationDialog')) return;
  if (edit.deleting) {
    const active = s.id === state.activeId;
    remoteDelete(s.id);
    state.sessions = state.sessions.filter(item => item.id !== s.id);
    if (active) {
      state.activeId = null;
      $('#input').value = '';
      if (state.sessions.length) switchSession(state.sessions[0].id);
      else newSession();
    }
    toast('Conversation removed from this browser. Backend records are unchanged.');
  } else {
    const name = $('#conversationName').value.trim();
    if (!name) { $('#conversationName').setCustomValidity('Enter a conversation name.'); $('#conversationName').reportValidity(); return; }
    s.title = name;
    s.updatedAt = Date.now();
    if (s.id === state.activeId) $('#conversationTitle').textContent = name;
    toast('Conversation renamed.');
  }
  saveSessions(s.id);
  renderChatList();
  $('#conversationDialog')?.close();
}
function renderMessages() {
  $('#messages').innerHTML = '';
  const s = session();
  if (!s || !s.messages.length) {
    const tpl = $('#welcomeTemplate');
    if (tpl) { $('#messages').appendChild(tpl.content.cloneNode(true)); bindSuggestions(); }
    return;
  }
  for (const m of s.messages) renderMsg(m);
  scrollDown(true);
}

function bindSuggestions() {
  const sug = $('#suggestions');
  if (!sug) return;
  for (const suggestion of SUGGESTIONS) {
    const b = document.createElement('button');
    b.className = 'sug';
    b.style.setProperty('--c', DOMAINS[suggestion.domain].color);
    b.innerHTML = `<span class="sug-icon">${icon(suggestion.domain)}</span><span class="sug-copy"><strong>${esc(suggestion.title)}</strong><small>${esc(suggestion.detail)}</small></span><svg class="icon sug-arrow" aria-hidden="true"><use href="#i-arrow"/></svg>`;
    b.onclick = () => send(suggestion.query);
    sug.appendChild(b);
  }
  $('#demoBtn').onclick = () => {
    state.available = new Set(Object.keys(DOMAINS));
    updateScope();
    send('Reset my VPN password and when is the fee deadline');
  };
}

/* ── run-state helpers (single in-flight query at a time) ──── */
let running = false;
function setRunning(value) {
  running = value;
  document.body.classList.toggle('is-running', value);
  const nc = $('#newChat'), rs = $('#resetScope'), sp = $('#scopePicker'), spSum = $('#scopePicker summary');
  if (nc) nc.disabled = value;
  if (rs) rs.disabled = value;
  if (value && sp) sp.open = false;
  spSum?.setAttribute('aria-disabled', String(value));
  document.querySelectorAll('.f-chip,.domain-item').forEach(b => b.disabled = value);
  $('#messages').setAttribute('aria-busy', value);
  updateComposer();
}
/* ── live pipeline: same UI, driven by POST /query/stream SSE ──
   Backend emits {"event":"stage",stage,state,...} / {"event":"skill",...} /
   error hints, then a final result event identical to the /query body. */
function liveStage(evt) {
  if (evt.event !== 'stage') return;   // per-skill telemetry folds into the skills node
  const names = (evt.domains || []).map(d => DOMAINS[d]?.title || d);
  const S = {
    m1:     { active:['Reading intent and selecting departments…','active'],
              done:[evt.action === 'route' ? `route · ${names.join(' + ')}` : (evt.action || 'done'),'done'],
              fail:['Routing failed','fail'] },
    skills: { active:[(names.length ? names.join(' + ') + ' · ' : '') + 'retrieving…','active'],
              done:[`${evt.answered ?? 0} answered${evt.failed ? ` · ${evt.failed} abstained` : ''}`,'done'],
              fail:['No matching evidence in the knowledge base','fail'] },
    m2:     { active:['Bringing the department answers together…','active'],
              done:[`${evt.citations ?? 0} unique sources`,'done'],
              fail:['Merge unavailable','fail'] },
    v1:     { active:['Matching citations against retrieved evidence…','active'],
              done:[`verdict: ${evt.status || 'done'}`, evt.status === 'failed' ? 'fail' : 'done'],
              fail:['Verification unavailable','fail'] },
  };
  const entry = S[evt.stage]?.[evt.state];
  if (entry) stage(evt.stage, entry[0], entry[1]);
  if (evt.stage === 'm1' && evt.action === 'route') markHot(evt.domains || []);
}

function applyLiveResult(result, query, { s, typing, t0, hints }) {
  const routing = result.routing || {};
  const share = routing.evidence_share ?? routing.confidence;   // new name, legacy payload fallback
  markHot(routing.domains || [], share);
  const skills = result.skills || [];
  const citations = result.citations || [];
  const abstained = (result.errors || []).filter(e => e.code === 'no_evidence').map(e => e.service);
  state.lastTrace.m1 = { action:routing.action, domains:routing.domains || [], options:routing.options || [], evidence_share:share, engine:'live' };
  if (skills.length || abstained.length)
    state.lastTrace.skills = { answered:skills.map(x => x.domain), abstained };
  if (citations.length) state.lastTrace.m2 = { citations:citations.map(c => c.doc_id) };
  if (result.verification && result.verification.status !== 'not_run') state.lastTrace.v1 = result.verification;

  typing.remove();
  if (result.status === 'clarify') {
    const options = (routing.options || []).map(o => o.domain).filter(d => DOMAINS[d]);
    if (options.length) { s.clarifyAttempts++; s.pendingQuery = query; }
    stage('skills', options.length ? 'Choose a department to continue' : 'Tell me what you need', 'waiting');
    pushMsg({ id:uid(), role:'bot', sections:[{ text:result.message }], options,
      statusLine:'A little context will help me find the right source.' });
    setText('#runStatus', 'Waiting for clarification');
    return;
  }
  if (result.status === 'handoff' || result.status === 'unsupported') {
    for (const name of STAGES.slice(1)) stage(name, 'Not needed for this request', 'skipped');
    s.pendingQuery = null;
    const isHandoff = result.status === 'handoff';
    pushMsg({ id:uid(), role:'bot', handoff:isHandoff,
      sections:[{ text:result.message }], statusLine:isHandoff
        ? (result.ticket ? `Local handoff record · ticket ${result.ticket} · contact the help desk to follow up` : 'Suggested next step · contact the help desk.')
        : 'Outside the configured knowledge domains · no answer invented',
      statusCls:'warn', retry:isHandoff ? null : query });
    setText('#runStatus', isHandoff ? 'Human assistance recommended' : 'Outside the knowledge base');
    return;
  }
  if (!skills.length || !['completed','partial','needs_review','unverified','demo','aggregation_unavailable'].includes(result.status)) {
    const noEvidence = result.status === 'no_evidence';
    stage('m2', 'No answer to merge', 'skipped');
    stage('v1', 'No citations to check', 'skipped');
    pushMsg({ id:uid(), role:'bot', sections:[{ text:noEvidence
        ? (result.message || 'I could not find this in the configured knowledge bases. Try adding a little more detail, or contact the relevant department.')
        : (result.message || hints[0] || 'The pipeline could not complete this request.') }],
      statusLine:`${result.status} · ${hints[0] || 'retry available'}`, statusCls:'warn', retry:query });
    setText('#runStatus', noEvidence ? 'No supporting evidence' : 'Something interrupted the response');
    return;
  }
  const rejected = result.verification?.status === 'failed';
  const finalText = result.response || result.draft;
  const sections = rejected
    ? [{ text:'I could not verify this answer against the sources, so I am not presenting it as reliable guidance. Please rephrase your question or contact the relevant department.' }]
    : finalText ? [{ text:finalText }]
    : result.status === 'aggregation_unavailable' ? skills.map(sk => ({ domain:sk.domain, text:sk.answer }))
    : [{ text:'No final answer was returned. Please try again.' }];
  if (abstained.length) sections.push({ text:`No supporting evidence was found for ${abstained.map(d => DOMAINS[d]?.title || d).join(', ')}. That part of your question is still unresolved.` });
  showEvidence(skills);
  const passed = result.verification?.status === 'passed' && Boolean(result.response);
  const elapsed = Number.isFinite(result.elapsed_ms) ? `${(result.elapsed_ms/1000).toFixed(1)}s` : null;
  const mergeNote = result.status === 'aggregation_unavailable'
    ? 'Showing department answers directly · merge step unavailable'
    : null;
  const line = `${skills.length} department${skills.length>1?'s':''} · ${citations.length} sources${elapsed ? ` · ${elapsed}` : ''}${mergeNote ? ` · ${mergeNote}` : ''}${hints.length ? ` · ${hints[0]}` : ''}`;
  pushMsg({ id:uid(), role:'bot', badge:passed && !abstained.length && !mergeNote ? 'verified' : 'review', sections, citations,
    statusLine: line,
    statusCls:passed && !abstained.length && !mergeNote ? 'good' : 'warn', feedback:true, fbChosen:null, req:result.request_id, trace:{ ...(state.lastTrace.m1 || {}) } });
  s.clarifyAttempts = 0;
  s.pendingQuery = null;
  setText('#runStatus', passed && !abstained.length ? 'Complete · sources checked' : 'Complete · some parts need review');
}

async function liveRun(query, forcedDomain, ctx) {
  stage('m1', 'Contacting the Cortex API…', 'active');
  const res = await apiFetch(`${API}/query/stream`, {
    method:'POST', headers:{ 'Content-Type':'application/json' },
    body:JSON.stringify({ query, privacy:'local_only',
      available:state.user?.role === 'guest' ? ['general'] : (forcedDomain ? [forcedDomain] : [...state.available]),
      clarify_attempts:ctx.s.clarifyAttempts }),
  });
  if (!res.ok || !res.body) throw new Error(`API returned ${res.status}`);

  let result = null;
  const hints = [];
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream:true });
    let boundary;
    while ((boundary = /\r?\n\r?\n/.exec(buf))) {
      const frame = buf.slice(0, boundary.index); buf = buf.slice(boundary.index + boundary[0].length);
      let name = '', data = '';
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '');
      }
      if (!data || name === 'done') continue;
      let evt;
      try { evt = JSON.parse(data); } catch { continue; }   // one bad frame must not kill a good answer
      if (name === 'result') result = evt;
      else if (name === 'error') hints.push(evt.hint || `${evt.service}: ${evt.code}`);
      else liveStage(evt);
    }
  }
  if (!result) throw new Error('The stream ended without a result');
  applyLiveResult(result, query, { ...ctx, hints });
}

async function runPipeline(query, forcedDomain=null) {
  const s = session();
  if (!s || running) return;
  setRunning(true);
  resetPipeline();
  const idle = $('#inspectorIdle'), content = $('#inspectorContent');
  if (idle) idle.hidden = true;
  if (content) content.hidden = false;
  markHot([]);
  $('#hero')?.remove();
  document.querySelectorAll('.opt-chip').forEach(b => b.disabled = true);
  pushMsg({ id:uid(), role:'user', text:forcedDomain ? `${DOMAINS[forcedDomain].title} — continue with this department` : query });
  renderChatList();

  const typing = document.createElement('div');
  typing.className = 'msg bot';
  typing.setAttribute('aria-hidden', 'true');
  typing.innerHTML = `<div class="avatar">${icon('cortex')}</div><div class="col"><div class="assistant-label">Cortex</div><div class="bubble"><div class="word-loader"><svg class="icon loader-spark"><use href="#i-spark"/></svg><span class="wl-label">Cortex is</span><span class="wl-words"><span class="wl-track"><span class="wl-word">routing</span><span class="wl-word">retrieving</span><span class="wl-word">merging</span><span class="wl-word">verifying</span></span></span></div><div class="loading-caption">Following the evidence, one step at a time.</div></div></div>`;
  $('#messages').appendChild(typing);
  scrollDown(true);

  const t0 = performance.now();
  const timer = setInterval(() => setText('#runTime', ((performance.now()-t0)/1000).toFixed(1) + 's'), 100);
  try {
    if (!state.live) {
      typing.remove();
      stage('m1', 'Backend unreachable', 'fail');
      setText('#runStatus', 'Backend unreachable');
      pushMsg({ id:uid(), role:'bot',
        sections:[{ text:'Cortex is offline right now — I could not answer that. Please try again in a moment; if it keeps happening, contact the help desk.' }],
        statusLine:'Offline · no answer was generated', statusCls:'warn', retry:query });
      return;
    }
    // one transparent retry covers a dropped stream (e.g. server restart) —
    // liveRun only throws before a result is applied, so this cannot duplicate an answer
    for (let attempt = 0; attempt < 2; attempt++) {
      try { await liveRun(query, forcedDomain, { s, typing, t0 }); break; }
      catch (err) {
        if (attempt === 0) {
          try { await probeBackend(); } catch { /* offline */ }
          if (state.live) continue;
        }
        typing.remove();
        s.pendingQuery = null;
        setText('#runStatus', 'Something interrupted the response');
        pushMsg({ id:uid(), role:'bot', sections:[{ text:'Something interrupted this response. Your conversation is still here; please try again.' }], statusLine:'Interrupted · retry available', statusCls:'warn', retry:query });
        break;
      }
    }
  } catch {
    typing.remove();
    s.pendingQuery = null;
    setText('#runStatus', 'Something interrupted the response');
    pushMsg({ id:uid(), role:'bot', sections:[{ text:'Something interrupted this response. Your conversation is still here; please try again.' }], statusLine:'Interrupted · retry available', statusCls:'warn', retry:query });
  } finally {
    clearInterval(timer);
    typing.remove();
    setText('#runTime', ((performance.now()-t0)/1000).toFixed(1) + 's');
    if (document.hidden) document.title = '\u25CF ' + BASE_TITLE;   // answer landed while the tab was away
    setRunning(false);
    saveSessions();
  }
}

/* ── wire-up ─────────────────────────────────────────────────── */
function updateComposer() {
  const input = $('#input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 132) + 'px';
  const sendBtn = $('#send');
  if (sendBtn) sendBtn.disabled = running || !input.value.trim();
}
function updateScope() {
  document.querySelectorAll('.f-chip').forEach(button => {
    const selected = state.available.has(button.dataset.domain);
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', selected);
  });
  const all = state.available.size === Object.keys(DOMAINS).length;
  const scopeLabel = $('#scopeLabel'), resetScope = $('#resetScope');
  if (scopeLabel) scopeLabel.textContent = all ? 'All departments' : state.available.size === 1 ? DOMAINS[[...state.available][0]].title : `${state.available.size} departments`;
  if (resetScope) resetScope.textContent = all ? 'All selected' : 'Use all departments';
  if (session()) { session().scope = all ? null : [...state.available]; saveSessions(); }
}
/* corpus browser: one folder per domain, fanned files = real docs.
   The folder toggle is a dedicated transparent button (.folder-hit); the
   fanned .file buttons are siblings in .file-stack (inert while closed) so
   interactive elements never nest. Gap/front-flap clicks collapse the folder. */
/* badge for real file formats (pdf/docx/txt); seed/json sources stay unmarked */
const fmtTag = doc => doc.format && !['json', 'md'].includes(doc.format)
  ? `<span class="fmt-tag">${esc(doc.format.toUpperCase())}</span>` : '';
function buildCorpusShelf() {
  const shelf = $('#corpusShelf');
  if (!shelf) return;
  shelf.innerHTML = '';
  Object.entries(DOMAINS).forEach(([id, d]) => {
    const files = d.docs.slice(0, 5);
    const n = files.length;
    const card = document.createElement('div');
    card.className = 'folder-card';
    card.style.setProperty('--c', d.color);
    card.innerHTML =
      `<div class="folder-container">
        <svg class="folder-back" viewBox="0 0 50 40" fill="none" aria-hidden="true"><path d="M0 4C0 1.79 1.79 0 4 0h12.52c1.2 0 2.32.54 3.05 1.47l2.86 3.6c.73.93 1.85 1.46 3.05 1.46H46c2.21 0 4 1.79 4 4V36c0 2.21-1.79 4-4 4H4c-2.21 0-4-1.79-4-4V4Z"/></svg>
        <div class="file-stack" inert>
        ${files.map((doc, i) => `<button type="button" class="file fc-${n - i}" data-doc="${esc(doc.id)}" aria-label="${esc(doc.title)} — open document"><span class="shine"></span><span class="file-text">${esc(doc.title)}</span><span class="file-tag">${esc(doc.id)}</span>${fmtTag(doc)}</button>`).join('')}
        </div>
        <div class="folder-front-wrapper" aria-hidden="true"><svg class="folder-front" viewBox="0 0 50 34" fill="none"><path d="M0 4C0 1.79 1.79 0 4 0h42c2.21 0 4 1.79 4 4v26c0 2.21-1.79 4-4 4H4c-2.21 0-4-1.79-4-4V4Z"/></svg><div class="folder-label"></div>
          <div class="counter"><div class="status-dot"></div><span class="counter-label">docs</span><span class="counter-number">${String(d.docs.length).padStart(2, '0')}</span></div>
        </div>
      </div>
      <button type="button" class="folder-hit" aria-expanded="false" aria-label="${esc(d.title)} folder — ${d.docs.length} documents. Activate to open."></button>
      <span class="folder-name"><span class="fname-icon">${icon(id)}</span>${esc(d.title)}</span>
      ${d.docs.length > n ? `<button type="button" class="folder-more">All ${d.docs.length} documents ›</button>` : ''}`;
    const stack = card.querySelector('.file-stack');
    const hit = card.querySelector('.folder-hit');
    const setOpen = open => {
      card.classList.toggle('open', open);
      hit.setAttribute('aria-expanded', open);
      hit.setAttribute('aria-label', open ? `${d.title} folder — open` : `${d.title} folder — ${d.docs.length} documents. Activate to open.`);
      hit.tabIndex = open ? -1 : 0;
      stack.inert = !open;
      if (open) document.querySelectorAll('.folder-card.open').forEach(other => { if (other !== card) other.collapse(); });
    };
    card.collapse = () => setOpen(false);
    hit.onclick = () => setOpen(true);
    stack.addEventListener('click', e => { if (e.target === stack) setOpen(false); });
    stack.querySelectorAll('.file').forEach((btn, i) => {
      btn.onclick = e => { e.stopPropagation(); openCorpusDoc(id, files[i], btn); };
    });
    card.querySelector('.folder-more')?.addEventListener('click', () => openCorpusDocList(id, card.querySelector('.folder-more')));
    shelf.appendChild(card);
  });
}
/* reader pane inside the corpus modal: single doc, a domain's full doc list,
   or live search results (corpusMode tracks which Back should restore) */
function showCorpusShelf() {
  $('#corpusReader').hidden = true;
  $('#corpusShelf').hidden = false;
  $('#corpusNote').hidden = false;
  state.corpusMode = null;
}
/* structured frontmatter line: department/owner, version status, provenance.
   sensitivity/allowed_* render as descriptive labels only — the backend does
   not enforce them, so the UI never implies they are access controls. */
function docMetaLine(doc) {
  const m = doc.meta || {};
  const bits = [];
  if (m.department) bits.push(`Department: ${m.department}`);
  if (m.original_category && m.original_category !== m.category) bits.push(`filed under ${m.category} (was ${m.original_category})`);
  if (m.version != null) bits.push(`v${m.version}${m.status ? ` · ${m.status}` : ''}`);
  else if (m.status) bits.push(String(m.status));
  if (m.sensitivity) bits.push(`label: ${m.sensitivity}`);
  if (doc.path) bits.push(doc.path);
  return bits.length ? `<p class="doc-meta">${esc(bits.join(' · '))}</p>` : '';
}
function openCorpusReader(fromEl, focusBack = true) {
  $('#corpusShelf').hidden = true;
  $('#corpusReader').hidden = false;
  $('#corpusNote').hidden = true;
  state.docReturn = fromEl;
  if (focusBack) $('#corpusBack').focus();
}
function closeCorpusReader() {
  if (state.corpusMode === 'results') {
    state.searchQ = '';
    $('#corpusSearch').value = '';
    showCorpusShelf();
    $('#corpusSearch').focus();
    return;
  }
  if (state.corpusMode === 'doc' && state.searchQ) {
    renderSearchResults();
    $('#corpusReaderBody .doc-list-item')?.focus();
    return;
  }
  showCorpusShelf();
  state.docReturn?.focus();
  state.docReturn = null;
}
function openCorpusDoc(domain, doc, fromEl) {
  state.corpusMode = 'doc';
  $('#corpusBack').textContent = state.searchQ ? '‹ Back to results' : '‹ Back to folders';
  $('#corpusReaderBody').innerHTML =
    `<span class="doc-dom" style="--c:${DOMAINS[domain].color}">${icon(domain)} ${esc(DOMAINS[domain].title)}</span>
     <h4 class="doc-title">${esc(doc.title)}</h4>
     <span class="file-tag doc-tag">${esc(doc.id)}</span>${fmtTag(doc)}${
       doc.file ? `<a class="doc-dl" href="${API}/corpus/file/${encodeURIComponent(doc.id)}" download>${icon('doc')}Original file</a>` : ''}
     ${docMetaLine(doc)}
     <p class="doc-body">${esc(doc.content)}</p>`;
  openCorpusReader(fromEl);
}
function openCorpusDocList(domain, fromEl) {
  const d = DOMAINS[domain];
  state.corpusMode = 'list';
  $('#corpusBack').textContent = '‹ Back to folders';
  $('#corpusReaderBody').innerHTML =
    `<span class="doc-dom" style="--c:${d.color}">${icon(domain)} ${esc(d.title)}</span>
     <h4 class="doc-title">${d.docs.length} documents</h4>
     <div class="doc-list">${d.docs.map(doc => `<button type="button" class="doc-list-item" data-doc="${esc(doc.id)}"><span>${esc(doc.title)}</span><span class="doc-list-id">${esc(doc.id)}${fmtTag(doc)}</span></button>`).join('')}</div>`;
  $('#corpusReaderBody').querySelectorAll('.doc-list-item').forEach((btn, i) => {
    btn.onclick = () => openCorpusDoc(domain, d.docs[i], btn);
  });
  openCorpusReader(fromEl);
}
/* search: server-side ranked retrieval via GET /corpus/search when signed in —
   the endpoint filters hits to the caller's access tier. The client-side
   substring scan stays as the fallback for guests without a token or when the
   backend is unreachable. */
let searchSeq = 0, searchTimer = null;
function renderSearchResults() {
  state.corpusMode = 'results';
  $('#corpusBack').textContent = '‹ Clear search';
  const q = state.searchQ;
  const seq = ++searchSeq;
  if (apiFetch && state.user) {
    apiFetch(`${API}/corpus/search?q=${encodeURIComponent(q)}`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(data => {
        if (seq !== searchSeq) return;   // a newer keystroke already rendered
        renderSearchHits(q, data.results.map(h => ({
          domId: h.domain,
          doc: (DOMAINS[h.domain]?.docs || []).find(x => x.id === h.doc_id)
               || { id: h.doc_id, title: h.title, content: h.snippet },
          ctx: h.snippet
        })));
      })
      .catch(() => { if (seq === searchSeq) renderLocalSearch(q); });
    return;
  }
  renderLocalSearch(q);
}
function renderLocalSearch(q) {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = [];
  Object.entries(DOMAINS).forEach(([domId, d]) => d.docs.forEach(doc => {
    if (terms.every(t => `${doc.title} ${doc.id} ${doc.content}`.toLowerCase().includes(t))) hits.push({ domId, doc });
  }));
  const excerpt = doc => {
    const lc = doc.content.toLowerCase();
    const at = Math.min(...terms.map(t => lc.indexOf(t)).filter(i => i >= 0));
    if (!isFinite(at)) return '';
    const s = Math.max(0, at - 40), e = Math.min(doc.content.length, at + 80);
    return (s ? '…' : '') + doc.content.slice(s, e) + (e < doc.content.length ? '…' : '');
  };
  renderSearchHits(q, hits.map(h => ({ ...h, ctx: excerpt(h.doc) })));
}
function renderSearchHits(q, hits) {
  $('#corpusReaderBody').innerHTML = hits.length
    ? `<span class="doc-dom" style="--c:var(--accent)">${icon('search')} Search</span>
       <h4 class="doc-title">${hits.length} document${hits.length > 1 ? 's' : ''} match “${esc(q)}”</h4>
       <div class="doc-list">${hits.map(h =>
         `<button type="button" class="doc-list-item hit-item" style="--c:${DOMAINS[h.domId].color}"><span class="cdot"></span><span class="hit-copy"><span class="hit-title">${esc(h.doc.title)}</span><span class="hit-ctx">${esc(h.ctx)}</span></span><span class="doc-list-id">${esc(h.doc.id)}</span></button>`).join('')}</div>`
    : `<span class="doc-dom" style="--c:var(--accent)">${icon('search')} Search</span>
       <h4 class="doc-title">No matches for “${esc(q)}”</h4>
       <p class="doc-body">Titles, document ids and full text are all searched — try different words.</p>`;
  $('#corpusReaderBody').querySelectorAll('.doc-list-item').forEach((btn, i) => {
    btn.onclick = () => openCorpusDoc(hits[i].domId, hits[i].doc, btn);
  });
  openCorpusReader(null, false);
}
function closeCorpusFolder() {
  const open = $('#corpusModal .folder-card.open');
  if (open) { open.collapse(); open.querySelector('.folder-hit').focus(); return true; }
  return false;
}
function syncPanels() {
  for (const [id, narrow] of [['sidebar', window.innerWidth < 920], ['pipelinePanel', window.innerWidth < 1200]]) {
    const panel = $(`#${id}`);
    if (!panel) continue;
    const open = state.overlay === id;
    const key = id === 'sidebar' ? 'sidebar' : 'inspector';
    const collapsed = !narrow && state.collapsed[key];
    panel.inert = (narrow && !open) || (!!state.overlay && !open) || collapsed;
    panel.setAttribute('aria-hidden', panel.inert);
    panel.classList.toggle('collapsed', collapsed);
  }
  const app = $('.app');
  if (app) {
    app.classList.toggle('hide-left', window.innerWidth >= 920 && state.collapsed.sidebar);
    app.classList.toggle('hide-right', window.innerWidth >= 1200 && state.collapsed.inspector);
  }
  $('#chatMain').inert = !!state.overlay;
  $('#metricsDrawer').inert = state.overlay !== 'metricsDrawer';
  const corpus = $('#corpusModal');
  corpus.inert = state.overlay !== 'corpusModal';
  corpus.setAttribute('aria-hidden', corpus.inert);
  syncToggles();
}
function syncToggles() {
  const menu = $('#menuBtn'), insp = $('#inspectorBtn');
  if (menu) {
    const expanded = window.innerWidth < 920 ? state.overlay === 'sidebar' : !state.collapsed.sidebar;
    menu.setAttribute('aria-expanded', expanded);
    menu.setAttribute('aria-label', expanded ? 'Hide navigation' : 'Show navigation');
  }
  if (insp) {
    const expanded = window.innerWidth < 1200 ? state.overlay === 'pipelinePanel' : !state.collapsed.inspector;
    insp.setAttribute('aria-expanded', expanded);
    insp.setAttribute('aria-label', expanded ? 'Hide sources and activity' : 'Show sources and activity');
  }
}
function toggleCollapse(key) {
  state.collapsed[key] = !state.collapsed[key];
  try { localStorage.setItem('cortex.panels.v1', JSON.stringify(state.collapsed)); } catch {}
  syncPanels();
  if (state.collapsed[key]) $('#input')?.focus();
}
function openPanel(id) {
  const previous = document.activeElement;
  closePanel(false);
  state.overlay = id;
  state.returnFocus = previous;
  const panel = $(`#${id}`);
  panel.classList.add(id === 'metricsDrawer' ? 'on' : 'panel-open');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-hidden', 'false');
  $('#drawerScrim').classList.add('on');
  syncPanels();
  panel.querySelector('button:not(:disabled),select:not(:disabled)')?.focus();
}
function closePanel(restore=true) {
  closePop();
  if (state.overlay) {
    const panel = $(`#${state.overlay}`);
    panel.classList.remove('on', 'panel-open');
    if (state.overlay !== 'metricsDrawer') {
      panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
    }
  }
  state.overlay = null;
  $('#drawerScrim').classList.remove('on');
  $('#metricsDrawer').setAttribute('aria-hidden', 'true');
  syncPanels();
  if (restore && state.returnFocus?.isConnected) state.returnFocus.focus();
  state.returnFocus = null;
}
function send(text) {
  const q = (text ?? $('#input').value).trim();
  if (!q || running) return;
  $('#input').value = '';
  updateComposer();
  const s = session();
  if (!s) { newSession(); }
  const cur = session();
  cur.draft = '';
  if (!cur.title) { cur.title = q.length > 54 ? q.slice(0,54) + '…' : q; renderChatList(); }
  const convTitle = $('#conversationTitle');
  if (convTitle) convTitle.textContent = cur.title;
  if (text) cur.pendingQuery = null;   // fresh suggestion/typed query clears clarify state
  if (!cur.pendingQuery) cur.clarifyAttempts = 0;
  runPipeline(q);
}

/* popover for pipeline nodes */
function openPop(stageName, anchorEl) {
  const pop = $('#nodePop');
  const wasOpen = pop.classList.contains('on') && state.popAnchor === anchorEl;
  closePop();
  if (wasOpen) return;
  state.popAnchor = anchorEl;
  const data = state.lastTrace[stageName];
  $('#nodePopFile').textContent = stageName + '.json';
  $('#nodePopBody').innerHTML = jsonColor({ stage:stageName, ...(data || { status:state.stageStates[stageName]?.cls || 'idle', message:'No output recorded for this stage yet.' }) });
  pop.classList.add('on');
  pop.setAttribute('aria-hidden', 'false');
  anchorEl.setAttribute('aria-expanded', 'true');
  const r = anchorEl.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  const left = r.left - pw - 12 >= 12 ? r.left - pw - 12 : Math.min(r.left, window.innerWidth - pw - 12);
  pop.style.left = Math.max(12, left) + 'px';
  pop.style.top = Math.max(12, Math.min(window.innerHeight - ph - 12, r.top)) + 'px';
  $('#popClose').focus();
}
function closePop(restore=false) {
  const pop = $('#nodePop');
  pop.classList.remove('on');
  pop.setAttribute('aria-hidden', 'true');
  if (state.popAnchor) {
    state.popAnchor.setAttribute('aria-expanded', 'false');
    if (restore && state.popAnchor.isConnected) state.popAnchor.focus();
    state.popAnchor = null;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  /* auth: no token, no app — the backend rejects calls anyway */
  if (!sessionStorage.getItem(AUTH_KEY)) { location.replace('/login'); return; }
  /* theme */
  const themeToggle = $('#themeBtn');
  const systemTheme = () => matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light';
  const savedTheme = () => { try { const t = localStorage.getItem('cortex.theme'); return (t === 'dark' || t === 'light') ? t : null; } catch { return 'light'; } };
  const applyTheme = theme => {
    document.documentElement.dataset.theme = theme;
    themeToggle.setAttribute('aria-checked', String(theme === 'dark'));
    themeToggle.setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
    $('meta[name="theme-color"]').content = theme === 'dark' ? '#0b0d14' : '#f7f8fa';
  };
  applyTheme(savedTheme() || systemTheme());
  matchMedia('(prefers-color-scheme:dark)').addEventListener('change', e => {
    if (!savedTheme()) applyTheme(e.matches ? 'dark' : 'light');   // follows the OS only while the user hasn't chosen
  });
  themeToggle.onclick = () => {
    const next = themeToggle.getAttribute('aria-checked') === 'true' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('cortex.theme', next); }
    catch { toast('Theme changed for this visit. Browser storage is unavailable.'); }
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) document.title = BASE_TITLE; });

  renderDomainList();
  renderFilterChips();
  bind('#resetScope', 'onclick', () => { state.available = new Set(Object.keys(DOMAINS)); updateScope(); });
  on('#scopePicker summary', 'click', e => { if (running) e.preventDefault(); });
  document.addEventListener('click', e => { const sp = $('#scopePicker'); if (sp && !e.target.closest('#scopePicker')) sp.open = false; });

  /* sessions */
  loadSessions();
  if (!state.sessions.length) newSession();
  else switchSession(state.activeId);
  bind('#newChat', 'onclick', () => newSession(true));
  on('#historySearch', 'input', e => { state.historyQ = e.target.value.trim(); renderChatList(); });
  bind('#conversationForm', 'onsubmit', saveConversationSettings);
  bind('#conversationName', 'oninput', () => $('#conversationName')?.setCustomValidity(''));
  const closeConversation = () => $('#conversationDialog')?.close();
  bind('#conversationClose', 'onclick', closeConversation);
  bind('#conversationCancel', 'onclick', closeConversation);
  bind('#conversationDelete', 'onclick', () => {
    state.conversationEdit.deleting = true;
    const s = state.sessions.find(s => s.id === state.conversationEdit.id);
    $('#conversationDialogTitle').textContent = 'Delete this conversation?';
    $('#conversationDialogNote').textContent = `“${s?.title || 'Untitled conversation'}” and its draft will be removed from this browser. This cannot be undone. Backend query logs and handoff records are not deleted.`;
    $('#conversationNameField').hidden = true;
    $('#conversationName').disabled = true;
    $('#conversationDelete').hidden = true;
    $('#conversationSave').textContent = 'Delete conversation';
    $('#conversationSave').classList.add('danger');
    $('#conversationCancel').focus();
  });
  on('#conversationDialog', 'close', () => {
    const opener = state.conversationEdit?.opener;
    state.conversationEdit = null;
    const target = opener?.isConnected ? opener : state.overlay === 'sidebar' ? $('#historySearch') : $('#input');
    target?.focus();
  });
  window.addEventListener('pagehide', rememberDraft);

  /* drawer */
  bind('#metricsBtn', 'onclick', () => { renderDrawer(); openPanel('metricsDrawer'); });
  bind('#drawerClose', 'onclick', () => closePanel());
  bind('#drawerScrim', 'onclick', () => closePanel());
  bind('#menuBtn', 'onclick', () => { window.innerWidth < 920 ? openPanel('sidebar') : toggleCollapse('sidebar'); });
  bind('#inspectorBtn', 'onclick', () => { window.innerWidth < 1200 ? openPanel('pipelinePanel') : toggleCollapse('inspector'); });
  buildCorpusShelf();
  const openCorpus = () => {
    state.searchQ = '';
    const cs = $('#corpusSearch'), cb = $('#corpusBack');
    if (cs) cs.value = '';
    if (cb) cb.textContent = '‹ Back to folders';
    showCorpusShelf();
    document.querySelectorAll('#corpusModal .folder-card.open').forEach(c => c.collapse());
    openPanel('corpusModal');
  };
  on('#corpusSearch', 'input', e => {
    state.searchQ = e.target.value.trim();
    clearTimeout(searchTimer);
    if (!state.searchQ) { showCorpusShelf(); return; }
    // server-ranked search debounces per keystroke; the local fallback stays instant
    searchTimer = setTimeout(renderSearchResults, apiFetch && state.user ? 220 : 0);
  });
  bind('#browseCorpus', 'onclick', openCorpus);
  bind('#browseCorpus2', 'onclick', openCorpus);
  bind('#corpusClose', 'onclick', () => closePanel());
  bind('#corpusBack', 'onclick', () => closeCorpusReader());
  document.querySelectorAll('[data-close-panel]').forEach(b => b.onclick = () => closePanel());
  window.addEventListener('resize', () => { closePop(); if (state.overlay && state.overlay !== 'metricsDrawer') closePanel(); syncPanels(); syncJump(); });

  /* pipeline node popovers */
  document.querySelectorAll('.pipe-node').forEach(n => {
    n.addEventListener('click', e => { e.stopPropagation(); openPop(n.dataset.stage, n); });
  });
  bind('#popClose', 'onclick', () => closePop(true));
  document.addEventListener('click', e => { if (!e.target.closest('.node-pop,.pipe-node')) closePop(); });
  bind('#activityTab', 'onclick', () => selectInspector('activity'));
  bind('#sourcesTab', 'onclick', () => selectInspector('sources'));
  on('.inspector-tabs', 'keydown', e => {
    if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {
      e.preventDefault();
      const tab = e.key === 'Home' ? 'sources' : e.key === 'End' ? 'activity' : $('#activityTab').getAttribute('aria-selected') === 'true' ? 'sources' : 'activity';
      selectInspector(tab, true);
    }
  });

  /* jump-to-latest pill + scroll tracking */
  on('#messages', 'scroll', () => syncJump(), { passive: true });
  bind('#jumpLatest', 'onclick', () => {
    const m = $('#messages');
    m.scrollTo({ top: m.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth' });
    const jl = $('#jumpLatest');
    if (jl) { jl.classList.remove('on'); jl.inert = true; }
  });

  /* composer */
  bind('#send', 'onclick', () => { send(); $('#input').focus(); });
  on('#input', 'input', () => { updateComposer(); rememberDraft(); renderChatList(); });
  on('#input', 'keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      if (!e.repeat) send();
    }
  });
  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || e.isComposing || e.keyCode === 229 || $('#conversationDialog')?.open) return;
    if (e.key === 'Escape') {
      if ($('#nodePop')?.classList.contains('on')) closePop(true);
      else if (state.overlay === 'corpusModal' && !$('#corpusReader')?.hidden) closeCorpusReader();
      else if (state.overlay === 'corpusModal' && closeCorpusFolder()) {}
      else if (state.overlay) closePanel();
      else if ($('#scopePicker')?.open) { $('#scopePicker').open = false; $('#scopePicker summary')?.focus(); }
      else if (document.activeElement === $('#input')) { $('#input').value = ''; updateComposer(); rememberDraft(); renderChatList(); }
      return;
    }
    if (state.overlay && e.key === 'Tab') {
      const root = $(`#${state.overlay}`);
      if (!root) return;
      const elements = [...root.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),summary,[tabindex="0"]'), ...($('#nodePop')?.querySelectorAll('button,[tabindex="0"]') || [])].filter(el => el.getClientRects().length && !el.closest('[hidden],[inert]'));
      const first = elements[0], last = elements.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      return;
    }
    if (state.overlay) return;
    const active = document.activeElement;
    const editing = active?.matches('input,textarea,select') || active?.isContentEditable;
    if ((!editing && !e.ctrlKey && !e.metaKey && !e.altKey && e.key === '/') || ((e.ctrlKey || e.metaKey) && !e.altKey && e.code === 'KeyK')) { e.preventDefault(); $('#input').focus(); }
    if (!e.repeat && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.code === 'KeyN' && (e.altKey || !editing)) {
      e.preventDefault();
      newSession(true);
    }
  });

  /* message interaction delegation */
  on('#messages', 'click', async e => {
    const opt = e.target.closest('.opt-chip');
    if (opt) {
      const s = session();
      if (!running && !opt.disabled && s?.pendingQuery) {
        const q = s.pendingQuery;
        s.pendingQuery = null;
        runPipeline(q, opt.dataset.domain);
      }
      return;
    }
    const retry = e.target.closest('.retry-chip');
    if (retry) { (async () => { if (!state.live) await probeBackend(); send(retry.dataset.retry); })(); return; }
    const copyBtn = e.target.closest('.copy-btn');
    if (copyBtn) {
      const bubble = copyBtn.closest('.bubble');
      const message = session()?.messages.find(m => m.id === bubble.closest('.msg').dataset.msgId);
      const text = message ? (message.sections || []).map(s => s.text).join('\n\n') : [...bubble.querySelectorAll('.sec-body')].map(s => s.innerText).join('\n\n');
      try {
        if (!navigator.clipboard) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(text);
        copyBtn.innerHTML = `${icon('check')}Copied`;
        copyBtn.classList.add('copied');
        toast('Answer copied to clipboard.');
        setTimeout(() => { copyBtn.innerHTML = `${icon('copy')}Copy`; copyBtn.classList.remove('copied'); }, 1800);
      } catch { toast('Clipboard is unavailable. Select the answer text to copy it.'); }
      return;
    }
    const rel = e.target.closest('.rel-chip');
    if (rel) {
      const doc = (DOMAINS[rel.dataset.relDomain]?.docs || []).find(d => d.id === rel.dataset.relDoc);
      if (doc) {
        state.searchQ = '';
        const cs = $('#corpusSearch');
        if (cs) cs.value = '';
        document.querySelectorAll('#corpusModal .folder-card.open').forEach(c => c.collapse());
        openPanel('corpusModal');
        openCorpusDoc(rel.dataset.relDomain, doc, rel);
      }
      return;
    }
    const cite = e.target.closest('.cite');
    if (cite) {
      if (running) { toast('Let this response finish before inspecting an earlier answer.'); return; }
      const message = session().messages.find(m => m.id === cite.closest('.msg').dataset.msgId);
      restoreInspector(message);
      selectInspector('sources');
      if (window.innerWidth < 1200) openPanel('pipelinePanel');
      else if (state.collapsed.inspector) toggleCollapse('inspector');
      const ev = document.getElementById('ev-' + cite.dataset.doc);
      if (ev) {
        ev.open = true;
        ev.scrollIntoView({ behavior:reducedMotion() ? 'auto' : 'smooth', block:'nearest' });
        ev.querySelector('summary').focus({ preventScroll:true });
        ev.classList.remove('flash'); void ev.offsetWidth; ev.classList.add('flash');
        announce('Source opened: ' + ev.querySelector('.ev-title').textContent);
      }
      return;
    }
    const fb = e.target.closest('.fb-btn');
    if (fb && !fb.disabled) {
      const resolved = fb.dataset.fb === '1';
      fb.classList.add(resolved ? 'done-y' : 'done-n');
      fb.parentElement.querySelectorAll('.fb-btn').forEach(b => b.disabled = true);
      const msgEl = fb.closest('.msg');
      const s = session();
      const desc = s?.messages.find(m => m.id === msgEl?.dataset.msgId);
      if (desc) { desc.fbChosen = resolved ? 1 : 0; saveSessions(); }
      if (state.live && desc?.req && apiFetch)   // report resolution to the real pipeline
        apiFetch(`${API}/feedback`, { method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ request_id:desc.req, resolved }) }).catch(() => {});
      renderDrawer();
      toast(resolved ? 'Thanks. Marked as resolved in your metrics.' : 'Thanks. Marked as unresolved in your metrics.');
    }
  });
  on('#messages', 'keydown', e => {
    const option = e.target.closest('.opt-chip');
    if (!option || !['ArrowLeft','ArrowRight'].includes(e.key)) return;
    const options = [...option.parentElement.querySelectorAll('button:not(:disabled)')];
    const index = options.indexOf(option);
    e.preventDefault();
    options[(index + (e.key === 'ArrowRight' ? 1 : -1) + options.length) % options.length]?.focus();
  });

  try { const p = JSON.parse(localStorage.getItem('cortex.panels.v1') || 'null');
    if (p) { state.collapsed.sidebar = !!p.sidebar; state.collapsed.inspector = !!p.inspector; } } catch {}
  renderDrawer();
  updateComposer();
  syncPanels();
  apiFetch(`${API}/auth/me`).then(async r => {
    if (!r.ok) return;
    state.user = await r.json();
    const display = state.user.name || state.user.email || 'Account';
    setText('#userBadge', display);
    setText('#userMenuName', display);
    setText('#userMenuEmail', state.user.email || '');
    setText('#userMenuRole', state.user.role);
    const av = $('#userAvatar');
    if (av) av.textContent = display.trim()[0] || '?';
    const admin = state.user.role === 'admin';
    if (!admin) { const mb = $('#metricsBtn'); if (mb) mb.hidden = true; }
    if (admin) initCorpusUpload();
    if (state.user.role === 'guest') { const sp = $('#scopePicker'); if (sp) sp.hidden = true; }
    loadRemoteSessions();
  }).catch(() => {});
  /* account chip menu */
  const userChip = $('#userChip'), userMenu = $('#userMenu');
  if (userChip && userMenu) {
    const setMenu = open => { userMenu.hidden = !open; userChip.setAttribute('aria-expanded', String(open)); };
    userChip.addEventListener('click', e => { e.stopPropagation(); setMenu(userMenu.hidden); });
    document.addEventListener('click', e => { if (!userMenu.hidden && !e.target.closest('.user-wrap')) setMenu(false); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !userMenu.hidden) { setMenu(false); userChip.focus(); } });
  }
  $('#logoutBtn')?.addEventListener('click', () => {
    sessionStorage.removeItem(AUTH_KEY);
    location.replace('/login');
  });
  probeBackend().then(() => {
    /* /app?q=... deep link — a question shared into the assistant (landing-page
       tickers use it) runs once the backend is confirmed live; offline it just
       lands in the composer instead of erroring. */
    const params = new URLSearchParams(location.search);
    if (params.get('panel') === 'knowledge') openCorpus();
    const q = params.get('q');
    if (!q?.trim()) return;
    if (session()?.messages.length || session()?.draft) newSession();
    params.delete('q');
    history.replaceState(null, '', location.pathname + (params.size ? '?' + params.toString() : '') + location.hash);
    if (state.live) send(q); else { $('#input').value = q.slice(0,4000); updateComposer(); rememberDraft(); renderChatList(); }
  });
});

/* The backend is the only engine — when it answers /health the UI goes live;
   otherwise it stays offline and says so instead of fabricating answers. */
async function probeBackend() {
  try {
    if (!apiFetch) throw new Error('no api');
    const r = await apiFetch(`${API}/health`);
    if (!r.ok) throw new Error(r.status);
    await r.json();
    state.live = true;
    await syncDomains();
    await loadCorpus();
    announce('Connected to the live backend.');
  } catch {
    state.live = false;
    toast('Cortex is offline — answers will resume when the service is reachable.');
  }
}

/* Pull the backend's domain registry — keeps titles in sync and surfaces any
   domain the UI doesn't know yet. */
async function syncDomains() {
  try {
    const { domains } = await apiFetch(`${API}/domains`).then(r => r.json());
    let added = 0;
    for (const d of domains || []) {
      if (!d?.id) continue;
      if (DOMAINS[d.id]) DOMAINS[d.id].title = d.title || DOMAINS[d.id].title;
      else {
        DOMAINS[d.id] = { title:d.title || d.id, color:'#9aa0aa', docs:[] };
        state.available.add(d.id);
        added++;
      }
    }
    const count = $('.label-count');
    // Facilities stays routable in the backend but is hidden from the sidebar list.
    if (count) count.textContent = String(Object.keys(DOMAINS).filter(id => id !== 'facilities').length).padStart(2, '0');
    if (added) { renderDomainList(); renderFilterChips(); updateScope(); }
  } catch { /* registry unavailable — UI keeps its built-in domain titles */ }
}

/* The corpus browser's data — the backend's real knowledge base via GET /corpus.
   In live mode (remote services, no local corpus) this 503s and the shelf just
   shows empty folders. */
async function loadCorpus() {
  try {
    const { domains } = await apiFetch(`${API}/corpus`).then(r => r.json());
    for (const [id, body] of Object.entries(domains || {})) {
      const meta = DOMAINS[id] || (DOMAINS[id] = { title:body.title || id, color:'#9aa0aa', docs:[] });
      meta.title = body.title || meta.title;
      meta.docs = (body.sources || []).map(s => ({ id:s.id, title:s.title, content:s.content, format:s.format, file:!!s.file, meta:s.meta || {}, path:s.path || '' }));
    }
    buildCorpusShelf(); renderDomainList(); renderDrawer();
  } catch { /* no local corpus — shelf shows empty folders */ }
}

/* Corpus upload: admin-only on the backend — only shown here for admins too. */
function initCorpusUpload() {
  const box = $('#corpusUpload');
  if (!box || state.user?.role !== 'admin') return;
  const sel = $('#uploadDomain');
  sel.innerHTML = Object.entries(DOMAINS).map(([id, d]) => `<option value="${esc(id)}">${esc(d.title)}</option>`).join('');
  box.hidden = false;
  const input = $('#uploadFile'), drop = $('#uploadDrop'), hint = $('#uploadHint');
  const say = t => { hint.textContent = t; };
  const send = async file => {
    if (!file) return;
    if (!/\.(md|txt|pdf|docx)$/i.test(file.name)) { say('Only .md, .txt, .pdf or .docx'); return; }
    const domain = sel.value;
    say(`Indexing ${file.name}…`);
    const fd = new FormData();
    fd.append('domain', domain); fd.append('file', file);
    try {
      const res = await apiFetch(`${API}/corpus/upload`, { method:'POST', body:fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { say(data.detail || `Upload failed (${res.status})`); return; }
      const text = /\.(md|txt)$/i.test(file.name) ? await file.text() : '';
      DOMAINS[domain].docs.push({ id:data.added, format:data.format || file.name.split('.').pop().toLowerCase(),
        file:!!data.file,
        title:file.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' '),
        content:text || `Uploaded to the knowledge base as ${data.added} — ${data.chunks} searchable chunks. The original file holds the full text.` });
      buildCorpusShelf(); renderDomainList(); renderDrawer();
      say(`${data.chunks} chunks indexed — ready to answer from it`);
      toast(`Added "${file.name}" to ${DOMAINS[domain].title}.`);
    } catch { say('Upload failed — is the backend still running?'); }
  };
  input.onchange = () => { send(input.files[0]); input.value = ''; };
  drop.onclick = () => input.click();
  drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); send(e.dataTransfer.files[0]); };
}
