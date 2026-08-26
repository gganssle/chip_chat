-- One database, three schemas, and a boundary you can grant on.
--
-- RFC-001 §04: Snowflake holds two populations that must never blur -- the real
-- catalogue, harvested and versioned, and the synthetic account data generated
-- against it. Issue #41 adds the third: the marts Databricks publishes
-- overnight, which are derived from the second but written by a different
-- identity on a different clock.
--
--   CHIP_CHAT.CATALOGUE   real, harvested, cited        #42 fills it
--   CHIP_CHAT.ACCOUNTS    synthetic, visitor-scoped     #42 fills it, #43 puts
--                                                       row access policies on it
--   CHIP_CHAT.MARTS       published nightly, derived    #39 fills it
--
-- Why schemas and not a table-name prefix: the same argument the lakehouse made
-- in docs/lakehouse-catalog.md. A prefix cannot be granted on. A schema can, and
-- 03_grants.sql spends the whole file doing it -- the write role gets ACCOUNTS
-- and not MARTS, the publish role gets MARTS and not ACCOUNTS, and neither of
-- those sentences is expressible about a naming convention.
--
-- MANAGED ACCESS is the setting worth stopping on. In an ordinary schema the
-- owner of an object may grant access to it; in a managed-access schema only
-- the schema owner may, and object owners cannot. So when #42 creates tables
-- and #46 creates procedures here, the ability to widen access to them stays
-- with CHIP_CHAT_ADMIN rather than travelling with whoever created the object.
-- A boundary that any later object owner can open is not a boundary.
--
-- PUBLIC is dropped. Snowflake creates it with every database and grants the
-- PUBLIC role usage on it, which makes it precisely the ungoverned corner where
-- a table ends up when someone forgets to qualify a name.
--
-- CREATE DATABASE IF NOT EXISTS, never OR REPLACE: replacing it would drop
-- every table in it. The from-scratch path is `snowflake-reset`, which says so.

USE ROLE SYSADMIN;

CREATE DATABASE IF NOT EXISTS CHIP_CHAT
    COMMENT = 'The serving layer. Catalogue, demo accounts, published marts. Built by snowflake/sql, not by hand.';

GRANT OWNERSHIP ON DATABASE CHIP_CHAT TO ROLE CHIP_CHAT_ADMIN COPY CURRENT GRANTS;

USE ROLE CHIP_CHAT_ADMIN;

CREATE SCHEMA IF NOT EXISTS CHIP_CHAT.CATALOGUE
    WITH MANAGED ACCESS
    COMMENT = 'Real. Harvested from Chipotle published pages, versioned, cited. menu_items, item_prices, modifiers, stores. RFC-001 §04.';

CREATE SCHEMA IF NOT EXISTS CHIP_CHAT.ACCOUNTS
    WITH MANAGED ACCESS
    COMMENT = 'Synthetic and visitor-scoped. Every table here carries demo_id and will carry a row access policy (#43). personas, persona_fixtures, demo_visitors, orders, order_items, loyalty_ledger.';

CREATE SCHEMA IF NOT EXISTS CHIP_CHAT.MARTS
    WITH MANAGED ACCESS
    COMMENT = 'Published nightly from Databricks (#39). customer_360, usual_order, item_affinity, spend_summary. Read by the personalization lane, written by nobody else.';

-- ENABLE MANAGED ACCESS on a re-run: IF NOT EXISTS above is silent about the
-- properties of a schema that already exists, and a schema someone turned
-- managed access off on would otherwise stay that way forever.
ALTER SCHEMA CHIP_CHAT.CATALOGUE ENABLE MANAGED ACCESS;
ALTER SCHEMA CHIP_CHAT.ACCOUNTS  ENABLE MANAGED ACCESS;
ALTER SCHEMA CHIP_CHAT.MARTS     ENABLE MANAGED ACCESS;

DROP SCHEMA IF EXISTS CHIP_CHAT.PUBLIC;
