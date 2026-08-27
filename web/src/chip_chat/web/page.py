"""The entry flow and the chat widget, as one self-contained page.

Deliberately one file of HTML with inline CSS and inline script, and no build
step. The reason has not changed since the week-one slice: a front-end toolchain
in the middle of this would be a second deployment story answering a question
nobody asked, and the page is small enough that the whole surface a visitor
touches can be read in one sitting.

Six things on this page are requirements rather than decoration.

**The unaffiliated-demo disclosure.** :data:`~chip_chat.web.copy.BANNER` is
issue #70's launch criterion, and the criterion has two halves: on the entry
screen *and* persisting in the chat header. So the banner is ``position:
sticky`` at the top of the viewport rather than a block at the top of the
document -- it is on screen in the conversation, at the bottom of a long
transcript, on a phone. There is no control that hides it.

**No borrowed branding.** No logo, no wordmark, and a palette chosen away from
the incumbent's: the accent here is a slate indigo, and the surface a warm
paper grey. ``docs/public-demo.md`` records the review. The name is already
deliberately distinct from the assistant this is not; the visual design is too.

**``noindex``.** The meta tag here is half of it; the ``X-Robots-Tag`` header
and ``/robots.txt`` in :mod:`chip_chat.api.app` are the half that works when
something fetches the page without executing it.

**The confirmation card.** Issue #68's first criterion is that *no write is
reachable from the UI without the card*, and the card's layout is fixed by PRD
Flow 3. :func:`_SCRIPT`'s ``renderCard`` draws that layout and nothing else
draws an order; there is no path in this file from a typed message to a placed
order that does not go through a rendered card and a pressed button.

**"Simulated", on the card.** Not in a footnote. Every card and every receipt
carries :data:`~chip_chat.web.copy.SIMULATED` in its action row, where the eye
already is because that is where the buttons are.

**The switcher, one tap from the chat surface.** Issue #69. It is in the chat
header beside the persona's label, and pressing it clears the transcript in
front of the visitor before the new opening message arrives -- the restart is
visible first and explained second, which is the right order for something that
just threw away what was on screen.

The stop state has its own page (:func:`stop_page`) because the spend cap can
refuse a visitor on entry, before there is any conversation to put a message in.
It carries no composer and no script: offering a text box that cannot be
answered would be a worse experience than the one the cap is protecting.
"""

from chip_chat.web.copy import (
    BANNER,
    NAME_GATE_HINT,
    NAME_GATE_PLACEHOLDER,
    NAME_GATE_SUBMIT,
    NAME_GATE_TITLE,
    PHOTO_RETENTION,
    SIMULATED,
    STOP_STATE_HEADING,
    SWITCH_CONFIRM,
    SWITCH_LABEL,
    TITLE,
)

__all__ = ["chat_page", "stop_page"]

_STYLE = """\
:root { color-scheme: light dark; --bg:#f6f5f3; --card:#fff; --ink:#1d1c1a;
  --muted:#67635d; --line:#e2ded7; --accent:#3d4a72; --accent-ink:#fff;
  --warn:#7a6a2f; }
@media (prefers-color-scheme: dark) { :root { --bg:#131313; --card:#1d1d1e;
  --ink:#eeece8; --muted:#a29d95; --line:#2f2f31; --accent:#8d9bcb;
  --accent-ink:#14161f; --warn:#cbbd85; } }
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5
  ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%; }
.banner { position:sticky; top:0; z-index:9; background:var(--card);
  border-bottom:1px solid var(--line); color:var(--muted); font-size:.78rem;
  padding:.45rem .9rem; text-align:center; }
main { max-width: 44rem; margin: 0 auto; padding: 1rem 1rem 7.5rem; }
h1 { font-size:1.4rem; margin:1.5rem 0 .35rem; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:0 0 1.25rem; font-size:.92rem; }
label { display:block; font-size:.85rem; color:var(--muted); margin:0 0 .35rem; }
.head { display:flex; align-items:center; gap:.75rem; justify-content:space-between;
  border-bottom:1px solid var(--line); padding:.6rem 0 .7rem; margin-bottom:.5rem;
  position:sticky; top:2.1rem; background:var(--bg); z-index:8; }
.head .who { font-weight:600; font-size:.95rem; }
.head .role { color:var(--muted); font-size:.8rem; }
.msg { border:1px solid var(--line); border-radius:.9rem; padding:.7rem .9rem;
  margin:.55rem 0; background:var(--card); white-space:pre-wrap;
  overflow-wrap:anywhere; }
.msg.you { background:transparent; border-color:transparent; color:var(--muted);
  padding-left:0; padding-right:0; }
.msg.system { background:transparent; border-style:dashed; color:var(--muted);
  font-size:.88rem; }
.msg img { display:block; max-width:100%; height:auto; border-radius:.6rem;
  margin:.15rem 0 .45rem; }
.msg .cap { color:var(--muted); font-size:.78rem; }
.card { border:1px solid var(--accent); border-radius:.9rem; padding:.85rem .9rem;
  margin:.55rem 0; background:var(--card); }
.card .title { font-weight:600; letter-spacing:.02em; text-transform:uppercase;
  font-size:.86rem; }
.card .mods { color:var(--muted); font-size:.88rem; margin:.15rem 0 0; }
.card hr { border:0; border-top:1px solid var(--line); margin:.6rem 0; }
.card .money { font-variant-numeric:tabular-nums; font-weight:600; }
.card .pts { color:var(--muted); font-weight:400; }
.card .actions { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem;
  margin-top:.7rem; }
.card .sim { color:var(--warn); font-size:.78rem; letter-spacing:.04em; }
.card .stamp { color:var(--muted); font-size:.78rem; margin-top:.5rem; }
.edit-row { display:flex; align-items:center; gap:.5rem; padding:.25rem 0;
  border-bottom:1px solid var(--line); }
.edit-row .grow { flex:1; font-size:.9rem; }
.edit-row button { padding:.25rem .55rem; }
button { font:inherit; border-radius:.6rem; border:1px solid var(--line);
  background:var(--card); color:var(--ink); padding:.45rem .8rem; cursor:pointer;
  min-height:2.25rem; }
button.primary { background:var(--accent); color:var(--accent-ink);
  border-color:var(--accent); }
button.link { border-color:transparent; background:transparent;
  color:var(--muted); text-decoration:underline; padding:.25rem .35rem; }
button[disabled] { opacity:.5; cursor:default; }
.chips { display:flex; flex-wrap:wrap; gap:.4rem; margin:.75rem 0 0; }
.chips button { font-size:.85rem; color:var(--muted); }
form.composer { position:fixed; left:0; right:0; bottom:0; background:var(--bg);
  border-top:1px solid var(--line); padding:.7rem;
  padding-bottom: calc(.7rem + env(safe-area-inset-bottom)); }
form.composer .row { max-width:44rem; margin:0 auto; display:flex; gap:.5rem; }
input[type=text] { flex:1; font:inherit; padding:.6rem .8rem; border-radius:.6rem;
  border:1px solid var(--line); background:var(--card); color:var(--ink);
  min-width:0; }
.photo-btn { display:inline-flex; align-items:center; justify-content:center;
  border:1px solid var(--line); border-radius:.6rem; background:var(--card);
  color:var(--muted); padding:.45rem .7rem; cursor:pointer; font-size:.85rem;
  white-space:nowrap; }
.gate form { display:flex; gap:.5rem; }
.stop { text-align:center; padding:4rem 1rem; }
.stop p { color:var(--muted); }
[hidden] { display:none !important; }
@media (max-width: 26rem) {
  main { padding-left:.75rem; padding-right:.75rem; }
  .photo-btn { padding:.45rem .55rem; }
}
"""

_SCRIPT = """\
const $ = id => document.getElementById(id);
const gate = $('gate'), chat = $('chat'), log = $('log'), chips = $('chips');
const composer = $('composer'), input = $('input'), photo = $('photo');
const SIMULATED = %(simulated)s;
const RETENTION = %(retention)s;
const SWITCH_CONFIRM = %(switch_confirm)s;

let persona = null;
let busy = false;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function bottom(node) { node.scrollIntoView({block: 'end'}); }

function bubble(text, who) {
  const node = el('div', 'msg ' + who, text);
  log.appendChild(node);
  bottom(node);
  return node;
}

function setChips(items) {
  chips.replaceChildren();
  for (const item of items || []) {
    const button = el('button', null, item.prompt);
    button.dataset.lane = item.lane;
    button.onclick = () => { chips.replaceChildren(); send(item.prompt, {}); };
    chips.appendChild(button);
  }
}

// --- the confirmation card, PRD Flow 3 ------------------------------------

function lineTitle(card) {
  const first = (card.lines && card.lines[0]) || {};
  const store = (card.store && card.store.name) || '';
  const name = first.name || 'Order';
  const extra = (card.lines || []).length - 1;
  const many = extra > 0 ? ' + ' + extra + ' more' : '';
  return (name + many).toUpperCase() + (store ? ' \\u2014 ' + store : '');
}

function modifierText(card) {
  const parts = [];
  for (const line of card.lines || []) {
    const bits = [];
    if (line.quantity > 1) bits.push(line.quantity + ' \\u00d7 ' + line.name);
    else if ((card.lines || []).length > 1) bits.push(line.name);
    for (const pick of line.selections || []) {
      const named = pick.portion && pick.portion !== 'regular';
      bits.push((named ? pick.portion + ' ' : '') + pick.name);
    }
    if (bits.length) parts.push(bits.join(' \\u00b7 '));
  }
  return parts;
}

function renderCard(card, isReceipt) {
  const node = el('div', 'card');
  node.dataset.draftId = card.draft_id || '';
  if (isReceipt) {
    node.appendChild(el('div', 'title', 'RECEIPT \\u2014 ' + (card.order_id || '')));
  }
  node.appendChild(el('div', 'title', lineTitle(card)));
  for (const part of modifierText(card)) node.appendChild(el('p', 'mods', part));
  node.appendChild(el('hr'));
  const money = el('div', 'money', '$' + card.total);
  const points = persona ? persona.points_balance : null;
  if (points !== null && points !== undefined) {
    const shown = points.toLocaleString() + ' pts available';
    money.appendChild(el('span', 'pts', ' \\u00b7 ' + shown));
  }
  node.appendChild(money);

  const actions = el('div', 'actions');
  if (!isReceipt) {
    const edit = el('button', null, 'Edit');
    edit.onclick = () => editCard(node, card);
    const place = el('button', 'primary', 'Place order');
    place.onclick = () => {
      place.disabled = true;
      edit.disabled = true;
      send('Yes, place it.', {confirm_draft_id: card.draft_id});
    };
    actions.appendChild(edit);
    actions.appendChild(place);
  }
  actions.appendChild(el('span', 'sim', '\\u00b7 ' + SIMULATED));
  node.appendChild(actions);
  if (isReceipt) {
    const kept = 'Kept in this conversation \\u2014 ask me about order '
      + (card.order_id || '') + ' any time.';
    node.appendChild(el('div', 'stamp', kept));
  }
  log.appendChild(node);
  bottom(node);
  return node;
}

// --- editing a card in place ---------------------------------------------

function editCard(node, card) {
  const draft = (card.lines || []).map(line => ({
    item_id: line.item_id,
    name: line.name,
    quantity: line.quantity,
    selections: (line.selections || []).map(pick => ({
      modifier_item_id: pick.modifier_item_id, portion: pick.portion, name: pick.name,
    })),
  }));
  const panel = el('div');
  function draw() {
    panel.replaceChildren();
    draft.forEach((line, index) => {
      const row = el('div', 'edit-row');
      row.appendChild(el('span', 'grow', line.name));
      const less = el('button', null, '\\u2212');
      less.onclick = () => {
        line.quantity = Math.max(0, line.quantity - 1);
        if (!line.quantity) draft.splice(index, 1);
        draw();
      };
      const count = el('span', null, String(line.quantity));
      const more = el('button', null, '+');
      more.onclick = () => { line.quantity += 1; draw(); };
      row.appendChild(less); row.appendChild(count); row.appendChild(more);
      panel.appendChild(row);
      for (const pick of line.selections) {
        const sub = el('div', 'edit-row');
        sub.appendChild(el('span', 'grow', '\\u21b3 ' + pick.name));
        const drop = el('button', 'link', 'Remove');
        drop.onclick = () => {
          line.selections.splice(line.selections.indexOf(pick), 1);
          draw();
        };
        sub.appendChild(drop);
        panel.appendChild(sub);
      }
    });
    const actions = el('div', 'actions');
    const save = el('button', 'primary', 'Re-price');
    save.onclick = () => revise(node, card, draft);
    const cancel = el('button', 'link', 'Cancel');
    cancel.onclick = () => { node.replaceWith(renderCardInto(node, card, false)); };
    actions.appendChild(save); actions.appendChild(cancel);
    actions.appendChild(el('span', 'sim', '\\u00b7 ' + SIMULATED));
    panel.appendChild(actions);
  }
  draw();
  node.replaceChildren(el('div', 'title', 'EDIT THIS ORDER'), panel);
  bottom(node);
}

function renderCardInto(oldNode, card, isReceipt) {
  const fresh = renderCard(card, isReceipt);
  oldNode.replaceWith(fresh);
  return fresh;
}

async function revise(node, card, draft) {
  const body = {
    draft_id: card.draft_id,
    lines: draft.map(line => ({
      item_id: line.item_id,
      quantity: line.quantity,
      selections: line.selections.map(pick => ({
        modifier_item_id: pick.modifier_item_id, portion: pick.portion,
      })),
    })),
  };
  const res = await fetch('/api/draft/revise', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.card) {
    renderCardInto(node, data.card, false);
    if (data.reply) bubble(data.reply, 'system');
  } else {
    node.replaceChildren();
    renderCardInto(node, card, false);
    bubble(data.reply || 'That edit did not price up.', 'system');
  }
}

// --- one turn, streamed ---------------------------------------------------

async function send(text, extra) {
  if (busy) return;
  busy = true;
  chips.replaceChildren();
  if (text) bubble(text, 'you');
  const answer = bubble('\\u2026', 'them');
  let streamed = '';
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/x-ndjson'},
      body: JSON.stringify(Object.assign({message: text}, extra || {})),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf('\\n')) >= 0) {
        const raw = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 1);
        if (!raw) continue;
        const frame = JSON.parse(raw);
        if (frame.type === 'text') {
          streamed += frame.text;
          answer.textContent = streamed;
          bottom(answer);
        } else if (frame.type === 'card') {
          renderCard(frame.card, frame.receipt);
        }
      }
    }
    if (!streamed) answer.textContent = 'I did not have anything to say to that.';
  } catch (error) {
    answer.textContent = 'I could not reach the server just then. Try again.';
  } finally {
    busy = false;
    input.focus();
  }
}

// --- entry, and switching -------------------------------------------------

function showPersona(body, restarted) {
  persona = body.visitor;
  gate.hidden = true;
  chat.hidden = false;
  composer.hidden = false;
  $('who').textContent = (persona && persona.display_name) || 'You';
  $('role').textContent = persona ? persona.label : 'no synthetic account loaded';
  if (restarted) log.replaceChildren();
  bubble(body.opening, restarted ? 'system' : 'them');
  setChips(body.chips);
  input.focus();
}

async function enter(name) {
  const res = await fetch('/api/entry', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name || null}),
  });
  const body = await res.json();
  if (body.stopped) { document.location.reload(); return; }
  showPersona(body, false);
}

async function switchPersona() {
  if (busy) return;
  busy = true;
  log.replaceChildren();
  bubble(SWITCH_CONFIRM, 'system');
  try {
    const res = await fetch('/api/switch', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    const body = await res.json();
    if (body.stopped) { document.location.reload(); return; }
    busy = false;
    showPersona(body, true);
  } finally {
    busy = false;
  }
}

// --- photographs ----------------------------------------------------------

async function upload(file) {
  if (busy || !file) return;
  const holder = el('div', 'msg you');
  const image = el('img');
  image.alt = 'The photo you sent';
  image.src = URL.createObjectURL(file);
  holder.appendChild(image);
  holder.appendChild(el('div', 'cap', RETENTION));
  log.appendChild(holder);
  bottom(holder);
  // The body is the image itself: `/api/photo` reads the socket under a byte
  // ceiling, and a multipart envelope would have to be parsed before the first
  // gate could refuse anything.
  const res = await fetch('/api/photo', {
    method: 'POST',
    headers: {'Content-Type': file.type || 'application/octet-stream'},
    body: file,
  });
  const body = await res.json();
  if (!body.photo) {
    bubble(body.reply || 'That photo could not be accepted.', 'system');
    return;
  }
  if (body.retention) holder.lastChild.textContent = body.retention;
  send('Here is a photo of what I want.', {photo: body.photo});
}

$('gate-form').onsubmit = event => {
  event.preventDefault();
  enter($('name').value.trim());
};
$('switch').onclick = switchPersona;
photo.onchange = () => { upload(photo.files[0]); photo.value = ''; };
composer.onsubmit = event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  send(text, {});
};
$('name').focus();
"""


def _head(title: str) -> str:
    """The parts of the document that are the same on both pages."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow">'
        f'<meta name="description" content="{BANNER}">'
        f"<title>{title}</title><style>{_STYLE}</style></head><body>"
        f'<div class="banner" role="note">{BANNER}</div>'
    )


def chat_page() -> str:
    """Return the whole page: the name gate, the conversation, and the composer.

    Both views ship in one document and the gate hands over to the chat without
    a navigation, which is what issue #66's *name to persona to conversation in
    one screen, under two seconds* asks for: the second screen is already loaded
    before the visitor types, so the only thing between the name and the
    conversation is one ``POST /api/entry``.

    Nothing here knows which persona the visitor will be assigned. The opening
    message, the persona's label and the chips all arrive in that response, from
    a binding the server made and the browser was never consulted about.
    """
    script = _SCRIPT % {
        "simulated": _js(SIMULATED),
        "retention": _js(PHOTO_RETENTION),
        "switch_confirm": _js(SWITCH_CONFIRM),
    }
    return (
        _head(TITLE)
        + "<main>"
        + '<section id="gate" class="gate">'
        + "<h1>Cilantro</h1>"
        + f'<p class="sub">{NAME_GATE_HINT}</p>'
        + f'<label for="name">{NAME_GATE_TITLE}</label>'
        + '<form id="gate-form">'
        + '<input type="text" id="name" autocomplete="off" autofocus maxlength="64"'
        + f' placeholder="{NAME_GATE_PLACEHOLDER}">'
        + f'<button class="primary" type="submit">{NAME_GATE_SUBMIT}</button>'
        + "</form></section>"
        + '<section id="chat" hidden>'
        + '<div class="head"><div>'
        + '<div class="who" id="who"></div>'
        + '<div class="role" id="role"></div>'
        + "</div>"
        + f'<button id="switch" type="button">{SWITCH_LABEL}</button>'
        + "</div>"
        + '<div id="log" role="log" aria-live="polite"></div>'
        + '<div class="chips" id="chips"></div>'
        + "</section></main>"
        + '<form class="composer" id="composer" hidden><div class="row">'
        + '<label class="photo-btn" for="photo">Photo</label>'
        + '<input type="file" id="photo" accept="image/*" hidden>'
        + '<input type="text" id="input" autocomplete="off"'
        + ' placeholder="Ask about the menu, your points, or order something">'
        + '<button class="primary" type="submit">Send</button>'
        + "</div></form>"
        + f"<script>{script}</script></body></html>"
    )


def stop_page(message: str) -> str:
    """Return the stop-state page.

    Served with HTTP 200. PRD requirement S4: this is a designed state and not
    an error, so it never carries a 4xx or 5xx and never apologises for a
    failure. Nothing failed -- the cap worked.

    Args:
        message: The one stop-state sentence,
            :data:`~chip_chat.api.outcome.STOP_STATE_MESSAGE`.
    """
    return (
        _head(TITLE)
        + f'<main class="stop"><h1>{STOP_STATE_HEADING}</h1><p>{message}</p></main>'
        + "</body></html>"
    )


def _js(value: str) -> str:
    """Render a copy string as a JavaScript literal.

    The strings interpolated into :data:`_SCRIPT` are constants from
    :mod:`chip_chat.web.copy` rather than anything a visitor typed, so this is a
    correctness measure rather than a boundary: an apostrophe in a sentence
    somebody edits later should change the wording, not break the page.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("<", "\\u003c")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'
