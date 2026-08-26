Cilantro PRD 

 Product requirements · Chip Chat

# Cilantro

 A conversational assistant that answers questions about the menu, answers questions about your own account, and takes action on it — in one conversation, and from a photograph.

 Status
 Draft, for review

 Owner
 Graham Ganssle

 Updated
 25 August 2026

 Engineering
 RFC-001 · Chip Chat system design

 Milestone
 V0 — public demo

 Framing. Cilantro is a proof of concept. It runs on Chipotle's publicly published menu and nutrition data and on entirely synthetic customer accounts — no real customer data, no real orders, no payment, no fulfillment. Every account in it was generated. It exists to demonstrate a product shape and to exercise an Azure AI, Snowflake, and Databricks stack end to end. This is the only place in the document that says so; everything below is written as the product it is proposing.

 01

## The problem

 Three observations, in order of how much they cost us.

### A customer's question doesn't respect our surface boundaries

 “What's actually in a burrito bowl,” “how many points do I have,” and “get me my usual” feel to a customer like one kind of question. To us they are three systems: a help center, an account page, and an order builder. The customer has to work out which surface they're standing in before they're allowed to ask. Most restaurant assistants solve the first of the three and hand off for the other two — which makes the handoff itself the experience.

### Intent decays at every hop

 Each move from chat to account page to order builder is an opportunity to abandon. The sharpest case is the regular: someone who knows exactly what they want, has ordered it forty times, and still walks through a multi-screen builder to get it. Every screen between intent and confirmation is a screen we are asking them to want it more.

### The highest-intent moment has no input method

 Someone sees a bowl — a colleague's lunch, a photo on their phone — and wants that one . A photograph is the fastest and most precise expression of that intent that exists, and no restaurant chat surface accepts one. We currently ask that customer to translate a picture into a sequence of ingredient checkboxes from memory, which is both slower and less accurate than what they were holding.

 02

## Who this is for

 Three primary personas. They are also, deliberately, the three fixtures a visitor can be assigned in the demo — the product is designed against them and demonstrated through them.

 | The Regular. Orders a near-identical meal most weeks and values speed over everything. Measured by turns-to-reorder, where the target is one.

 | The Lapsed Customer. Hasn't ordered in months and has unredeemed points they have forgotten about. Measured by whether we surface stored value before they think to ask for it.

 | The Explorer. Newer, orders vary, asks real questions about ingredients, calories and allergens. Measured by whether answers are correct, sourced, and appropriately hedged.

 A fourth — the group orderer assembling an office lunch — is out of scope for V0 but shapes the action model, since anything we build for a single order should extend to several.

 03

## Goals

 | One conversation spans menu knowledge, account questions, and account actions, with no handoff to another surface.

 | Every claim about food is grounded in published data and carries a citation.

 | A returning customer can reorder in a single turn.

 | A photograph of a meal becomes an editable order in a single turn.

 | No action is ever taken on an account without an explicit confirmation step.

 | The assistant knows the boundary of what it knows, and says so rather than inferring across it.

 04

## Non-goals

 | Payment, fulfillment, or delivery. Orders are simulated and labeled as such.

 | Authentication and real accounts. A visitor types a name; there is nothing to protect and nothing to log into.

 | Replacing the ordering interface. This augments it. Anything faster in the builder should stay in the builder.

 | Nutrition, dietary, or medical advice. Cilantro reports what is published and declines to reason past it. This is a deliberate boundary, not a gap.

 | Voice, additional languages, and group ordering. V1 and beyond.

 | Anything operational. Staffing, inventory, and store systems are out of scope entirely.

 05

## How we'll know it worked

 Two of these are gates rather than targets: they are pass or fail, and a failure blocks the demo going public regardless of how everything else scores.

### Launch gates

 Zero cross-visitor data disclosures across the full adversarial suite. Not “few” — zero. One visitor seeing another's order history is a total product failure, and the mechanism that prevents it is structural rather than statistical, so anything above zero indicates a broken mechanism rather than a bad day.

 Zero account writes executed without explicit confirmation. Same reasoning. An order the customer did not confirm is not a bug, it is the thing the product is not allowed to do.

 | Metric | Target | Measured by 

 | Task completion on the golden set | ≥ 85% | Arize experiment 

 | Tool-selection accuracy — did it pick the right lane | ≥ 95% | Arize trajectory eval 

 | Groundedness of food and policy claims | ≥ 0.95 | Arize online eval 

 | Menu claims made without a citation | 0 | Arize online eval 

 | Photo → order, component-level F1 | ≥ 0.80 | Labeled photo set 

 | Photo orders confirmed with one edit or fewer | ≥ 70% | Product analytics 

 | Turns to reorder, for the Regular persona | 1 | Product analytics 

 | Median turn latency | < 2 s | Application Insights 

 | 95th percentile turn latency | < 4 s | Application Insights 

 | Cost per conversation | < $0.05 | Arize + cost dashboard 

 06

## The cold start is the product risk

 The single largest threat to this demo is not accuracy. It is that a visitor types their name, arrives at an empty account, asks the only question that occurs to them, and is told they have zero points and no order history. There is nothing wrong with any component in that exchange and the visitor leaves anyway.

 So the entry flow assigns each new visitor a fully populated persona and — critically — tells them who they've become in the opening message. That single line converts a blank prompt into a menu of things worth asking.

 WITHOUT A PERSONA 

 Types a name 

 Empty account 
 no history, no points 

 Asks the one 
 obvious question 

 “You have 0 points.” 
 nothing to come back for 

 WITH AN ASSIGNED PERSONA 

 Types a name 

 Persona assigned 
 18 months · 1,250 pts 

 Opening line 
 says who they are 

 Asks something real 
 and gets a real answer 

 The same four steps and the same technology. The only difference is whether the account had anything in it and whether the assistant said so unprompted — which is the difference between a demo that gets played with and one that gets closed. 

 07

## Requirements

 Grouped by capability. Colors match the lanes in the system design and the RFC.

### Entry
 E 

 E1
 A visitor provides a first name and nothing else, and is in the conversation within one screen.

 E2
 On entry, the visitor is assigned a synthetic persona with order history, a home store, and a points balance.

 E3
 The opening message states which persona they've been given — store, points, and a characteristic order — so the visitor knows what is worth asking.

 E4
 Suggested opening prompts are offered as tappable chips, spanning at least three different capabilities.

 E5
 The visitor can switch to a different persona at any time. Switching starts a fresh conversation and says so.

 E6
 Every screen carries a visible notice that this is an unaffiliated demo and that orders are simulated.

### Menu knowledge
 K 

 K1
 Answers questions about menu items, ingredients, nutrition, allergens, the rewards programme, and ordering policy, from published data.

 K2
 Every food or policy claim carries a citation to the source page it came from.

 K3
 When the published data does not contain an answer, says so plainly rather than inferring — and does this unconditionally for allergen and dietary questions.

 K4
 Handles comparative and constrained questions: which option has fewer calories, what is vegetarian, what can be made without dairy according to the published data.

### Account
 A 

 A1
 Answers questions about the visitor's own order history, spend, points balance, and store visits.

 A2
 Handles aggregates and time ranges: what they spent this year, what they order most, when they last visited a given store.

 A3
 Never returns another visitor's data, under any phrasing, including instructions embedded in a document or a photo.

 A4
 When an account question cannot be answered reliably, says so rather than producing a plausible number.

### Personalization
 P 

 P1
 Can state the visitor's usual order and briefly how it worked that out.

 P2
 Can recommend items the visitor has not tried, grounded in their actual ordering behaviour rather than generic popularity.

 P3
 Surfaces unredeemed value — points, available rewards — without being asked, when the persona has meaningful stored value.

### Action
 T 

 T1
 Supports: place an order, reorder the usual, modify a proposed order, cancel a pending order, redeem points, and update stated preferences.

 T2
 Every action renders a structured confirmation card showing exactly what will happen, before it happens.

 T3
 The card is editable in place — items and modifiers can be changed without restarting the conversation.

 T4
 Every completed action returns a receipt the visitor can refer back to later in the conversation.

 T5
 Actions are simulated, and the confirmation card says so.

### Vision
 V 

 V1
 Accepts a photo upload inline in the conversation, on desktop and mobile.

 V2
 Returns a proposed order matching the photo, composed only of items that exist on the real menu.

 V3
 States what it believes it saw, in the visitor's language, so they can correct it before confirming.

 V4
 When the photo is not Chipotle-style food, says so and offers the closest thing that is available.

 V5
 When confidence on any component is low, asks a clarifying question rather than guessing.

 V6
 Never names a menu item that does not exist.

### Trust & safety
 S 

 S1
 Content moderation runs on inbound text and on every uploaded image before anything else processes it.

 S2
 Instructions embedded in retrieved documents are treated as data, never as direction.

 S3
 Rate limited per session and per source address.

 S4
 When the daily budget is exhausted, degrades to a friendly limit message rather than an error.

 08

## Key flows

 Written as the actual copy, because in a conversational product the copy is the interface and reviewing it in the abstract reviews nothing.

#### Flow 1 — Cold entry

 Visitor
 Sam

 Cilantro
 Hi Sam. I've set you up as a regular at the Ballard store — eighteen months of orders, 1,250 points, and a well-documented weakness for double barbacoa. Ask me anything about the menu, about your account, or send me a photo of something you want.

 Chips
 “What's my usual?” · “How many points do I have?” · “Is the barbacoa spicy?” · “Switch persona”

#### Flow 2 — A menu question, answered with a source

 Visitor
 is the barbacoa spicy

 Cilantro
 Moderately. It's braised with chipotle chiles and cumin, so it carries more heat than the carnitas but less than the hot salsa. If you want to dial it up, the tomatillo-red chili salsa is the hottest thing on the line. — Menu · Barbacoa 

#### Flow 3 — Reorder in one turn

 Visitor
 usual but add guac

 Cilantro
 Here's your usual with guacamole added. Anything else before I place it?

 Card
 BURRITO BOWL — Ballard
double barbacoa · white rice · black beans
mild salsa · cheese · + guacamole 
——
$13.85 · 1,250 pts available
[ Edit ] [ Place order ] · simulated

#### Flow 4 — A photo becomes an order

 Visitor
 uploads a photo of a burrito bowl

 Cilantro
 Looks like a bowl with chicken, white rice, black beans, corn salsa, cheese and guac. I've built that below — the protein was the least certain call, so change it if I've got it wrong.

 Card
 BURRITO BOWL — Ballard
chicken · white rice · black beans
corn salsa · cheese · guacamole
——
$12.40 · matched from your photo
[ Edit ] [ Place order ] · simulated

 09

## Constraints and assumptions

 | Menu knowledge is bounded by what is published. If Chipotle doesn't publish it, Cilantro doesn't know it, and should say so rather than reason toward it.

 | Account realism is bounded by the generator. Behaviour that the synthetic population doesn't exhibit cannot be demonstrated, which makes the generator a product decision rather than a technical one.

 | Anyone with the link is a visitor. There is no gate, so the experience has to be legible to someone arriving with no context whatsoever.

 | Spend is capped in the application. A hard daily ceiling exists and visitors can hit it; the product must handle that state gracefully rather than treat it as an error.

 | English, one region, one currency. 

 10

## Risks

 | Risk | Mitigation 

 | Assistant invents a menu item that has never existed | The vision model describes ingredients only; a deterministic matcher resolves them against the real catalogue 

 | Allergen answer is wrong or over-confident | Cite published data, decline to reason past it, and treat this as a launch-blocking eval rather than a nice-to-have 

 | Open endpoint is abused to run up cost | Inline per-request budget check, per-session and per-IP limits, hard daily ceiling with a friendly stop state 

 | Persona assignment feels arbitrary or confusing | State it explicitly in the opening message and make switching one tap 

 | Account questions get plausible but wrong answers | Bounded semantic model, and an explicit “I can't answer that reliably” path rather than a best guess 

 | Instructions hidden in harvested documents redirect the assistant | Prompt shields, retrieved content treated strictly as data, and a standing adversarial suite 

 | Visitor uploads something that isn't food | Content moderation on every image before processing, then a graceful “that doesn't look like lunch” 

 11

## Open questions

 Genuinely open — each one changes what gets built, and none has an obvious default.

 Q1
 
 Does a visitor's state persist between visits? 
 A cookie means Sam comes back tomorrow to the order they placed today, which is a much better story. It also means demo data accumulates and the nightly reset has to become more careful. The alternative — a fresh persona every visit — is simpler and slightly colder.

 Q2
 
 Can visitors edit their persona, or only switch between fixtures? 
 Editing makes it feel like an account. It also lets a visitor construct a state the gold marts were never computed against, at which point personalization quietly degrades.

 Q3
 
 Do citations show inline or on demand? 
 Inline is more trustworthy and noisier. On demand is cleaner and easier to miss. Given that groundedness is one of the headline metrics, there's an argument that visitors should see the receipts by default.

 Q4
 
 Does V0 handle several meals in one photograph? 
 A photo of a table with four bowls is a plausible thing for someone to send. Supporting it well means a multi-order flow the action model doesn't have yet; not supporting it means detecting the case and saying so.

 12

## Launch criteria for V0

 | Both launch gates pass on the full adversarial suite.

 | Every requirement above is either met or explicitly deferred with a note.

 | The golden set clears its targets in an Arize experiment, and the result is recorded.

 | Online evals and cost monitors are live before the URL is shared, not after.

 | The daily spend ceiling has been tested by actually tripping it.

 | A stranger completes a menu question, an account question, and an order without narration.

 | The unaffiliated-demo notice appears on entry and persists in the chat header.

 Related documents: the system design and build plan, and RFC-001 covering the engineering design. Requirement identifiers in this document are referenced directly by the RFC's tool contracts and test plan.