-- The real half. Four tables, harvested from Chipotle's published pages.
--
-- RFC-001 §04 fixes this schema and issue #42 says to match it exactly, so the
-- columns below are §04's columns and the two additions are argued where they
-- appear. What §04 does *not* fix is the type of anything, and the types here
-- are the ones the producing layer already uses: silver's DECIMAL(10,2) for
-- money and DECIMAL(8,2) for calories, so a publish (#39) is a copy rather than
-- a cast nobody wrote down. See databricks/src/chip_chat/databricks/silver.py.
--
-- This is a PROJECTION of silver, not a copy of it. `menu_items` carries
-- fifteen columns in the lakehouse and nine here, because the serving layer
-- answers the account and action lanes -- what is orderable, what it costs,
-- where -- while the knowledge lane answers from Azure AI Search over the same
-- harvest with citations. A column nothing in a conversation reads is a column
-- Cortex Analyst can still put in a generated query, so the narrow table is the
-- retrieval decision as much as it is the storage one.
--
-- CREATE OR ALTER TABLE, not CREATE TABLE IF NOT EXISTS, and it is the whole
-- reason every comment below is re-asserted rather than written once:
--
--   * IF NOT EXISTS is silent about a table that already exists, so a reworded
--     column comment would land in git and never reach the account -- and these
--     comments are what #45's semantic view retrieves against. A comment that
--     drifts is worse than one that is missing, because it still answers.
--   * OR ALTER converges the live table to what is written here, keeping the
--     rows. Verified on this account: adding a column and rewording a comment
--     left the existing row untouched.
--   * A row access policy attached to the table SURVIVES it. Also verified,
--     and load-bearing: #43 attaches the policies that keep visitors apart, and
--     a routine apply that silently detached one would be a breach nobody sees.
--     docs/snowflake-schema.md §4 has both transcripts.
--
-- The sharp edge, stated once for all three DDL files: removing a column from a
-- definition here DROPS it on the next apply. That is a deliberate act by
-- whoever edits the file, not something a re-run does on its own -- but it is
-- the one way an apply can destroy, and `test_schema_layout.py` cannot see it
-- coming.
--
-- CONSTRAINTS. Snowflake enforces NOT NULL and nothing else: PRIMARY KEY,
-- UNIQUE and FOREIGN KEY are metadata. They are declared anyway, and not as
-- decoration -- #45's semantic view reads them to know which joins exist, and a
-- text-to-SQL system that has to guess a join key guesses. So: NOT NULL carries
-- every invariant that must actually hold, and the keys carry the shape. None
-- of them is declared RELY, which would let the optimizer eliminate a join on
-- the strength of a promise nothing checks.
--
-- CLUSTERING. None, on purpose, and the arithmetic is the argument. A micro-
-- partition holds up to 16 MB compressed; this whole catalogue is a few
-- thousand rows and the account data is under fifty thousand order lines, so
-- every table here is one or two partitions and there is nothing to prune.
-- Snowflake's own guidance is that clustering keys pay off on tables in the
-- multi-terabyte range. Adding one would enable automatic clustering, which
-- bills to a serverless pool -- against a trial capped at $400 of credits, that
-- is a recurring cost buying a scan that was already one partition wide.
-- docs/snowflake-schema.md §5 measures it.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.CATALOGUE;

-- --------------------------------------------------------------------------
-- menu_items -- what is orderable, and what Chipotle says about it.
--
-- No price. §04 moved money to item_prices because Chipotle's published prices
-- vary by nearly twenty percent between restaurants -- a Steak Burrito was
-- $11.15 at one and $13.15 at another on the same afternoon -- so a base_price
-- column would have had to name one store's number and present it as the
-- price. See docs/decisions/menu-pricing.md.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE menu_items (
    item_id VARCHAR NOT NULL
        COMMENT 'Chipotle''s own identifier, e.g. CMG-2. Published in the menu API, stable across harvests, and the value every order line and every mart names an item by.',
    name VARCHAR NOT NULL
        COMMENT 'The name as published, e.g. Steak Burrito. What a visitor says out loud, and what an answer should call it back.',
    category VARCHAR
        COMMENT 'The published top-level category: Entree, Side, Drink, or Non Food Items. Null where the menu publishes none. The usual_order mart uses category = ''Entree'' to decide which item of a basket is the meal.',
    description VARCHAR
        COMMENT 'What Chipotle publishes about the item, where it publishes anything. Null is an absence of published copy, not an empty description.',
    calories NUMBER(8,2)
        COMMENT 'The published total-calorie figure for this item as ordered by default, from the nutrition endpoint. Null where nothing is published -- which is most composed entrees, whose calories depend on how they are built.',
    allergens ARRAY
        COMMENT 'The allergen codes Chipotle marks this item as CONTAINING, e.g. ["dairy"]. Read allergen_disclosure before reading an empty array: empty does not mean none.',
    allergen_disclosure VARCHAR NOT NULL
        COMMENT 'PUBLISHED if Chipotle publishes allergen data for this item, NOT_PUBLISHED if it publishes none at all. The column exists so that an empty allergens array cannot be read as a promise: an unmarked item is one Chipotle has declined to make a statement about, and an item with no published data at all is a third thing again. docs/decisions/allergen-absence.md. Allergen ANSWERS are composed in the knowledge lane against the published chart, with a citation; this column is what stops a generated query from inventing one here.',
    source_url VARCHAR NOT NULL
        COMMENT 'The endpoint the item''s identity was read from. Half of a citation; harvested_at is the other half.',
    harvested_at TIMESTAMP_NTZ NOT NULL
        COMMENT 'When that endpoint was fetched, UTC. Every timestamp in this database is UTC and carries no zone, because there is exactly one clock in this system and a rendered local time is a rendering decision.',
    CONSTRAINT pk_menu_items PRIMARY KEY (item_id)
)
COMMENT = 'Real, harvested from Chipotle''s published menu, versioned and cited. One row per orderable item. Carries identity and structure, which are the same at every restaurant; money lives in item_prices, which is keyed by restaurant. Nothing in this table is invented -- the synthetic half of the demo is CHIP_CHAT.ACCOUNTS, and every order there names an item_id from here.';

-- --------------------------------------------------------------------------
-- item_prices -- money, keyed by the restaurant that published it.
--
-- Modifiers are priced here too, under their own modifier_item_id: guacamole
-- is an item on the price list whether it arrives as a bowl topping or on its
-- own, which is why modifiers.modifier_item_id below exists.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE item_prices (
    restaurant_id NUMBER(10,0) NOT NULL
        COMMENT 'Chipotle''s numeric restaurant identifier -- the same number as stores.store_id. A price is a fact about one restaurant on one day and is never true of the chain.',
    item_id VARCHAR NOT NULL
        COMMENT 'The item priced. Joins to menu_items.item_id, and to modifiers.modifier_item_id when the priced thing is something added to another item.',
    unit_price NUMBER(10,2) NOT NULL
        COMMENT 'The in-store price of one, in US dollars, as published by this restaurant.',
    unit_delivery_price NUMBER(10,2) NOT NULL
        COMMENT 'The delivery price of one. Chipotle publishes it as a separate and higher number, which is why orders.channel says which of the two priced an order.',
    is_available BOOLEAN NOT NULL
        COMMENT 'Whether this restaurant had the item at harvest time. False is a real answer to "can I order this here" and not a gap in the harvest.',
    source_url VARCHAR NOT NULL
        COMMENT 'The endpoint this price was read from.',
    harvested_at TIMESTAMP_NTZ NOT NULL
        COMMENT 'When it was fetched, UTC. A quoted price without this is a number with no date on it, and RFC-001 §08 requires the date.',
    CONSTRAINT pk_item_prices PRIMARY KEY (restaurant_id, item_id),
    CONSTRAINT fk_item_prices_item FOREIGN KEY (item_id) REFERENCES menu_items (item_id)
)
COMMENT = 'What each restaurant charges for each item, in-store and for delivery. One row per restaurant per item. Price varies by nearly twenty percent between restaurants, so any quoted price has a store and a harvest date attached to it or it is not a quote. docs/decisions/menu-pricing.md.';

-- --------------------------------------------------------------------------
-- modifiers -- how an item is built.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE modifiers (
    modifier_id VARCHAR NOT NULL
        COMMENT 'Spelled <item_id>:<modifier_item_id>. The same ingredient added to two different entrees is two rows, because what it costs and what it adds depends on what it is added to. This is the value order_items.modifiers and usual_order.modifiers hold.',
    item_id VARCHAR NOT NULL
        COMMENT 'The item being modified. A modifier belongs to one item and cannot be attached to another.',
    modifier_item_id VARCHAR NOT NULL
        COMMENT 'The identifier of the thing added, which is itself a priced item -- so this is the join into item_prices that answers "what does adding guacamole cost at this restaurant". Not in RFC-001 §04, which spells the same fact inside modifier_id: without the column, pricing a modifier means splitting a string in a generated query, and a text-to-SQL system that has to do string surgery to find a join key will eventually do it wrong.',
    name VARCHAR NOT NULL
        COMMENT 'The name as published, e.g. Black Beans.',
    delta_calories NUMBER(8,2)
        COMMENT 'The published total-calorie figure for the thing being added, at the portion the menu treats as one. Null where nothing is published. Named delta because it is what the item''s own calories move by, not a figure about the finished bowl.',
    CONSTRAINT pk_modifiers PRIMARY KEY (modifier_id),
    CONSTRAINT fk_modifiers_item FOREIGN KEY (item_id) REFERENCES menu_items (item_id)
)
COMMENT = 'The ingredients an item can be built from, one row per (item, addition). No price column: a modifier is priced as an item at a restaurant, so its money is in item_prices under modifier_item_id -- the same reason menu_items carries no base_price.';

-- --------------------------------------------------------------------------
-- stores -- where an order happens, and when it could have.
-- --------------------------------------------------------------------------

CREATE OR ALTER TABLE stores (
    store_id NUMBER(10,0) NOT NULL
        COMMENT 'Chipotle''s restaurant number. The same identifier as item_prices.restaurant_id, and what orders.store_id and customer_360.favourite_store name.',
    name VARCHAR
        COMMENT 'The name the company uses for the restaurant, e.g. NC Town 1 Mall. Null where the locator publishes none -- an answer then names the city rather than inventing a name.',
    city VARCHAR
        COMMENT 'The city, as published. The locator appends a county for some restaurants and this keeps whatever it published.',
    region VARCHAR
        COMMENT 'The state or territory code, e.g. CA.',
    hours ARRAY
        COMMENT 'Seven objects, one per day, each {day_of_week, opens, closes, is_published} with opens and closes as published HH:MM strings. Seven entries whether or not seven days were published: is_published false is "this restaurant publishes no hours for Sunday", which is a different answer from "closed on Sunday" and the only one the harvest can support.',
    CONSTRAINT pk_stores PRIMARY KEY (store_id)
)
COMMENT = 'The restaurants in the demo, harvested from the published locator. Thirty of them by default. A store is where an order was placed and where a price was quoted; personas.home_store and demo_visitors.home_store_override both point here.';
