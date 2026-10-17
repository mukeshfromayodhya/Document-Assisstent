const card = document.getElementById('card');
const p = new URLSearchParams(location.search);
const doc = p.get('document_id') || '';
const chunk = p.get('chunk_id') || '';
function show(nodes) { card.replaceChildren(...nodes); }
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
(async () => {
  if (!/^[a-f0-9]{64}$/.test(doc) || !/^\d+$/.test(chunk)) return show([el('p', 'perr', 'This source link is not valid.')]);
  try {
    const res = await fetch(`/api/documents/${doc}/chunks/${chunk}`, { credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The passage could not be loaded.');
    document.title = `${data.filename} · page ${data.page_number}`;
    show([el('div', 'ref', `${data.filename} · page ${data.page_number}`), el('h3', 'serif', data.heading || 'Passage'), el('p', 'passage', data.text)]);
  } catch (e) { show([el('p', 'perr', e.message)]); }
})();
