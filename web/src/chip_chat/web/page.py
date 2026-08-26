"""The entry flow and the chat widget, as one self-contained page.

Deliberately one file of HTML with inline CSS and inline script, and no build
step. The week-one slice exists to flush out the deployment story, and a
front-end toolchain in the middle of it would be a second deployment story
answering a question nobody asked. Phase 8 (#62) is where the real widget lands.

Three things on this page are requirements rather than decoration, and should
survive whatever replaces it:

**The unaffiliated-demo banner.** This runs on a real restaurant's published
menu and is not affiliated with them. The banner says so above the fold, not in
a footer.

**``noindex``.** The system design asks that the demo never surface on the
brand's own search terms. The meta tag here is half of it; the
``X-Robots-Tag`` header and ``/robots.txt`` in :mod:`chip_chat.api.app` are the
half that works when something fetches the page without executing it.

**The confirm button.** Pressing it is what confirms an order, and the press
goes to the server as a field on the *request*. The model cannot press it and
the prompt cannot talk past it -- see :mod:`chip_chat.agent.orders`.

The stop state has its own page (:func:`stop_page`) because the spend cap can
refuse a visitor on entry, before there is any conversation to put a message in.
"""

from chip_chat.web.copy import (
    BANNER,
    OPENING_MESSAGE,
    STOP_STATE_HEADING,
    SUGGESTIONS,
    TITLE,
)

__all__ = ["chat_page", "stop_page"]

_STYLE = """\
:root { color-scheme: light dark; --bg:#faf7f2; --card:#fff; --ink:#221c15;
  --muted:#6b6257; --line:#e6ded1; --accent:#4a7c2f; --accent-ink:#fff; }
@media (prefers-color-scheme: dark) { :root { --bg:#16130f; --card:#211c17;
  --ink:#f2ece3; --muted:#a89c8c; --line:#332b23; --accent:#7fb85a;
  --accent-ink:#12180c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5
  ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 1rem 1rem 6rem; }
.banner { background:var(--card); border-bottom:1px solid var(--line);
  color:var(--muted); font-size:.8rem; padding:.5rem 1rem; text-align:center; }
h1 { font-size:1.35rem; margin:1.25rem 0 .25rem; }
.sub { color:var(--muted); margin:0 0 1.25rem; font-size:.9rem; }
.msg { border:1px solid var(--line); border-radius:.9rem; padding:.7rem .9rem;
  margin:.55rem 0; background:var(--card); white-space:pre-wrap; }
.msg.you { background:transparent; border-color:transparent; color:var(--muted);
  padding-left:0; }
.msg.you b { color:var(--ink); }
.card { border:1px solid var(--accent); border-radius:.9rem; padding:.9rem;
  margin:.55rem 0; background:var(--card); }
.card table { width:100%; border-collapse:collapse; font-size:.92rem; }
.card td { padding:.15rem 0; }
.card td.n { text-align:right; font-variant-numeric:tabular-nums; }
.card .total { border-top:1px solid var(--line); font-weight:600; }
.card .notice { color:var(--muted); font-size:.8rem; margin-top:.6rem; }
button { font:inherit; border-radius:.6rem; border:1px solid var(--line);
  background:var(--card); color:var(--ink); padding:.45rem .8rem; cursor:pointer; }
button.primary { background:var(--accent); color:var(--accent-ink);
  border-color:var(--accent); margin-top:.7rem; }
button[disabled] { opacity:.5; cursor:default; }
.chips { display:flex; flex-wrap:wrap; gap:.4rem; margin:.6rem 0 0; }
.chips button { font-size:.85rem; color:var(--muted); }
form { position:fixed; left:0; right:0; bottom:0; background:var(--bg);
  border-top:1px solid var(--line); padding:.75rem; }
form .row { max-width:44rem; margin:0 auto; display:flex; gap:.5rem; }
input { flex:1; font:inherit; padding:.6rem .8rem; border-radius:.6rem;
  border:1px solid var(--line); background:var(--card); color:var(--ink); }
.stop { text-align:center; padding:4rem 1rem; }
.stop p { color:var(--muted); }
"""

_SCRIPT = """\
const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const money = n => '$' + Number(n).toFixed(2);

function bubble(text, who) {
  const el = document.createElement('div');
  el.className = 'msg ' + who;
  el.textContent = text;
  log.appendChild(el);
  el.scrollIntoView({block: 'end'});
  return el;
}

function renderCard(card, isReceipt) {
  const el = document.createElement('div');
  el.className = 'card';
  const rows = card.lines.map(l =>
    `<tr><td>${l.quantity} x ${l.name}</td><td class="n">${money(l.line_total)}</td></tr>`
  ).join('');
  el.innerHTML =
    (isReceipt ? `<b>Order ${card.order_id}</b><br>` : '<b>Confirm your order</b><br>') +
    `<span class="notice">${card.store.name} - ${card.store.address}</span>` +
    `<table>${rows}<tr class="total"><td>Total</td>` +
    `<td class="n">${money(card.total)}</td></tr></table>` +
    `<div class="notice">${card.notice}</div>`;
  if (!isReceipt) {
    const go = document.createElement('button');
    go.className = 'primary';
    go.textContent = 'Confirm order';
    go.onclick = () => { go.disabled = true; send('Yes, place it.', card.draft_id); };
    el.appendChild(go);
  }
  log.appendChild(el);
  el.scrollIntoView({block: 'end'});
}

let busy = false;
async function send(text, confirmDraftId) {
  if (busy) return;
  busy = true;
  bubble(text, 'you');
  const thinking = bubble('...', 'them');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, confirm_draft_id: confirmDraftId || null}),
    });
    const data = await res.json();
    thinking.textContent = data.reply;
    if (data.card) renderCard(data.card, data.receipt);
  } catch (err) {
    thinking.textContent = 'Something went wrong reaching the server.';
  } finally {
    busy = false;
    input.focus();
  }
}

form.onsubmit = e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  send(text, null);
};
for (const chip of document.querySelectorAll('.chips button')) {
  chip.onclick = () => send(chip.textContent, null);
}
"""


def _head(title: str) -> str:
    """The parts of the document that are the same on both pages."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{title}</title><style>{_STYLE}</style></head><body>"
        f'<div class="banner">{BANNER}</div>'
    )


def chat_page() -> str:
    """Return the whole chat page: banner, opening message, chips, composer."""
    chips = "".join(f"<button>{prompt}</button>" for prompt in SUGGESTIONS)
    return (
        _head(TITLE)
        + "<main><h1>Cilantro</h1>"
        + f'<p class="sub">{OPENING_MESSAGE}</p>'
        + '<div id="log"></div>'
        + f'<div class="chips">{chips}</div>'
        + "</main>"
        + '<form id="form"><div class="row">'
        + '<input id="input" autocomplete="off" autofocus '
        + 'placeholder="Ask about the menu, your points, or order something">'
        + "<button>Send</button></div></form>"
        + f"<script>{_SCRIPT}</script></body></html>"
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
