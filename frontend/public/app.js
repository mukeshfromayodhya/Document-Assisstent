// Document Assistant UI. Plain browser JavaScript, no build step.
const $ = (s, r = document) => r.querySelector(s);
const STEPS = ['Upload accepted', 'Text extracted, OCR where needed', 'Cleaned and chunked by page', 'Generating embeddings', 'Storing vectors', 'Warming up search'];
const STAGE = { queued: 0, extracting: 1, structuring: 2, chunking: 2, embedding: 3, storing: 4, optimizing: 5, finalizing: 5 };
const CHECK = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M2 6.5l2.5 2.5L10 3.5" stroke="#06201C" stroke-width="2" stroke-linecap="round"/></svg>';
const SPARK = '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="M9 2l1.8 4.7L16 8l-4.2 2.3L9 16l-1.8-5.7L2 8l5.2-1.3z" fill="#35D0BA"/></svg>';
const newId = () => 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const st = { session: { authenticated: false, email: null }, chatId: newId(), createdAt: Date.now(), doc: null, msgs: [], chats: [], busy: false, poll: 0, lastQ: '', mode: 'login' };

/* ---------- helpers ---------- */
async function api(path, opts = {}) {
  let res;
  try { res = await fetch('/api' + path, { credentials: 'same-origin', ...opts }); }
  catch { throw Object.assign(new Error('Cannot reach the frontend server. Is it running?'), { status: 0 }); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON body */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join(' ') : `Request failed (${res.status}).`;
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return data;
}
const send = (path, body, method = 'POST') => api(path, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
function toast(msg, bad = false) {
  const t = el('div', 'toast' + (bad ? ' bad' : ''), msg);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), 5500);
}
function fmt(text, sources) {
  const blocks = esc(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').split(/\n{2,}/).map((b) => {
    const lines = b.split('\n');
    if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) return '<ul>' + lines.map((l) => '<li>' + l.replace(/^\s*[-*•]\s+/, '') + '</li>').join('') + '</ul>';
    return '<p>' + lines.join('<br>') + '</p>';
  }).join('');
  return blocks.replace(/\[(S\d+)\]/g, (m, id) => {
    const s = sources.find((x) => x.citation === m);
    return s ? chipHtml(s, id) : m;
  });
}
const LINK = '<svg viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M5 7l2-2M4.6 3.4l.9-.9a2 2 0 013 3l-.9.9M7.4 8.6l-.9.9a2 2 0 01-3-3l.9-.9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>';
const chipHtml = (s, label) => `<button class="chip" type="button" data-chunk="${esc(s.chunk_id)}" aria-label="Open source ${esc(label)}, page ${esc(s.page_number)}">${LINK}${esc(label)} · p.${esc(s.page_number)}</button>`;

/* ---------- views ---------- */
function showView(name) {
  $('#uploadView').hidden = name !== 'upload';
  $('#chatView').hidden = name !== 'chat';
  const d = st.doc;
  $('#docChip').hidden = !d || name !== 'chat';
  $('#srcBtn').hidden = name !== 'chat';
  if (name !== 'chat') setSrc(false);
  if (d) { $('#docName').textContent = d.filename; $('#docMeta').textContent = (d.page_count ? d.page_count + ' pages · ' : '') + 'Indexed and ready'; }
  if (name === 'chat') $('#q').focus();
}
const overlay = () => matchMedia('(max-width:1180px)').matches;
function syncScrim() { $('#scrim').hidden = !($('#side').classList.contains('open') || ($('#src').classList.contains('open') && overlay())); }
function markChips(id) { st.openChunk = id; document.querySelectorAll('[data-chunk]').forEach((c) => c.classList.toggle('on', id != null && c.dataset.chunk === String(id))); }
function setSrc(open) {
  $('#app').classList.toggle('src-open', open); $('#src').classList.toggle('open', open);
  $('#srcBtn').setAttribute('aria-expanded', String(open));
  if (!open) markChips(null);
  syncScrim();
}
function closePanels() {
  $('#side').classList.remove('open'); $('#menuBtn').setAttribute('aria-expanded', 'false'); setSrc(false);
}
function openSide() {
  closePanels(); $('#side').classList.add('open'); $('#menuBtn').setAttribute('aria-expanded', 'true'); syncScrim();
}
/* Source panel width: drag the left edge, use arrow keys, or double-click to reset. */
const SRC_MIN = 300, SRC_DEFAULT = 380;
const srcMax = () => Math.max(SRC_MIN, Math.min(760, innerWidth - (overlay() ? 40 : 264 + 360)));
function setSrcW(w, save) {
  w = Math.round(Math.max(SRC_MIN, Math.min(srcMax(), w)));
  document.documentElement.style.setProperty('--src-w', w + 'px');
  const r = $('#resizer'); r.setAttribute('aria-valuenow', String(w)); r.setAttribute('aria-valuemax', String(srcMax()));
  if (save) { try { localStorage.setItem('da-src-w', String(w)); } catch { /* storage unavailable */ } }
}
function wireResizer() {
  const rz = $('#resizer');
  try { const v = Number(localStorage.getItem('da-src-w')); if (v) setSrcW(v); } catch { /* ignore */ }
  rz.addEventListener('pointerdown', (e) => { rz.setPointerCapture(e.pointerId); document.body.classList.add('resizing'); });
  rz.addEventListener('pointermove', (e) => { if (rz.hasPointerCapture(e.pointerId)) setSrcW(innerWidth - e.clientX); });
  const end = () => { document.body.classList.remove('resizing'); setSrcW(Number(rz.getAttribute('aria-valuenow')), true); };
  rz.addEventListener('pointerup', end); rz.addEventListener('pointercancel', end);
  rz.addEventListener('dblclick', () => setSrcW(SRC_DEFAULT, true));
  rz.addEventListener('keydown', (e) => {
    const cur = $('#src').getBoundingClientRect().width;
    const next = { ArrowLeft: cur + 24, ArrowRight: cur - 24, Home: SRC_MIN, End: srcMax() }[e.key];
    if (next != null) { e.preventDefault(); setSrcW(next, true); }
  });
  addEventListener('resize', () => { syncScrim(); setSrcW(parseInt(document.documentElement.style.getPropertyValue('--src-w'), 10) || SRC_DEFAULT); });
}

function msgEl(m) {
  if (m.role === 'user') return el('div', 'msg user', m.content);
  const wrap = el('div', 'msg ai' + (m.error ? ' err' : ''));
  const av = el('div', 'av'); av.innerHTML = SPARK;
  const body = el('div', 'body'); body.innerHTML = fmt(m.content, m.sources || []);
  const cited = new Set((m.content.match(/\[S\d+\]/g) || []));
  const extra = (m.sources || []).filter((s) => !cited.has(s.citation));
  if (extra.length) { const row = el('div', 'srcs'); row.innerHTML = extra.map((s) => chipHtml(s, s.citation.replace(/[\[\]]/g, ''))).join(''); body.append(row); }
  if (m.welcome && st.msgs.length === 1) {
    const row = el('div', 'sugg');
    for (const q of ['Give me a summary of this document', 'What are the main findings?', 'What does page 1 say?']) { const b = el('button', 'chip amb', q); b.type = 'button'; b.dataset.ask = q; row.append(b); }
    body.append(row);
  }
  wrap.append(av, body);
  return wrap;
}
function renderThread() {
  const t = $('#thread');
  t.replaceChildren(...st.msgs.map(msgEl));
  if (st.busy) { const ty = el('div', 'typing'); ty.setAttribute('aria-label', 'Assistant is answering'); ty.innerHTML = '<i></i><i></i><i></i>'; t.append(ty); }
  t.scrollTop = t.scrollHeight;
  $('#send').disabled = st.busy;
  markChips(st.openChunk ?? null);
}
function renderChats() {
  const ul = $('#chats'); ul.replaceChildren();
  if (!st.session.authenticated) { ul.append(el('li', 'empty-note', 'Sign in to keep your chat history. Guest chats are temporary.')); return; }
  if (!st.chats.length) { ul.append(el('li', 'empty-note', 'Saved chats appear here.')); return; }
  for (const c of st.chats) {
    const li = el('li', c.id === st.chatId ? 'on' : '');
    const open = el('button', 'open', c.title); open.type = 'button'; open.dataset.open = c.id; open.title = c.title;
    const del = el('button', 'icon-btn del'); del.type = 'button'; del.dataset.del = c.id; del.setAttribute('aria-label', 'Delete chat ' + c.title);
    del.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M5 4.5l.5 8h5l.5-8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    li.append(open, del); ul.append(li);
  }
}
function renderAcct() {
  const a = $('#acct'); a.replaceChildren();
  if (st.session.authenticated) {
    a.append(el('span', null, 'Signed in as'), el('strong', null, st.session.email));
    const b = el('button', 'link', 'Sign out'); b.type = 'button'; b.id = 'logoutBtn'; a.append(b);
  } else {
    a.append(el('span', null, 'Guest session. Sign in to keep chat history.'));
    const b = el('button', 'link', 'Sign in or create account'); b.type = 'button'; b.id = 'loginBtn'; a.append(b);
  }
}

/* ---------- upload & indexing ---------- */
function buildProgress() {
  $('#viz').innerHTML = `<div><div class="row"><div class="pg"><i data-w="90"></i><i data-w="100"></i><i data-w="70"></i><i data-w="95"></i><i data-w="80"></i><i data-w="60"></i><div class="beam"></div></div><div class="lns">${[100, 86, 94, 70, 90, 58].map((w) => `<div class="ln" data-w="${w}"></div>`).join('')}</div></div><div class="cap">EXTRACT TEXT</div></div>
  <div><div class="chk"><span>chunk 1</span><span>chunk 2</span><span>chunk 3</span><span>chunk 4</span></div><div class="cap">CHUNK BY PAGE</div></div>
  <div><div class="vec">${Array.from({ length: 8 }, (_, i) => `<i data-d="${i}"></i>`).join('')}</div><div class="cap">EMBED</div></div>`;
  // Inline style attributes are blocked by the CSP, so sizes are applied through the CSSOM.
  $('#viz').querySelectorAll('[data-w]').forEach((n) => { n.style.width = n.dataset.w + '%'; });
  $('#viz').querySelectorAll('[data-d]').forEach((n) => { n.style.animationDelay = (-0.12 * n.dataset.d).toFixed(2) + 's'; });
  $('#psteps').innerHTML = STEPS.map((s) => `<li><span class="dot">${CHECK}</span><span>${esc(s)}</span></li>`).join('');
}
function setProgress(stage, pct) {
  const step = stage === 'ready' ? 6 : (STAGE[stage] ?? Math.min(5, Math.floor(pct / 17)));
  const card = $('#pcard');
  card.dataset.step = String(step);
  card.className = 'pcard ' + Array.from({ length: step + 1 }, (_, i) => 'has' + i).join(' ');
  $('#psteps').querySelectorAll('li').forEach((li, i) => { li.className = i < step ? 'done' : i === step ? 'active' : ''; });
  $('#ppct').textContent = Math.round(pct) + '%';
  $('#pfill').style.width = Math.max(2, pct) + '%';
  $('#pbar').setAttribute('aria-valuenow', String(Math.round(pct)));
}
function failProgress(msg) {
  $('#perr').textContent = msg; $('#perr').hidden = false; $('#pretry').hidden = false;
  $('#psteps').querySelectorAll('.active').forEach((li) => li.classList.remove('active'));
}
async function handleFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') return toast('Please choose a PDF file.', true);
  if (file.size > 10 * 1024 * 1024) return toast('That PDF is over the 10 MB limit.', true);
  if (!file.size) return toast('That file is empty.', true);
  st.msgs = []; st.doc = null; st.chatId = newId(); st.createdAt = Date.now(); setSrc(false);
  const token = ++st.poll;
  $('#drop').hidden = true; $('#pcard').hidden = false; $('#perr').hidden = true; $('#pretry').hidden = true;
  $('#pname').textContent = file.name; buildProgress(); setProgress('queued', 1);
  try {
    const fd = new FormData(); fd.append('file', file);
    const r = await api('/documents/upload', { method: 'POST', body: fd });
    st.doc = { document_id: r.document_id, filename: r.filename || file.name };
    setProgress(r.stage || 'queued', r.progress || 1);
    pollStatus(token, 0);
  } catch (e) { failProgress(e.message); }
}
async function pollStatus(token, errors) {
  if (token !== st.poll) return;
  try {
    const s = await api(`/documents/${st.doc.document_id}/status`);
    if (token !== st.poll) return;
    if (s.status === 'failed') return failProgress(s.error || 'Indexing failed. Check the backend log and try again.');
    if (s.status === 'indexed') {
      setProgress('ready', 100);
      st.doc = { document_id: s.document_id, filename: s.filename || st.doc.filename, page_count: s.page_count };
      return setTimeout(() => { if (token !== st.poll) return; st.msgs = [{ role: 'assistant', welcome: true, content: 'Your document is indexed and ready. Ask a question and I will cite the exact PDF page for each answer.' }]; enterChat(); }, 900);
    }
    setProgress(s.stage, s.progress || 1);
    setTimeout(() => pollStatus(token, 0), 1000);
  } catch (e) {
    if (e.status === 401 || e.status === 404 || errors >= 4) return failProgress(e.message);
    setTimeout(() => pollStatus(token, errors + 1), 1500);
  }
}
function resetUpload() {
  st.poll++; $('#drop').hidden = false; $('#pcard').hidden = true; $('#file').value = '';
}
function enterChat() { showView('chat'); renderThread(); renderChats(); }

/* ---------- asking & sources ---------- */
async function ask(q) {
  q = q.trim();
  if (!q || !st.doc || st.busy) return;
  st.lastQ = q; st.msgs.push({ role: 'user', content: q }); st.busy = true; renderThread();
  try {
    const r = await send('/documents/ask', { query: q, document_id: st.doc.document_id, top_k: 5 });
    st.msgs.push({ role: 'assistant', content: r.answer || 'No answer was returned.', sources: r.sources || [] });
  } catch (e) {
    st.msgs.push({ role: 'assistant', error: true, content: e.status === 409 ? 'This document is still being indexed. Please wait a moment.' : e.message });
  }
  st.busy = false; renderThread(); scheduleSave();
}
async function openSource(chunkId) {
  if ($('#src').classList.contains('open') && String(st.openChunk) === String(chunkId)) return setSrc(false);
  setSrc(true); markChips(chunkId);
  const body = $('#srcBody'); body.replaceChildren(el('div', 'skel'), el('div', 'skel'), el('div', 'skel'));
  try {
    const c = await api(`/documents/${st.doc.document_id}/chunks/${encodeURIComponent(chunkId)}`);
    const words = [...new Set(st.lastQ.toLowerCase().match(/[a-z0-9]{4,}/g) || [])].slice(0, 8);
    let html = esc(c.text);
    if (words.length) html = html.replace(new RegExp('(' + words.join('|') + ')', 'gi'), '<mark>$1</mark>');
    const link = el('a', 'link', 'Open in a new tab'); link.target = '_blank'; link.rel = 'noopener';
    link.href = `/source.html?document_id=${encodeURIComponent(c.document_id)}&chunk_id=${encodeURIComponent(c.chunk_id)}`;
    const p = el('p', 'passage'); p.innerHTML = html;
    const h = el('h3', 'serif', c.heading || 'Passage');
    body.replaceChildren(el('div', 'ref', `${c.filename} · page ${c.page_number}`), h, p, link);
  } catch (e) { body.replaceChildren(el('p', 'perr', e.message)); }
}

/* ---------- account chats ---------- */
let saveTimer = 0;
function scheduleSave() { clearTimeout(saveTimer); if (st.session.authenticated && st.doc) saveTimer = setTimeout(saveChat, 600); }
async function saveChat() {
  const first = st.msgs.find((m) => m.role === 'user');
  const title = (first ? first.content : st.doc.filename).replace(/\s+/g, ' ').trim().slice(0, 80) || 'Untitled chat';
  const chat = { id: st.chatId, title, createdAt: st.createdAt, updatedAt: Date.now(), document: { document_id: st.doc.document_id, filename: st.doc.filename, page_count: st.doc.page_count || null }, messages: st.msgs.filter((m) => !m.error).slice(-300).map(({ role, content, sources }) => (sources ? { role, content, sources } : { role, content })) };
  try {
    await send('/chats/' + encodeURIComponent(chat.id), { chat }, 'PUT');
    const i = st.chats.findIndex((c) => c.id === chat.id);
    if (i >= 0) st.chats.splice(i, 1);
    st.chats.unshift(chat); renderChats();
  } catch (e) { toast('Chat was not saved: ' + e.message, true); }
}
async function loadChats() {
  try { st.chats = (await api('/chats')).chats || []; } catch { st.chats = []; }
  renderChats();
}
async function openChat(id) {
  const c = st.chats.find((x) => x.id === id); if (!c) return;
  closePanels(); st.poll++;
  st.chatId = c.id; st.createdAt = c.createdAt || Date.now(); st.msgs = (c.messages || []).map((m) => ({ ...m })); st.doc = null;
  if (c.document && c.document.document_id) {
    try {
      const s = await api(`/documents/${c.document.document_id}/status`);
      if (s.status === 'indexed') { st.doc = { document_id: s.document_id, filename: s.filename || c.document.filename, page_count: s.page_count }; }
    } catch { /* document unavailable */ }
  }
  if (st.doc) { enterChat(); } else { toast('The PDF for this chat is no longer available. Upload it again to continue.', true); resetUpload(); showView('upload'); }
}
function newChat() {
  closePanels(); st.poll++; st.chatId = newId(); st.createdAt = Date.now(); st.doc = null; st.msgs = []; st.busy = false;
  $('#srcBody').innerHTML = '<p class="muted">Select a citation in an answer to read the exact passage here.</p>';
  resetUpload(); showView('upload'); renderChats();
}

/* ---------- authentication ---------- */
function setMode(mode) {
  st.mode = mode; const up = mode === 'signup';
  $('#tabIn').setAttribute('aria-selected', String(!up)); $('#tabUp').setAttribute('aria-selected', String(up));
  $('#authTitle').textContent = up ? 'Create account' : 'Sign in'; $('#authGo').textContent = up ? 'Create account' : 'Sign in';
  $('#pw').autocomplete = up ? 'new-password' : 'current-password';
  $('#meter').hidden = !up; $('#pwHint').hidden = !up; $('#authErr').hidden = true; strength();
}
function strength() {
  const v = $('#pw').value;
  const score = [v.length >= 12, /[A-Za-z]/.test(v), /\d/.test(v), /[^A-Za-z0-9\s]/.test(v)].filter(Boolean).length;
  $('#meter').querySelectorAll('span').forEach((s, i) => { s.className = i < score ? 'on' + (score <= 2 ? 1 : score === 3 ? 2 : 3) : ''; });
  return score === 4;
}
async function submitAuth(e) {
  e.preventDefault();
  const email = $('#email').value.trim(), password = $('#pw').value, err = $('#authErr');
  err.hidden = true;
  if (!/^\S+@\S+\.\S+$/.test(email)) { err.textContent = 'Enter a valid email address.'; err.hidden = false; return; }
  if (st.mode === 'signup' && !strength()) { err.textContent = 'Use at least 12 characters with a letter, a number and a special character.'; err.hidden = false; return; }
  if (!password) { err.textContent = 'Enter your password.'; err.hidden = false; return; }
  $('#authGo').disabled = true;
  try {
    const r = await send(st.mode === 'signup' ? '/auth/signup' : '/auth/login', { email, password });
    st.session = { authenticated: true, email: r.email }; $('#pw').value = ''; $('#authDlg').close();
    renderAcct(); await loadChats(); toast('Signed in as ' + r.email); if (st.doc) scheduleSave();
  } catch (x) { err.textContent = x.message; err.hidden = false; }
  $('#authGo').disabled = false;
}
async function logout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
  st.session = { authenticated: false, email: null }; st.chats = [];
  try { await api('/auth/guest', { method: 'POST' }); } catch { /* backend offline */ }
  renderAcct(); newChat();
}

/* ---------- wiring ---------- */
function wire() {
  $('#newBtn').onclick = newChat;
  $('#menuBtn').onclick = openSide;
  $('#srcBtn').onclick = () => setSrc(!$('#src').classList.contains('open'));
  $('#srcClose').onclick = () => setSrc(false); $('#scrim').onclick = closePanels;
  wireResizer();
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePanels(); });
  $('#file').onchange = (e) => handleFile(e.target.files[0]);
  $('#pretry').onclick = resetUpload;
  const drop = $('#drop');
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => handleFile(e.dataTransfer.files[0]));
  const q = $('#q');
  q.addEventListener('input', () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 160) + 'px'; });
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#form').requestSubmit(); } });
  $('#form').onsubmit = (e) => { e.preventDefault(); const v = q.value; q.value = ''; q.style.height = 'auto'; ask(v); };
  $('#thread').addEventListener('click', (e) => {
    const c = e.target.closest('[data-chunk]'); if (c) return openSource(c.dataset.chunk);
    const a = e.target.closest('[data-ask]'); if (a) ask(a.dataset.ask);
  });
  $('#chats').addEventListener('click', async (e) => {
    const o = e.target.closest('[data-open]'); if (o) return openChat(o.dataset.open);
    const d = e.target.closest('[data-del]');
    if (d && confirm('Delete this chat? The indexed PDF is kept.')) {
      try { await api('/chats/' + encodeURIComponent(d.dataset.del), { method: 'DELETE' }); st.chats = st.chats.filter((c) => c.id !== d.dataset.del); if (d.dataset.del === st.chatId) newChat(); else renderChats(); }
      catch (x) { toast(x.message, true); }
    }
  });
  $('#acct').addEventListener('click', (e) => {
    if (e.target.id === 'loginBtn') { closePanels(); setMode('login'); $('#authDlg').showModal(); $('#email').focus(); }
    if (e.target.id === 'logoutBtn') logout();
  });
  $('#tabIn').onclick = () => setMode('login'); $('#tabUp').onclick = () => setMode('signup');
  $('#authX').onclick = () => $('#authDlg').close();
  $('#authForm').onsubmit = submitAuth; $('#pw').addEventListener('input', strength);
  $('#pwShow').onclick = (e) => { const p = $('#pw'); const show = p.type === 'password'; p.type = show ? 'text' : 'password'; e.target.textContent = show ? 'Hide' : 'Show'; e.target.setAttribute('aria-pressed', String(show)); };
}
async function init() {
  wire(); renderAcct(); renderChats(); showView('upload');
  try {
    st.session = await api('/auth/session');
    if (!st.session.authenticated) st.session = { ...(await api('/auth/guest', { method: 'POST' })), authenticated: false };
  } catch (e) { toast(e.message, true); }
  renderAcct();
  if (st.session.authenticated) await loadChats();
}
init();
