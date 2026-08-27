# web

The chat widget and the entry flow — everything a visitor actually looks at.
Issues [#66](https://github.com/gganssle/chip_chat/issues/66)–[#70](https://github.com/gganssle/chip_chat/issues/70),
written up in [docs/public-demo.md](../docs/public-demo.md).

**No build step and no front-end toolchain.** One HTML document with inline CSS
and an inline `<script>`, returned by `chip_chat.api.app`. The argument is in
`page.py`'s docstring and it is the usual one: a bundler here would be a second
deployment artefact, a second cache to invalidate and a second thing to explain,
for a page with one text input and a list of cards.

**The package imports nothing but `chip_chat.otel`.** It used to reach into
`chip_chat.agent.hardcoded`; it no longer knows what a menu, an account or a
model is. It is handed a persona and some text and it renders.

## The map

| Module | Holds |
|---|---|
| `copy.py` | Every sentence the visitor reads, in one place. |
| `page.py` | `chat_page()` and `stop_page()` — the whole document, markup, styles and script. |
| `persona.py` | The `Persona` value object, the opening and restart messages, the archetype grammar, and Chip's four suggestions. |

### Why the copy is a module and not a template

Because several of these strings are **requirements, not wording**, and a string
that is a requirement should be somewhere a test can assert on it.

`BANNER` is [#70]'s launch criterion verbatim — *"Unofficial demo, not affiliated
with Chipotle Mexican Grill. All orders are simulated."* — sticky and not
dismissible. `SIMULATED` is PRD Flow 3's word, and it appears in the action row
of **every** card and **every** receipt. `web/tests/test_page.py` asserts both,
which is why editing them is a deliberate act rather than a copy tweak.

### Why `persona.py` holds no identity

`Persona` is a frozen dataclass with **no `demo_id` field**. It quotes the
narrative from `ACCOUNTS.persona_fixtures` rather than re-deriving it, and
supplies per-archetype framing and invitations plus four lane-tagged `Chip`
suggestions — one each for knowledge, account, personalization and action, so a
visitor's first tap lands somewhere different every time.

The absence of the identifier is the same rule the tool surface enforces:
identity is bound to the Snowflake session by the app, not carried around in the
things that render.

## The six things the page has to do

From `page.py`'s docstring, each one a requirement from a closed issue:

1. The banner is **sticky and not dismissible** (#70).
2. **No borrowed branding.** Slate indigo and warm paper grey; the review is in
   `docs/public-demo.md`.
3. `noindex` in a meta tag. The `X-Robots-Tag` header and `/robots.txt` halves
   live in `chip_chat.api.app` — three places, on purpose.
4. The **confirmation card is the only path to a write** (#68). Editing a card
   re-prices it; only confirm sends the draft id to the server.
5. **"Simulated"** in the action row of every card and receipt.
6. The **persona switcher one tap** from the chat header (#69).

`stop_page()` is a separate document and carries no composer at all, because the
stop state should not look like a chat you could try harder at.

## Running it

Nothing here is executable. `chip_chat.api.app` imports and serves it, so:

```bash
make dev                        # the local stack, page included
uv run pytest web/tests         # 26 tests, mostly requirement assertions
make test                       # or the whole suite
```

Deployed, it is inside the `chip-chat-web` image — `make image`, `make deploy`,
and `docs/deployment.md` for the rest. The live page is at the URL in
[docs/runbook.md](../docs/runbook.md) §1.

## The known defect worth reading before you show anybody

`docs/public-demo.md` records a visible contradiction that is **not** fixed: the
opening message reads the assigned persona while `get_points_balance` reads
`chip_chat.agent.hardcoded.ACCOUNT`, so one conversation can present two stores,
two balances and two usual orders. It is a wiring problem in the agent rather
than a rendering problem here, tracked as `cc-lpy4`, and it should be the first
thing anybody looks at before the link is given to a stranger.
