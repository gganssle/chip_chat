Chip Chat 

 Personal learning proof of concept

# Chip Chat

 A public, playable stand-in for Chipotle's website chatbot — built on real published menu data, fake customer accounts, and a photo-to-order camera, so that Azure AI, Snowflake, and Databricks each do the job they would actually do in production.

 The bot is Cilantro 
 ~5 weeks , evenings and weekends 
 Brain: Azure AI Foundry Agent Service 
 Watched by Arize 
 Open URL, no login 

 The shape of the problem

## Five lanes, five paths

 Cilantro has to field requests that look alike to a visitor and are nothing alike underneath. Separating them is the whole architecture — everything downstream is an implementation detail of this table. These colors reappear in the diagram and in the phase plan, so you can trace one lane end to end.

 Knowledge

 “Is the barbacoa spicy? What's actually in a burrito bowl?”

 Hybrid RAG · Azure AI Search
over the real published menu

 Account

 “How many points do I have? What did I order last time?”

 NL→SQL · Snowflake Cortex Analyst

 Action

 “Reorder my usual, but add extra guac.”

 Tool call → Functions → Snowflake proc
always behind a confirmation card

 Personalization

 “What is my usual? What should I try next?”

 Precomputed nightly · Databricks gold marts

 Vision

 “Here's a photo of what my friend got — make me that.”

 Foundry vision model → menu matcher
feeds the action lane

 Architecture

## Two clocks, two kinds of truth

 Two things distinguish this from a toy. The first is that the fourth lane earns Databricks its place: Snowflake is the governed, low-latency database the agent hits on every single turn, while Databricks is the batch and ML engine that computes overnight what would be far too slow to compute mid-conversation. That division of labor is how the two are genuinely used side by side, and being able to explain it is worth more in your first month at Chipotle than any single line of the code.

 The second is that the menu is real and the accounts are fake , and the pipeline keeps them apart on purpose. Everything Cilantro says about food comes from what Chipotle actually publishes. Everything it says about you comes from a synthetic account minted when you typed your name.

 NIGHTLY — REAL MENU IN, SYNTHETIC ACCOUNTS IN 

 Chipotle public web 
 menu · nutrition · rewards · FAQ 

 Synthetic accounts 
 personas · orders · points 

 ADLS Gen2 
 raw landing zone 

 Databricks 
 bronze → silver → gold 

 Azure AI Search 
 chunked + embedded on ingest 

 Gold marts 
 customer_360 · usual_order 

 Auto Loader 

 publish nightly 

 Snowflake 
 menu catalog (real) · demo accounts · orders · loyalty ledger · gold marts 
 
 row access policy · every query scoped to one visitor's demo id 
 SERVING 

 PER TURN — UNDER TWO SECONDS 

 binds the visitor's demo id 
 — never supplied by the model 

 Visitor 
 types a name 

 Chat app 
 name → persona, no login 
 public URL · rate limited 

 Foundry Agent 
 threads · tool calls 
 OTel spans · daily token cap 

 photo 

 Blob upload 
 Content Safety · 24h TTL 

 blob ref 

 search_menu_knowledge() 
 hybrid + reranker · cites the source page 

 ask_account_question() 
 Cortex Analyst · NL → SQL 

 get_usual_order() 
 gold mart lookup 

 match_meal_from_photo() 
 describes ingredients → menu catalog 

 pre-fills 

 place_order() 
 Functions ops API · visitor confirms first 

 one bound SQL session 

 one span tree per turn 

 ASYNCHRONOUS — OFF THE HOT PATH 

 OpenTelemetry · OpenInference 
 prompt · tool calls · tokens · latency 

 Application Insights 
 is the service healthy 

 Arize 
 is the agent behaving 

 Two details carry most of the design. Every account read and every write runs over a Snowflake session whose demo identity is set by the chat app — the model chooses which tool to call, never whose data it returns. And the vision tool only ever describes ingredients; a deterministic matcher turns that description into real menu SKUs, so Cilantro cannot invent a menu item that doesn't exist. 

 Observability

## Two questions, two backends, one instrumentation

 Application Insights and Arize look like overlapping purchases and aren't. App Insights answers is the service healthy — request rate, container latency, dependency failures, exceptions. Arize answers is the agent behaving — the span tree for a single turn, which tool it reached for and what it got back, tokens burned per span, and evaluation scores attached to live production traces. You need both, and on a public demo you'll reach for the second one far more often.

 They share their plumbing. Foundry's tracing is OpenTelemetry underneath, and Arize consumes OTel spans annotated with the OpenInference semantic conventions — the vocabulary that turns a generic span into this was an LLM call, here was the prompt, here were the tool definitions, here is what came back . So you instrument once and fan out to both exporters. That's not just tidy; it's the property that lets you change observability vendors later by changing an endpoint, which is worth demonstrating deliberately.

 Three things Arize gives you that a batch of offline test questions never will. Online evals: LLM-judges running continuously against real visitor traffic, which matters enormously here because strangers will ask things your golden set never imagined. Trajectory and tool-selection evals: agent-specific scoring of whether the model picked the right lane, which is the single question this entire architecture turns on. And multimodal spans for the photo path, so one trace holds the uploaded image, the structured ingredient description, and the SKUs the matcher landed on — which is the difference between debugging Phase 6 in an afternoon and debugging it for a week.

### Which Arize, though

 Arize AX is transactable through the Azure Marketplace, so it bills against your Azure account and brings the managed monitoring, alerting and online-eval automation. Phoenix is the Apache-2 open-source sibling, self-hostable as a container on Container Apps, using the identical OpenInference instrumentation.

 For this project: run Phoenix locally from week one, because tracing an agent while you build it is how you debug it and paying for that is silly. Then point the exporter at AX when you go public in Phase 8, where online evals against real traffic are the thing that actually earns the money. Switching is a configuration change, which is the whole argument for instrumenting on OTel in the first place. Confirm the current Marketplace listing and the Foundry integration path in Phase 0 — this corner of the ecosystem moves fast.

 Naming

## Call it Cilantro

 Another pepper would read as a Pepper variant rather than a different system, so the pick stays in the ingredient family but changes aisle. Cilantro is warm, obviously food, and unmistakably not the incumbent — and it comes with a built-in joke about the people who hate it, which is the sort of thing a demo bot should have. Any of these work; it's a one-line change.

 Cilantro 
 Tomatillo 
 Queso 
 Adobo 
 Sofrito 

 Sequencing

## Twelve phases

 Estimates assume evenings and weekends. The public URL, the photo feature and the observability plane add about two weeks over a private, text-only build — call it five weeks honestly rather than three optimistically. Each phase names what you should be able to demo at the end of it; if you can't, don't advance.

### Build the ugly slice in week one

 Do not run these phases strictly in order. In your first week, wire one end-to-end path on hardcoded data — one menu question, one account question, one fake order — and put it on the public URL immediately , however embarrassing it looks. It will be the most valuable week of the project, because it forces you through every platform's authentication and deployment story while the scope is still small enough to debug.

 Instrument that slice with OpenInference spans on day one and run Phoenix locally against it. Tracing is not a Phase 9 deliverable you add once things work — it is how you find out why they don't, and retrofitting it into a finished agent is miserable work you can simply avoid.

 0

### Foundation
 ½ weekend 

 Install the az , databricks , and snow CLIs. Resource group, Key Vault, budget alerts, and a domain name for the public demo — buy it now, because DNS and certificate propagation is exactly the kind of thing you don't want to discover on demo day. Everything in Terraform from the start, partly for the learning and mostly so teardown is one command. Scaffold the monorepo: infra/ harvest/ data-gen/ databricks/ snowflake/ agent/ vision/ api/ web/ eval/ otel/ . While you're in the portal, check the current Arize AX listing on the Azure Marketplace and how Foundry currently exports traces, so Phase 7 doesn't surprise you.

 Hold off on the Snowflake trial. It's a 30-day clock and roughly $400 of credits; starting it now means it burns while you're building the lakehouse. Start it at Phase 4.

 Demo: terraform apply stands up an empty but real environment on a real domain.

 1

### Harvest the real menu
 2–3 evenings 

 Pull what Chipotle actually publishes: menu items and descriptions, the nutrition and allergen data behind their calculator, rewards program terms, ordering and refund FAQ, store locator metadata, catering options. Read robots.txt first and honor it; prefer the JSON endpoints their own site calls over parsing HTML, since those are more stable and far lighter; rate-limit yourself to something politely slow; and cache every response to blob storage so you fetch once and iterate offline forever after. Azure Document Intelligence handles any PDF nutrition sheets.

 This phase also decides your action surface . The list of things a customer can genuinely do — build a bowl with these specific modifiers, reorder, add to a group order, redeem this many points for these specific rewards, find a store, ask for a refund — should come from their real ordering flow and rewards terms, not from your imagination. A PoC whose actions mirror the real product is a far better conversation starter than one whose actions you invented.

 knowledge lane action lane Document Intelligence 

 Demo: a menu_catalog table of real items, modifiers, prices, calories and allergens — plus a cached document corpus you never have to re-fetch.

 2

### Synthetic accounts on top of it
 2 evenings 

 Now generate the fake half — 500 customers, 18 months of orders composed only of real catalog items, a loyalty ledger, 30 stores — from a seeded generator so it's reproducible. Give the population real texture: the Tuesday regular who orders the identical bowl, the customer who lapsed four months ago, the office manager placing group orders. These become the personas your public visitors get assigned.

 This is the highest-leverage phase and the one everyone rushes. Thin data produces a chatbot with nothing interesting to say, and no amount of downstream engineering fixes that. Write your evaluation questions here too, while the data shapes are fresh in your head.

 Demo: a query that surfaces a genuinely interesting customer, whose every order is a real menu item.

 3

### Databricks lakehouse
 1 weekend 

 Unity Catalog with bronze / silver / gold schemas and a declarative pipeline carrying both streams — the harvested web corpus gets cleaned, deduplicated and chunked here; the synthetic orders get conformed and aggregated. Gold marts: customer_360 , usual_order , item_affinity , spend_summary . Train a modest item-affinity recommender, track it in MLflow, register it in Unity Catalog. Schedule a weekly re-harvest so the corpus stays current — a real freshness story is worth having.

 personalization lane Unity Catalog MLflow 

 Demo: ask the gold mart what customer 214's usual order is, and be right.

 4

### Snowflake serving layer
 1 weekend 

 Start the trial now. XS warehouse with 60-second auto-suspend, a read-only role for questions and a separate one for writes. Then the piece that matters most: row access policies keyed to a session variable , so a visitor's connection is physically incapable of returning another visitor's rows even if someone talks the model into trying. Build the semantic view Cortex Analyst needs, stored procedures for each write action, and a nightly job that resets demo data so the sandbox stays clean.

 account lane action lane Cortex Analyst RBAC 

 Demo: two browsers, two names, two different correct answers to the same question.

 5

### Retrieval on Azure
 2–3 evenings 

 Harvested corpus into an AI Search index using integrated vectorization, so chunking and embedding happen on ingest. Hybrid retrieval — keyword and vector together — with the semantic reranker on top, returning citations that link back to the real source page. Evaluate the retriever on its own before it ever touches the agent; retrieval bugs are nearly impossible to diagnose once a model is paraphrasing over them.

 knowledge lane Azure AI Search 

 Demo: top-3 recall on your allergen questions, measured, with numbers.

 6

### See it, order it
 1 weekend 

 Upload goes to Blob storage behind size and MIME limits, with EXIF stripped and a 24-hour lifecycle rule, then straight through Content Safety image moderation before anything else touches it. A Foundry vision model returns a structured description — vessel, protein, rice, beans, salsas, toppings, each with a confidence — and a deterministic matcher maps that description onto the real menu_catalog . The model describes; it never names a SKU. That separation is the entire trick, and it's why this can't hallucinate a menu item that doesn't exist.

 Handle the three cases that will actually come up: food that isn't Chipotle at all ( “that's a poke bowl — closest we do is…” ), low confidence (ask a clarifying question rather than guessing), and several meals in one frame. Then hand the match to the normal confirmation card, pre-filled and editable.

 vision lane feeds action Content Safety 

 Demo: 30 labeled photos scored for component-level precision and recall — and a photo of your own lunch turning into a correct order.

 7

### The agent
 1 weekend 

 A Foundry project, a model deployment, and an agent wired to about eleven tools across the five lanes. Two rules worth being strict about: the API mints the Snowflake session from the visitor's server-side session, never from a model-supplied argument; and every write is a two-step, where the agent proposes, the UI renders a structured confirmation card, the visitor clicks, and only then does it execute.

 Wire the OpenInference instrumentation properly here rather than leaving it as a stub: one span tree per turn, with the agent's reasoning, each tool call and its arguments, the retrieved documents, and token counts on every span. Export to Application Insights for the infrastructure view and to Phoenix for the agent view. If you were sloppy about span naming in the week-one slice, fix it now — Phase 9's evaluations attach to these spans, and inconsistent names are what make an eval dashboard useless.

 Foundry Agent Service tool calling OpenInference 

 Demo: “reorder my usual with extra guac” works, shows you what it's about to do first, and the whole turn is one readable trace.

 8

### The public demo
 1 weekend 

 Name gate, then straight into the conversation. The one design decision that makes or breaks this: a visitor with an empty account has nothing to ask. So assign each new name a loaded persona on entry and say so in the opening message — “Hi Sam, I've set you up as a regular at the Ballard store with 1,250 points and a weakness for double barbacoa” — and offer a switcher so they can try a different one. Suggested prompts as clickable chips do more for a cold visitor than any amount of prompt engineering.

 FastAPI on Container Apps, custom domain with a managed certificate, an unaffiliated-demo banner in the header, and noindex so it never surfaces on Chipotle's brand terms.

 Demo: send the URL to someone who knows nothing about the project and watch them use it without narration.

 9

### Evaluation on Arize
 3–4 evenings 

 Promote the golden set from Phase 2 into a versioned dataset , then run every prompt or model change as an experiment against it, so “I tweaked the system prompt and it feels better” becomes a number you can defend. Score groundedness, relevance, and above all tool-selection accuracy — did it choose the right lane — because that single metric is what this whole five-lane architecture exists to get right.

 Then turn on online evals against live traffic. This is the piece that only matters because you went public: real visitors will ask things your golden set never imagined, and continuous LLM-judges on production traces are how you find out. Add monitors for the failure modes you actually fear — ungrounded menu claims, a photo match with no confident SKU, a refusal where the corpus plainly had the answer — and let the interesting traces flow back into the dataset so the golden set grows from real usage rather than your imagination.

 Token counts already ride on the spans, so the cost dashboard falls out of this almost free: tokens per conversation, per lane, per tool. Add Snowflake credits and DBUs alongside for the full unit economics of one conversation, which is a genuinely rare thing to be able to quote.

 Arize AX datasets + experiments online evals 

 Demo: two prompt versions scored against the same dataset, and a live monitor that has already caught something you didn't predict.

 10

### Hardening the open door
 3–4 evenings 

 An unauthenticated public LLM endpoint attached to your own subscription is a genuinely different risk profile, and this phase is not optional. Start with the spend kill switch below. Then per-session and per-IP rate limits, Content Safety and prompt shields on inbound text, and upload abuse handling — oversized files, non-images, disguised payloads, anything Content Safety flags.

 Then attack your own bot, with the traces open in front of you. Plant an injection inside the harvested corpus telling the agent to redeem the reader's points. Ask for another visitor's order history from a second browser. Try to trigger a write without confirmation. Ask an allergen question the corpus genuinely can't answer. Watching an attack land in a span tree teaches you more than reading the eventual denial in the chat window, and each attack you survive becomes a permanent eval rather than a one-off you did once and forgot.

 Demo: a list of attacks that failed, each with the trace showing exactly where it died.

 11

### Write it up
 1 evening 

 A five-minute scripted demo, this architecture diagram, and an honest account of what you'd do differently. This is the phase that converts five weeks of fiddling into something you can walk a new colleague through — don't skip it because the code already works.

 The one that can actually cost you money

## Cap the spend in code

### Budget alerts notify; they don't stop anything

 An open URL with no login means anyone can drive tokens on your subscription, and a single curl loop left running overnight is a real bill. Azure budget alerts will email you about it after the fact — they will not prevent it. So the cap has to live in your application: a running daily token counter that flips the app into a friendly “Cilantro's had a busy day — come back tomorrow” state when it trips, plus per-session and per-IP limits underneath it. Build this in Phase 8, before you send anyone the link — not in Phase 10 when you finally reach the hardening checklist.

 And be clear with yourself that Arize is not this guardrail. Observability is asynchronous and slightly behind; a spend cap has to be enforced inline, in the request path, before the model is called. Arize will tell you beautifully what the damage was. Only your own counter can prevent it.

 | Azure AI Search — start on the free tier. Basic runs roughly $75/month and a PoC corpus doesn't need it. Check the current free-tier semantic-ranker quota before designing around it.

 | Databricks — single-node job clusters, auto-terminate at 10 minutes, and never leave an all-purpose cluster running. This is the most common way people quietly burn a month of credits.

 | Snowflake — XS warehouse, 60-second auto-suspend, and don't start the trial until Phase 4.

 | Container Apps — minimum replicas zero is still right most of the time; the cold start costs a visitor a couple of seconds. Scale to one only while you're actively sharing the link.

 | Blob uploads — a lifecycle rule deleting images after 24 hours keeps storage flat and means you aren't quietly accumulating strangers' photos.

 | Arize — Phoenix self-hosted costs you a small container and nothing else, which is the right way to spend the four weeks before anyone else can reach the URL. Move to AX when public traffic makes online evals worth paying for.

 Learn from other people's mistakes

## Seven ways this goes wrong

 | Thin synthetic data. If every persona looks the same, personalization has nothing to find and the demo has nothing to show. Fix it in Phase 2, not later.

 | Letting the model pick the visitor. If the demo id is a tool argument, one clever prompt reads someone else's session. Bind identity below the model, at the database session.

 | Letting the vision model name menu items. Ask it for SKUs and it will confidently invent a Chipotle product that has never existed. Ask it for ingredients and match them yourself.

 | Text-to-SQL over a raw schema. Cortex Analyst is only as good as the semantic model you hand it. Curating that view is the work; pointing it at bare tables produces confident nonsense.

 | Hand-waving allergens. “Does this contain dairy?” is a safety question, and it's about to be asked by strangers on the open internet. Decide deliberately that Cilantro cites its source and declines to reason past it — then be ready to explain that decision in an interview, because it's exactly the kind of judgment the job is about.

 | Evaluating last. By then you've made a hundred untested choices. Write the golden set in Phase 2 and run it from the first ugly slice onward.

 | Instrumenting last. The same mistake wearing a different hat. Span names and attributes chosen carelessly in week one become the axes of every dashboard and the anchors of every eval — and retrofitting consistent instrumentation into a working agent is dull, invisible work you can avoid entirely by spending twenty minutes on it up front.

 One non-technical note

## Public means the framing matters more

 Using real published menu data is the right call — it's what makes the RAG lane worth building and the action surface believable. But a publicly reachable bot, using a real company's menu, at a URL you hand to strangers, needs the framing to be unmissable rather than buried in a footer.

 So: keep the name clearly distinct from Pepper, put an unofficial demo, not affiliated with Chipotle Mexican Grill, all orders are simulated line on the entry screen and in the chat header, and don't use their logo, wordmark, or brand colors anywhere. Cache the menu data for the demo rather than republishing it as a downloadable dataset, and cite the source pages in answers — which you want to do anyway, for groundedness. Set noindex . And if anyone at Chipotle ever asks you to take it down, take it down cheerfully; you'll be an employee by then, and having built this is the point, not keeping it online.

 Service names and tiers across all three platforms move quickly — verify the current ones during Phase 0 rather than trusting any plan written earlier, this one included.