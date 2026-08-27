-- The account lane's semantic view. Curating it IS the work (#45).
--
-- Cortex Analyst is only as good as the model handed to it, and pointing it at
-- bare tables produces confident nonsense -- system-design.md's fourth trap.
-- What follows is therefore a deliberately NARROW description of five tables
-- out of fourteen, with a business name and a synonym list on every field a
-- visitor says out loud, and with seven verified queries so that the questions
-- people actually ask are not re-derived from scratch on every turn.
--
-- A NATIVE SEMANTIC VIEW, not a YAML file on a stage. docs/service-inventory.md
-- finding 8: `CREATE SEMANTIC VIEW` objects are the recommended form, with real
-- RBAC and sharing, and stage-hosted semantic-model YAML is backward
-- compatibility only. An older tutorial will send you to the stage; do not go.
--
-- ---------------------------------------------------------------------------
-- WHAT IS IN IT, AND WHY THAT IS THE SHORT LIST
-- ---------------------------------------------------------------------------
--
-- PRD A1 and A2 fix the scope: the visitor's own order history, spend, points
-- balance and store visits, with aggregates and time ranges over them. Five
-- logical tables answer all of that:
--
--   orders        ACCOUNTS.orders          what they bought and when
--   order_lines   ACCOUNTS.order_items     what was in it
--   points        ACCOUNTS.loyalty_ledger  every movement of points
--   items         CATALOGUE.menu_items     the NAME of the thing ordered
--   restaurants   CATALOGUE.stores         the NAME of the place
--
-- The two catalogue tables are here to name things and for nothing else. A
-- visitor asks "when did I last go to the Ballard store", not "when did I last
-- order at 2118", so `stores` earns its place; `menu_items` is in for the same
-- reason and exposes three of its nine columns.
--
-- NINE TABLES ARE DELIBERATELY OUT, and the omissions are the design:
--
--   menu_items.calories, .allergens, .allergen_disclosure
--                       The knowledge lane answers those from the published
--                       chart WITH a citation (PRD K3). Exposed here, "how many
--                       calories have I eaten this year" becomes a sum over a
--                       column that describes a default build of an item nobody
--                       ordered by default -- a number that is plausible,
--                       arithmetically sound and false. The golden set names
--                       that question as one the account lane must REFUSE
--                       (`a4-unanswerable-aggregate`), and the refusal is a
--                       property of this file rather than of a prompt.
--   item_prices         What an item costs today is a menu question. An order
--                       already carries the price it was actually charged, in
--                       order_items.unit_price, so joining a live price list to
--                       history is a way to answer "what did I spend" with a
--                       number the visitor was never charged.
--   modifiers           The build of a line is order_items.modifiers, an array
--                       of identifiers. Nothing in scope aggregates over it,
--                       and a text-to-SQL system given an array column will
--                       eventually FLATTEN it into a join nobody wanted.
--   demo_visitors       Where the three visitor-editable fields live. It is
--                       also the only table in ACCOUNTS that ties a demo_id to
--                       a person, and this view has no business naming one.
--   personas,
--   persona_fixtures    A kind of person, and the roster. Neither is a fact
--                       about the visitor in front of you.
--   customer_360,
--   usual_order,
--   item_affinity,
--   spend_summary       The four gold marts, and their absence is the sharpest
--                       line here. They are computed overnight and RFC-001 §10
--                       requires a stale mart to be served WITH its derived_at
--                       rather than silently as fresh. A generated query cannot
--                       be relied on to carry that timestamp into an answer,
--                       and a row that mixes last night's lifetime_spend with a
--                       live COUNT(*) is stale and fresh in the same sentence.
--                       The personalization lane reaches them through
--                       get_usual_order and get_recommendations, which are
--                       tools that know to say when the number was computed.
--
-- The other half of the same argument: spend_summary would answer "what have I
-- spent this year" too, and it would sometimes disagree with `orders` by a day.
-- One question, one path.
--
-- ---------------------------------------------------------------------------
-- demo_id APPEARS NOWHERE, WHICH IS ISSUE #45's FIFTH CRITERION
-- ---------------------------------------------------------------------------
--
-- Not as a dimension, not as a fact, not in a relationship, and not in the
-- WHERE clause of a single verified query. Isolation is #43's row access
-- policies, attached to orders, order_items and loyalty_ledger and filtered
-- against a session variable. Snowflake enforces a policy on a base table when
-- that table is reached through a semantic view -- and Cortex Analyst generates
-- SQL against the PHYSICAL tables, so the policy is on the path either way.
--
-- The consequence is worth stating plainly, because it looks like a bug: every
-- query below is written as though the visitor were the only person in the
-- database. `SELECT SUM(delta) FROM loyalty_ledger` with no predicate is this
-- visitor's balance, because the session cannot see another visitor's rows. A
-- generated query that helpfully added `WHERE demo_id = ...` would need a
-- visitor identifier to put in it, and RFC-001 §06's whole point is that no
-- tool signature has one. The AI_SQL_GENERATION instruction below says so in
-- the model's own words.
--
-- ---------------------------------------------------------------------------
-- SETTLED ORDERS ONLY, AND WHY THAT IS BAKED INTO THE FACTS
-- ---------------------------------------------------------------------------
--
-- orders.status is COMPLETED, CANCELLED or REFUNDED. A cancelled order never
-- happened; a refunded one had its money returned. Neither is spend, and
-- databricks/src/chip_chat/databricks/gold.py applies exactly that rule to
-- every mart, so a semantic view that counted differently would put two numbers
-- called `lifetime_spend` in the same conversation.
--
-- The rule is therefore a FACT -- `settled_spend`, `settled_order`,
-- `settled_at` are zero or null on an unsettled row -- and every money metric
-- aggregates the fact rather than the raw column. A generated query cannot
-- forget the filter, because there is no unfiltered metric to reach for.
-- orders.order_status stays a dimension, so "was one of my orders cancelled" is
-- still answerable.
--
-- ---------------------------------------------------------------------------
-- COPY GRANTS IS LOAD-BEARING. DO NOT DROP IT
-- ---------------------------------------------------------------------------
--
-- CREATE OR REPLACE drops the grants on the object it replaces, and a future
-- grant does not re-apply to a replaced object. Without COPY GRANTS, every
-- routine `make snowflake-apply` would silently revoke CHIP_CHAT_READ's SELECT
-- on this view and the account lane would go dark until somebody re-ran a
-- grants file. `test_semantic_view.py` fails if the words go missing.
--
-- The explicit GRANT at the foot of the file is the other half: COPY GRANTS
-- preserves what is there, and on the first apply after a `--reset` there is
-- nothing to preserve.
--
-- ---------------------------------------------------------------------------
-- CROSS-REGION INFERENCE
-- ---------------------------------------------------------------------------
--
-- This account is AWS us-east-2, where Cortex Analyst is NOT native (GitHub
-- #104: the region was fixed at signup and kept). It runs by cross-region
-- inference, which sends the inference out of the region and adds latency to
-- every turn of the account lane. The account already had
-- CORTEX_ENABLED_CROSS_REGION = ANY_REGION when this landed; the statement
-- below narrows it to AWS_US, which is what Snowflake recommends for an AWS US
-- account and is the smallest setting under which this view works at all.
-- Narrowing is what an apply is allowed to do; see apply.py's asymmetry.

USE ROLE ACCOUNTADMIN;

ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'AWS_US';

-- Granted to PUBLIC by default, and re-asserted here because "by default" is a
-- thing an account administrator can revoke, and the failure it would produce
-- is a lane that stops answering rather than an error anybody would connect to
-- a grant. Cortex Analyst needs it on the role that asks the question.
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE CHIP_CHAT_READ;

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

CREATE OR REPLACE SEMANTIC VIEW ACCOUNT_LANE

    TABLES (
        orders AS CHIP_CHAT.ACCOUNTS.orders
            PRIMARY KEY (order_id)
            WITH SYNONYMS = ('orders', 'my orders', 'order history', 'past orders', 'receipts', 'visits', 'times i came in')
            COMMENT = 'One row per order this visitor placed, over the last eighteen months. A visit and an order are the same event here: there is no walk-in that did not buy anything. Spend and counts over this table are settled orders only -- see settled_spend.',

        order_lines AS CHIP_CHAT.ACCOUNTS.order_items
            PRIMARY KEY (order_id, line_number)
            WITH SYNONYMS = ('order lines', 'what was in the order', 'items on my order', 'line items', 'what i got')
            COMMENT = 'The lines of an order: one row per item on one order. Two of the same item built differently are two lines rather than a quantity of two, so counting lines is not the same as counting items -- use the quantity fact.',

        points AS CHIP_CHAT.ACCOUNTS.loyalty_ledger
            PRIMARY KEY (entry_id)
            WITH SYNONYMS = ('points', 'rewards', 'loyalty', 'my points', 'points history', 'rewards account')
            COMMENT = 'Every movement of loyalty points and what moved it: earning on an order, redeeming a reward, a signup bonus. A balance is the sum of the movements and never a stored number, which is why the balance metric adds this table up rather than reading one.',

        items AS CHIP_CHAT.CATALOGUE.menu_items
            PRIMARY KEY (item_id)
            WITH SYNONYMS = ('items', 'menu items', 'dishes', 'food', 'what i ordered', 'products')
            COMMENT = 'The published menu, present so that an order line can be called a Steak Burrito rather than CMG-2. IDENTITY ONLY: this model exposes the name and the category and nothing else. It carries no calories, no allergens, no ingredients and no prices, and questions about any of those cannot be answered from here.',

        restaurants AS CHIP_CHAT.CATALOGUE.stores
            PRIMARY KEY (store_id)
            WITH SYNONYMS = ('stores', 'restaurants', 'locations', 'shops', 'the place i go', 'branches')
            COMMENT = 'The restaurants in the demo, present so that a store can be named and not merely numbered. A visitor asks about the store by its name or its town, which is what the two dimensions here are for.'
    )

    RELATIONSHIPS (
        lines_to_order AS order_lines (order_id) REFERENCES orders,
        lines_to_item AS order_lines (item_id) REFERENCES items,
        orders_to_restaurant AS orders (store_id) REFERENCES restaurants
    )

    FACTS (
        orders.order_total AS orders.total
            WITH SYNONYMS = ('order total', 'what that order came to', 'basket total')
            COMMENT = 'What one order came to, in US dollars, whatever its outcome. Present so that a single order can be quoted; do not sum it. settled_spend is the column every money metric adds up.',

        orders.settled_spend AS IFF(orders.status = 'COMPLETED', orders.total, 0)
            WITH SYNONYMS = ('spend on an order', 'money actually spent')
            COMMENT = 'An order total where the order settled, and zero where it was cancelled or refunded. The settled rule lives in this expression so that no aggregate can be written without it: a cancelled order never happened and a refunded one had its money returned, and the gold marts count the same way.',

        orders.settled_order AS IFF(orders.status = 'COMPLETED', 1, 0)
            WITH SYNONYMS = ('a completed order', 'an order that went through')
            COMMENT = 'One for a settled order and zero otherwise. Summed rather than counted, for the same reason as settled_spend: COUNT would silently include the orders that did not happen.',

        orders.settled_at AS IFF(orders.status = 'COMPLETED', orders.placed_at, NULL)
            WITH SYNONYMS = ('when the order actually happened')
            COMMENT = 'When a settled order was placed, and null where the order did not settle. What first_order_at and last_order_at are taken over, so a cancelled order cannot become the answer to when the visitor last came in.',

        order_lines.quantity AS order_lines.qty
            WITH SYNONYMS = ('how many', 'quantity', 'number of them')
            COMMENT = 'How many of that item on this line. Two of the same item built differently are two lines rather than a quantity of two, so summing this is the count of items and counting rows is not.',

        order_lines.line_spend AS order_lines.line_total
            WITH SYNONYMS = ('line total', 'what that item came to')
            COMMENT = 'Quantity times the unit price charged, plus every modifier the line carried, in US dollars. The price actually charged at the time, not a price read off today''s menu.',

        points.point_change AS points.delta
            WITH SYNONYMS = ('points moved', 'points earned or spent', 'change in points')
            COMMENT = 'Signed points on one movement: positive earns, negative redeems or expires. Summing it is what a balance is.'
    )

    DIMENSIONS (
        orders.order_id AS orders.order_id
            WITH SYNONYMS = ('order number', 'order id', 'receipt number')
            COMMENT = 'The identifier of one order, spelled ord-0000001. What an order line belongs to.',

        orders.ordered_at AS orders.placed_at
            WITH SYNONYMS = ('when i ordered', 'order time', 'date of the order', 'when i came in', 'when i went')
            COMMENT = 'When the order was placed, UTC. Every timestamp in this database is UTC and carries no zone: there is one clock in this system and a local time is a rendering decision made later.',

        orders.ordered_on AS CAST(orders.placed_at AS DATE)
            WITH SYNONYMS = ('day i ordered', 'order date', 'what day')
            COMMENT = 'The calendar day of the order, UTC. Present so that a question about days does not have to cast a timestamp in a generated query.',

        orders.order_month AS DATE_TRUNC('MONTH', orders.placed_at)
            WITH SYNONYMS = ('month', 'by month', 'each month', 'monthly')
            COMMENT = 'The first day of the month the order was placed in. The grain of every month-by-month answer, and the one that matches how spend is usually asked about.',

        orders.order_year AS YEAR(orders.placed_at)
            WITH SYNONYMS = ('year', 'this year', 'last year', 'by year')
            COMMENT = 'The calendar year the order was placed in, as a number. Compare against YEAR(CURRENT_DATE()) for this year rather than assuming the history runs up to today.',

        orders.order_status AS orders.status
            WITH SYNONYMS = ('status', 'outcome', 'cancelled', 'refunded', 'completed', 'did it go through')
            COMMENT = 'The outcome of the order: COMPLETED, CANCELLED or REFUNDED. Here so that a visitor can ask whether something was cancelled. It is NOT how spend is filtered -- the settled facts do that -- and an answer that filters on it by hand will disagree with the metrics.',

        orders.order_channel AS orders.channel
            WITH SYNONYMS = ('channel', 'delivery', 'in store', 'pickup', 'how i ordered')
            COMMENT = 'IN_STORE or DELIVERY, which is also which of the two published price lists priced the order. Chipotle publishes both and they differ by nearly twenty percent, so a total is unexplainable without it.',

        order_lines.line_number AS order_lines.line_number
            WITH SYNONYMS = ('line', 'line number')
            COMMENT = 'Which line of the order, from one. With the order id it is what one line is; two burritos built differently are lines one and two.',

        items.item_name AS items.name
            WITH SYNONYMS = ('item', 'item name', 'dish', 'what it is called', 'the food', 'burrito', 'bowl')
            COMMENT = 'The item as Chipotle publishes it, e.g. Steak Burrito. What an answer should call the thing back, and what a visitor names when they ask about one.',

        items.item_category AS items.category
            WITH SYNONYMS = ('category', 'kind of item', 'entree', 'side', 'drink', 'type of food')
            COMMENT = 'The published top-level category: Entree, Side, Drink or Non Food Items, and null where the menu publishes none. Entree is the category that marks a meal rather than an addition to one.',

        restaurants.store_name AS restaurants.name
            WITH SYNONYMS = ('store', 'store name', 'restaurant', 'location', 'which store', 'the branch')
            COMMENT = 'The name the company uses for the restaurant, and null where the locator publishes none -- an answer then names the town instead of inventing a name.',

        restaurants.store_city AS restaurants.city
            WITH SYNONYMS = ('city', 'town', 'where', 'neighbourhood', 'area')
            COMMENT = 'The town the restaurant is in, as published. Half of the population asks about a store by its town rather than by its name.',

        restaurants.store_region AS restaurants.region
            WITH SYNONYMS = ('state', 'region', 'which state')
            COMMENT = 'The state or territory code, e.g. CA. The coarsest way a visitor locates a restaurant.',

        points.movement_reason AS points.reason
            WITH SYNONYMS = ('reason', 'why the points moved', 'earned', 'redeemed', 'bonus')
            COMMENT = 'What moved the points: ORDER for points earned on an order, REWARD_REDEEMED for a redemption, SIGNUP_BONUS for an opening balance. The rates behind them come from the published rewards terms.',

        points.reward_name AS points.reward_name
            WITH SYNONYMS = ('reward', 'what i redeemed', 'free item', 'which reward')
            COMMENT = 'The published reward a redemption was spent on, verbatim, and null on every movement that is not a redemption. A cost is not an identity: two rewards can be priced the same.',

        points.moved_at AS points.created_at
            WITH SYNONYMS = ('when the points moved', 'when i earned them', 'when i redeemed')
            COMMENT = 'When the points movement happened, UTC. The time dimension of every question about points over a period.'
    )

    METRICS (
        orders.total_spend AS SUM(orders.settled_spend)
            WITH SYNONYMS = ('spend', 'total spend', 'how much have i spent', 'money spent', 'what have i spent', 'how much did i spend')
            COMMENT = 'What the visitor has actually spent, in US dollars, over whatever period and filters the question implies. Settled orders only.',

        orders.order_count AS SUM(orders.settled_order)
            WITH SYNONYMS = ('orders', 'how many orders', 'number of orders', 'how many times', 'visits', 'how often')
            COMMENT = 'How many settled orders. Also the answer to how many times the visitor came in, because an order is a visit in this data.',

        orders.average_order_value AS SUM(orders.settled_spend) / NULLIF(SUM(orders.settled_order), 0)
            WITH SYNONYMS = ('average order', 'average spend', 'typical order', 'average basket', 'how much do i usually spend')
            COMMENT = 'Spend divided by settled orders. Written as a ratio of the two settled facts rather than as an average of a column, so that a cancelled order neither adds a zero to the numerator nor a row to the denominator.',

        orders.first_order_at AS MIN(orders.settled_at)
            WITH SYNONYMS = ('first order', 'when did i start', 'my first visit', 'how long have i been coming')
            COMMENT = 'The earliest settled order in whatever the question scopes to. The start of the history, which is about eighteen months back.',

        orders.last_order_at AS MAX(orders.settled_at)
            WITH SYNONYMS = ('last order', 'when did i last order', 'most recent order', 'when was i last there', 'last visit', 'when did i last go')
            COMMENT = 'The most recent settled order in whatever the question scopes to. With a store filter this is when the visitor last went to that restaurant.',

        order_lines.items_ordered USING (lines_to_order) AS SUM(order_lines.quantity * orders.settled_order)
            WITH SYNONYMS = ('how many items', 'how many of them', 'times i ordered it', 'how many burritos', 'how often do i order')
            COMMENT = 'How many of an item the visitor has ordered, on settled orders. Reaches the order through lines_to_order to apply the settled rule, because a line carries no status of its own.',

        order_lines.item_spend USING (lines_to_order) AS SUM(order_lines.line_spend * orders.settled_order)
            WITH SYNONYMS = ('spent on an item', 'how much have i spent on', 'money on that item')
            COMMENT = 'What the visitor has spent on an item, on settled orders, at the prices actually charged. Summing this across every item equals total_spend.',

        points.points_balance AS SUM(points.point_change)
            WITH SYNONYMS = ('points', 'points balance', 'how many points do i have', 'my balance', 'rewards balance', 'how much have i got')
            COMMENT = 'The loyalty points balance: every movement added up. Earning is positive and redeeming is negative, so the sum over the whole ledger is what is available now.',

        points.points_earned AS SUM(IFF(points.delta > 0, points.delta, 0))
            WITH SYNONYMS = ('points earned', 'how many points have i earned', 'points i got')
            COMMENT = 'Points earned, over whatever period the question implies. Redemptions and expiries are excluded rather than netted off.',

        points.points_redeemed AS SUM(IFF(points.delta < 0, -points.delta, 0))
            WITH SYNONYMS = ('points redeemed', 'points spent', 'how many points have i used')
            COMMENT = 'Points spent on rewards, as a positive number, over whatever period the question implies.'
    )

    COMMENT = 'The account lane, and only the account lane: this visitor''s own order history, spend, loyalty points and store visits, over about eighteen months. Five tables out of fourteen and three columns out of nine on the menu, on purpose -- a narrow model that refuses is worth more here than a wide one that guesses, and PRD A4 makes the refusal a requirement rather than a preference. Nothing here knows about calories, allergens, ingredients, menu prices, another visitor, or what this one should order next. Isolation is not in this model at all: it is a row access policy on the base tables, so every query below is written as though this visitor were the only person in the database.'

    AI_SQL_GENERATION 'Spend and order counts are settled orders only -- status COMPLETED. Use the settled facts and the metrics built on them; never sum orders.total directly, because a cancelled order never happened and a refunded one had its money returned. NEVER filter on demo_id and never put a visitor identifier in a query: the session is already bound to one visitor by a row access policy, so an unfiltered query already returns only this visitor''s rows and a filtered one is a mistake. Every timestamp is UTC and money is US dollars. Prices on an order line are what was charged at the time; there is no current price list in this model. When a question asks about a store by name or by town, join through orders_to_restaurant and match with ILIKE on a fragment of store_name or store_city -- never with equality. The published name appends a suffix a visitor will not say, so store_name = ''NH Town 1'' matches nothing while store_name ILIKE ''%NH Town 1%'' matches the restaurant they meant. The same for an item: match item_name with ILIKE on a fragment. And a query that returns no rows is an answer -- it means the visitor has never ordered that -- so do not widen a filter to make one appear.'

    AI_QUESTION_CATEGORIZATION 'Answer only questions about this visitor''s own orders, spend, loyalty points and store visits that can be computed from the tables in this model. Treat as unanswerable, and say so rather than guessing: anything about calories, nutrition, allergens or ingredients; what an item costs on the menu today; any other visitor or any population average; what this visitor should order next or what they usually order; the visitor''s name or their stated preferences; and anything in the future. A plausible number is worse than no number here.'

    -- WRITTEN AGAINST THE LOGICAL MODEL, NOT THE TABLES, and that is not a
    -- style choice. A verified query whose SQL names a physical table is
    -- accepted by CREATE, then rewritten by Cortex Analyst into a CTE per
    -- logical table -- and the rewrite projects only the columns it thinks the
    -- query needs, so `SELECT s.name ... FROM __restaurants AS s` compiles
    -- against a CTE that selected store_id and nothing else. The response then
    -- carries, in its warnings, "these queries are removed from the semantic
    -- model (i.e. ignored)". They are ignored SILENTLY as far as the answer is
    -- concerned: the request still succeeds and still reports the verified
    -- query as used. Measured on this account, 2026-08-27.
    --
    -- So each query below names `__<logical table>` and the element names
    -- declared above -- `point_change`, not `delta`; `settled_at`, not
    -- `placed_at`. The settled rule comes with the facts, which is why none of
    -- these has a status predicate in it, and none of them mentions demo_id.
    AI_VERIFIED_QUERIES (
        points_balance AS (
            QUESTION 'how many points do i have'
            VERIFIED_AT 1787788800
            ONBOARDING_QUESTION TRUE
            SQL 'SELECT SUM(l.point_change) AS points_balance
                 FROM __points AS l'
        ),

        spend_this_year AS (
            QUESTION 'what have i spent here this year'
            VERIFIED_AT 1787788800
            ONBOARDING_QUESTION TRUE
            SQL 'SELECT SUM(o.settled_spend) AS total_spend,
                        SUM(o.settled_order) AS order_count
                 FROM __orders AS o
                 WHERE o.order_year = YEAR(CURRENT_DATE())'
        ),

        spend_by_month AS (
            QUESTION 'what have i spent each month'
            VERIFIED_AT 1787788800
            SQL 'SELECT o.order_month,
                        SUM(o.settled_spend) AS total_spend,
                        SUM(o.settled_order) AS order_count
                 FROM __orders AS o
                 GROUP BY o.order_month
                 ORDER BY o.order_month'
        ),

        last_order AS (
            QUESTION 'what did i order last time'
            VERIFIED_AT 1787788800
            ONBOARDING_QUESTION TRUE
            SQL 'WITH last_settled AS (
                     SELECT o.order_id, o.settled_at, o.store_id, o.order_total
                     FROM __orders AS o
                     WHERE o.settled_at IS NOT NULL
                     ORDER BY o.settled_at DESC
                     LIMIT 1
                 )
                 SELECT lo.settled_at AS ordered_at,
                        s.store_name,
                        m.item_name,
                        li.quantity,
                        li.line_spend,
                        lo.order_total
                 FROM last_settled AS lo
                 JOIN __order_lines AS li ON li.order_id = lo.order_id
                 JOIN __items AS m ON m.item_id = li.item_id
                 LEFT JOIN __restaurants AS s ON s.store_id = lo.store_id
                 ORDER BY li.line_spend DESC'
        ),

        most_ordered_item AS (
            QUESTION 'what do i order most'
            VERIFIED_AT 1787788800
            SQL 'SELECT m.item_name,
                        SUM(li.quantity * o.settled_order) AS items_ordered,
                        SUM(li.line_spend * o.settled_order) AS item_spend
                 FROM __order_lines AS li
                 JOIN __orders AS o ON o.order_id = li.order_id
                 JOIN __items AS m ON m.item_id = li.item_id
                 GROUP BY m.item_name
                 ORDER BY items_ordered DESC
                 LIMIT 10'
        ),

        most_visited_store AS (
            QUESTION 'which store do i go to most'
            VERIFIED_AT 1787788800
            SQL 'SELECT s.store_name,
                        s.store_city,
                        SUM(o.settled_order) AS visits,
                        SUM(o.settled_spend) AS total_spend
                 FROM __orders AS o
                 JOIN __restaurants AS s ON s.store_id = o.store_id
                 GROUP BY s.store_name, s.store_city
                 ORDER BY visits DESC
                 LIMIT 10'
        ),

        last_visit_by_store AS (
            QUESTION 'when did i last go to each store'
            VERIFIED_AT 1787788800
            SQL 'SELECT s.store_name,
                        s.store_city,
                        MAX(o.settled_at) AS last_visit,
                        SUM(o.settled_order) AS visits
                 FROM __orders AS o
                 JOIN __restaurants AS s ON s.store_id = o.store_id
                 GROUP BY s.store_name, s.store_city
                 ORDER BY last_visit DESC'
        )
    )

    COPY GRANTS;

-- The first apply after a reset has nothing for COPY GRANTS to copy, and this
-- is what makes that run right. `03_grants.sql` also grants ON ALL and ON
-- FUTURE SEMANTIC VIEWS in this schema; neither covers an object replaced in
-- place, which is what an apply does on every run after the first.
GRANT SELECT ON SEMANTIC VIEW CHIP_CHAT.ACCOUNTS.ACCOUNT_LANE TO ROLE CHIP_CHAT_READ;
