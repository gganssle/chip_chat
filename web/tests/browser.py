"""A browser small enough to read, so the page's own script can be tested.

Every other test of :mod:`chip_chat.web.page` is an assertion about a string,
which is the trade the no-build-step decision makes and is a good one while the
requirements are *"the disclosure is above the fold"*. Two of the requirements
are not like that. GitHub #105 -- the greeting drawn three times -- and decision
D9's source line are both about what the script *does* with what the server
sends, and a substring assertion can only ever say that the code which does it
is present.

So this module runs the real script. It implements the members of the DOM the
page actually touches -- eleven of them -- and throws the rest away: a fuller
fake would be a second thing to be wrong about, and the failure mode of a
missing member is a loud ``TypeError`` rather than a quiet wrong answer.

**Node is not a dependency of this repository** and ``make ci`` does not install
one, which is why :data:`available` exists and why every test built on this
skips without it. The structural assertions beside those tests are the floor
that always runs. This is the ceiling: it is the only thing here that can fail
for the reason a visitor would have noticed.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from chip_chat.web import chat_page

__all__ = ["IDS", "available", "run", "script"]

available = shutil.which("node") is not None
"""Whether a JavaScript runtime is on the path."""

IDS = (
    "gate",
    "chat",
    "log",
    "chips",
    "composer",
    "input",
    "photo",
    "gate-form",
    "gate-submit",
    "gate-hint",
    "switch",
    "name",
    "who",
    "role",
)
"""Every id the page's script resolves at load time.

Written out rather than parsed off the document, because a missing one is a
``TypeError`` on the first line of the script and that is the right way to find
out that the markup and the script have come apart.
"""

_DOM = """\
const root = {children: [], parentNode: null};

class El {
  constructor(tag) {
    this.tagName = tag;
    this.className = '';
    this._text = '';
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.files = [];
    this.value = '';
  }
  get textContent() {
    if (this.children.length) {
      return this._text + this.children.map(child => child.textContent).join('');
    }
    return this._text;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === root;
  }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    for (const node of nodes) this.appendChild(node);
  }
  replaceWith(node) {
    const at = this.parentNode.children.indexOf(this);
    node.parentNode = this.parentNode;
    this.parentNode.children[at] = node;
    this.parentNode = null;
  }
  // The waiting indicator takes itself out of the bubble when the first token
  // lands, so the shim has to model the one DOM call that does it. A node with
  // no parent removes to nothing, which is what the browser does too.
  remove() {
    if (!this.parentNode) return;
    const at = this.parentNode.children.indexOf(this);
    if (at >= 0) this.parentNode.children.splice(at, 1);
    this.parentNode = null;
  }
  scrollIntoView() {}
  focus() {}
}

const registry = {};
for (const id of %(ids)s) {
  const node = new El('div');
  node.parentNode = root;
  root.children.push(node);
  registry[id] = node;
}

let reloaded = false;
globalThis.document = {
  getElementById: id => registry[id],
  createElement: tag => new El(tag),
  location: {reload: () => { reloaded = true; }},
};
globalThis.URL = {createObjectURL: () => 'blob:fake'};

// Walk a subtree into something a Python assertion can read: one entry per
// element, with the class it was drawn with and the text it carries.
function drawn(node) {
  const seen = [];
  for (const child of node.children) {
    seen.push({cls: child.className, tag: child.tagName, text: child.textContent,
               hidden: child.hidden, href: child.href || null,
               children: drawn(child)});
  }
  return seen;
}
"""

_HARNESS = """\
%(dom)s
%(prelude)s
%(script)s
%(main)s
"""


def script() -> str:
    """Return the page's inline script, exactly as a browser receives it."""
    page = chat_page()
    return page.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def run(main: str, *, prelude: str = "", tmp_path: Path) -> dict[str, Any]:
    """Execute the page's script, then ``main``, and return the JSON it printed.

    Args:
        main: JavaScript to run after the page's script has loaded. It is
            expected to ``console.log`` one JSON object, which is what comes
            back.
        prelude: JavaScript to run before the page's script -- a ``fetch``
            double, most of the time.
        tmp_path: Where to write the module Node executes.

    Returns:
        The object ``main`` printed.
    """
    source = tmp_path / "page.mjs"
    source.write_text(
        _HARNESS
        % {
            "dom": _DOM % {"ids": json.dumps(list(IDS))},
            "prelude": prelude,
            "script": script(),
            "main": main,
        },
        encoding="utf-8",
    )
    finished = subprocess.run(
        ["node", str(source)], capture_output=True, text=True, timeout=30, check=True
    )
    printed: dict[str, Any] = json.loads(finished.stdout.strip().splitlines()[-1])
    return printed
