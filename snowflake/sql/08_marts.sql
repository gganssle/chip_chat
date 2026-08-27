-- The derived half. Four marts, computed overnight in Databricks and published
-- here by #39. Nothing in this file computes anything.
--
-- The columns are RFC-001 §04's, and they are also
-- `databricks.gold.RFC_COLUMNS` -- the same transcription, made twice, on the
-- two sides of the publish. That duplication is deliberate: the producer holds
-- itself to §04 in `make ci` and so does the consumer, and a rename that only
-- one of them made is a failing test on both sides rather than a nightly job
-- that lands a column into nothing.
--
-- derived_at is the one addition, and only where §04 did not already name it.
-- RFC-001 §10 requires a stale mart to be served WITH its timestamp and never
-- silently as fresh, and a mart with nowhere to put one cannot be. It is not a
-- publish audit column -- it is the value the personalization tools surface in
-- a sentence: "based on your orders through Tuesday".
--
-- WHY THREE OF THE FOUR ARE VISITOR-SCOPED AND ONE IS NOT. customer_360,
-- usual_order and spend_summary are keyed on demo_id and get #43's row access
-- policies like anything else. item_affinity is about two items and no person:
-- "people who ordered this also ordered that", aggregated over the whole
-- population. It carries no demo_id and 09_audit.sql exempts it by name, with
-- that reason recorded beside the exemption.
--
-- WHAT THE MARTS ARE NOT COMPUTED FROM. Not demo_visitors. The three columns a
-- visitor may edit live there and only there, so no editable field is an input
-- to a mart -- which is what makes "an edit cannot make a mart stale" a
-- property of the schema rather than a promise. A reviewer checks it by
-- confirming nothing under databricks/ selects from demo_visitors.
-- docs/decisions/persona-editing.md.
--
-- Money is NUMBER(12,2) here and NUMBER(10,2) in ACCOUNTS: gold widens the
-- precision because a lifetime of orders sums past what one order needs, and
-- these are the types the gold layer already declares.
--
-- Read 06_catalogue.sql's header for why every table is CREATE OR ALTER, why
-- the keys are declared but not enforced, and why nothing is clustered.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.MARTS;

-- --------------------------------------------------------------------------
-- customer_360 -- one row per visitor who has ever placed a settled order.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE customer_360 (
    demo_id VARCHAR NOT NULL
        COMMENT 'Whose account this is. What #43''s row access policy compares against.',
    order_count NUMBER(38,0)
        COMMENT 'Settled orders, all time. Cancelled and refunded orders are rows in ACCOUNTS.orders and are not counted here.',
    lifetime_spend NUMBER(12,2)
        COMMENT 'What those orders totalled, exactly, in US dollars.',
    last_order_at TIMESTAMP_NTZ
        COMMENT 'Their most recent settled order, UTC. What lapsed_flag is measured from.',
    favourite_store NUMBER(10,0)
        COMMENT 'The restaurant most of their orders were placed at, ties broken on the lowest store_id. DERIVED FROM orders.store_id, so it may legitimately disagree with a visitor''s demo_visitors.home_store_override -- someone who moved. The serving layer says so out loud rather than reconciling the two silently.',
    cadence_days NUMBER(8,2)
        COMMENT 'Mean days between consecutive settled orders. Null for a customer with one order, because one order is not a cadence and zero would read as "every day".',
    lapsed_flag BOOLEAN
        COMMENT 'Whether their last settled order is older than the lapsed threshold, measured from the latest order in the whole population rather than from now -- so a rebuild does not disagree with the mart it replaced and nobody lapses further overnight.',
    derived_at TIMESTAMP_NTZ
        COMMENT 'When the nightly job computed this row, UTC. Surfaced in the answer when it predates the visitor''s most recent order, per RFC-001 §10: a stale mart is served with its timestamp, never silently as fresh.',
    CONSTRAINT pk_customer_360 PRIMARY KEY (demo_id),
    CONSTRAINT fk_customer_360_visitor FOREIGN KEY (demo_id) REFERENCES CHIP_CHAT.ACCOUNTS.demo_visitors (demo_id)
)
COMMENT = 'One row per visitor who has ever placed a settled order: how many, how much, how often, where, and whether they have gone quiet. Published nightly from Databricks (#39) and written by nobody else. Visitor-scoped. Computed from orders, order_items and loyalty_ledger only -- never from demo_visitors, which is where every visitor-editable field lives.';

-- --------------------------------------------------------------------------
-- usual_order -- the one basket that is theirs, and how sure we are.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE usual_order (
    demo_id VARCHAR NOT NULL
        COMMENT 'Whose usual. What #43''s row access policy compares against.',
    item_id VARCHAR
        COMMENT 'The entree of their commonest basket, as a menu_items.item_id. The lowest by item_id where a basket carries several, so a family ordering three entrees still has one stable answer.',
    modifiers ARRAY
        COMMENT 'How they have it built: modifiers.modifier_id values, sorted. Empty means the basket carried no modifiers, not that they are unknown.',
    confidence NUMBER(5,4)
        COMMENT 'How dominant that basket is among their settled orders, in [0,1], with a lower bound applied so that a customer with three orders does not read as certain. LOAD-BEARING, not decorative: it decides whether the assistant states the usual, hedges it, or says it does not know -- the Explorer fixture is deliberately low-confidence and the honest "I am not sure what your usual is" path exists because of this column.',
    derived_at TIMESTAMP_NTZ
        COMMENT 'When the nightly job computed this row, UTC. Named in the explanation when it predates their most recent order -- "based on your orders through Tuesday".',
    CONSTRAINT pk_usual_order PRIMARY KEY (demo_id),
    CONSTRAINT fk_usual_order_visitor FOREIGN KEY (demo_id) REFERENCES CHIP_CHAT.ACCOUNTS.demo_visitors (demo_id),
    CONSTRAINT fk_usual_order_item FOREIGN KEY (item_id) REFERENCES CHIP_CHAT.CATALOGUE.menu_items (item_id)
)
COMMENT = 'What each visitor usually orders, computed from eighteen months of settled orders. One row per visitor. Visitor-scoped. A stated preference that contradicts this row does not rewrite it: the mart is reported as computed and the preference applied on top, out loud -- "your usual has cheese; you have told me no dairy, so I have left it off this one".';

-- --------------------------------------------------------------------------
-- item_affinity -- the one mart with no visitor in it.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE item_affinity (
    item_id VARCHAR NOT NULL
        COMMENT 'The item somebody ordered.',
    related_item_id VARCHAR NOT NULL
        COMMENT 'An item that turned up in the same order. Ordered pairs: the row for (a,b) and the row for (b,a) both exist and carry the same lift.',
    lift NUMBER(12,6)
        COMMENT 'P(both) / (P(a) * P(b)) over settled orders. Above one means the pair turns up together more often than independence would predict; one means it does not. Computed as four integers and one division so that a rebuild reproduces the number exactly.',
    derived_at TIMESTAMP_NTZ
        COMMENT 'When the nightly job computed this row, UTC.',
    CONSTRAINT pk_item_affinity PRIMARY KEY (item_id, related_item_id),
    CONSTRAINT fk_item_affinity_item FOREIGN KEY (item_id) REFERENCES CHIP_CHAT.CATALOGUE.menu_items (item_id),
    CONSTRAINT fk_item_affinity_related FOREIGN KEY (related_item_id) REFERENCES CHIP_CHAT.CATALOGUE.menu_items (item_id)
)
COMMENT = 'Which items are ordered together, across the whole population. THE ONE MART WITH NO demo_id: it is a fact about two items and about nobody, so there is no visitor to scope it to and 09_audit.sql exempts it by name. That also means it is the only personalization input a visitor with no history can be given.';

-- --------------------------------------------------------------------------
-- spend_summary -- spend by period, for the question that asks for a number.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE spend_summary (
    demo_id VARCHAR NOT NULL
        COMMENT 'Whose spend. What #43''s row access policy compares against.',
    period VARCHAR NOT NULL
        COMMENT 'The calendar month the orders fall in, as YYYY-MM. A month is the grain because it is the one the question is usually asked in.',
    total NUMBER(12,2)
        COMMENT 'What they spent in that period, in US dollars, over settled orders only.',
    order_count NUMBER(38,0)
        COMMENT 'How many settled orders that was.',
    derived_at TIMESTAMP_NTZ
        COMMENT 'When the nightly job computed this row, UTC.',
    CONSTRAINT pk_spend_summary PRIMARY KEY (demo_id, period),
    CONSTRAINT fk_spend_summary_visitor FOREIGN KEY (demo_id) REFERENCES CHIP_CHAT.ACCOUNTS.demo_visitors (demo_id)
)
COMMENT = 'Spend and order count per visitor per month. Visitor-scoped. A row exists only for a month the visitor ordered in, so a gap in the series is a month with no orders rather than a month with no data.';
