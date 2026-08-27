-- One database, four schemas, and a boundary you can grant on.
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
--   CHIP_CHAT.STAGING     the loading dock, not a lane  #39 writes and empties it
--
-- THE FOURTH IS NOT A POPULATION. The three above are lanes a conversation
-- reads. STAGING is where #39's nightly publish lands an incoming generation
-- before one INSERT OVERWRITE makes it live in the lane it belongs to.
--
-- It cannot land beside its target, and that is the whole reason this schema
-- exists. 03_grants.sql gives CHIP_CHAT_READ SELECT ON FUTURE TABLES in all
-- three lanes -- deliberately, so a table a later issue adds is readable
-- without anyone remembering to re-run a grants file. Applied to an incoming
-- generation that is exactly wrong: an `orders_incoming` in ACCOUNTS would be a
-- complete unscoped copy of the population, readable by the identity the agent
-- runs as, and covered by no row access policy, because #43 attaches policies
-- to tables BY NAME. A schema nothing but the publisher can reach is the only
-- shape of that dock which is not a hole.
--
-- It holds no declared table and is empty between runs: the publish drops a
-- staging table when its swap succeeds, so one that is still there is the
-- evidence of a run that stopped.
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

CREATE SCHEMA IF NOT EXISTS CHIP_CHAT.STAGING
    WITH MANAGED ACCESS
    COMMENT = 'The loading dock for the nightly publish (#39), and not one of the three populations. Holds one incoming generation per published table, for as long as it takes an INSERT OVERWRITE to make it live, and is empty between runs. Granted to CHIP_CHAT_PUBLISH and to nobody else: an incoming generation is an unscoped copy of a visitor-scoped table with no row access policy on it, so neither lane a conversation runs on may read one. Nothing here is declared by snowflake/sql -- every table in it is created and dropped by the job.';

-- ENABLE MANAGED ACCESS on a re-run: IF NOT EXISTS above is silent about the
-- properties of a schema that already exists, and a schema someone turned
-- managed access off on would otherwise stay that way forever.
ALTER SCHEMA CHIP_CHAT.CATALOGUE ENABLE MANAGED ACCESS;
ALTER SCHEMA CHIP_CHAT.ACCOUNTS  ENABLE MANAGED ACCESS;
ALTER SCHEMA CHIP_CHAT.MARTS     ENABLE MANAGED ACCESS;
ALTER SCHEMA CHIP_CHAT.STAGING   ENABLE MANAGED ACCESS;

DROP SCHEMA IF EXISTS CHIP_CHAT.PUBLIC;
