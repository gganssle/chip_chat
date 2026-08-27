-- Privileges. This file IS the security boundary; everything else is furniture.
--
-- Read it as four columns. CHIP_CHAT_READ may SELECT everywhere a conversation
-- reads and do nothing else anywhere. CHIP_CHAT_WRITE may change ACCOUNTS, may
-- read CATALOGUE to price what it writes, and cannot see MARTS at all.
-- CHIP_CHAT_PUBLISH may replace CATALOGUE and MARTS wholesale, owns the STAGING
-- dock outright, and reaches ACCOUNTS through three tables and no schema grant.
--
--                     CATALOGUE      ACCOUNTS       MARTS       STAGING   warehouse
--   READ              select         select         select      --        serving
--   WRITE             select         select+DML     --          --        serving
--   PUBLISH           select+DML     3 tables       select+DML  all       publish
--
-- THE THREE TABLES ARE THE ONE EXCEPTION IN THIS FILE, so read the argument for
-- them where it is made, at CHIP_CHAT_PUBLISH below. `account.GRANTS` carries
-- the same exception as `Access.tables` with the same reasoning attached, and
-- `test_account_layout.py` refuses a table-level grant the table does not name.
--
-- Every grant is made twice: once ON ALL, which covers what exists now, and once
-- ON FUTURE, which covers what #42, #39 and #46 will create. Future grants are
-- not retroactive and ALL grants are not prospective, so a schema that is
-- currently empty needs both or it will need a human later -- and "run the
-- grants again after you add a table" is the instruction everybody forgets.
--
-- Two privileges are conspicuously absent from all three lane roles, and their
-- absence is load-bearing:
--
--   APPLY ROW ACCESS POLICY   The policies #43 attaches to ACCOUNTS keep one
--                             visitor out of another's rows. A role that can
--                             apply policies can detach them. Only
--                             CHIP_CHAT_ADMIN owns them, and no service user
--                             holds CHIP_CHAT_ADMIN.
--   OWNERSHIP                 Nothing here transfers ownership of anything to a
--                             lane role. An owner can drop what it owns, and a
--                             dropped table has no policy on it.
--
-- Row access policies apply to every role, ownership included -- Snowflake has
-- no owner exemption -- which is why issue #41's third criterion, that the
-- write role cannot read another visitor's rows either, is a property of this
-- file rather than of #43's policy text. `snowflake-verify` proves it against a
-- live policy.

USE ROLE SECURITYADMIN;

-- Compute. The serving/publish split from 01_warehouses.sql is enforced here:
-- a role that has no USAGE on a warehouse cannot name it, so the nightly batch
-- has no way to end up on the compute a conversation is waiting on.
GRANT USAGE ON WAREHOUSE CHIP_CHAT_SERVING_WH TO ROLE CHIP_CHAT_READ;
GRANT USAGE ON WAREHOUSE CHIP_CHAT_SERVING_WH TO ROLE CHIP_CHAT_WRITE;
GRANT USAGE ON WAREHOUSE CHIP_CHAT_PUBLISH_WH TO ROLE CHIP_CHAT_PUBLISH;

GRANT USAGE, OPERATE, MONITOR ON WAREHOUSE CHIP_CHAT_SERVING_WH TO ROLE CHIP_CHAT_ADMIN;
GRANT USAGE, OPERATE, MONITOR ON WAREHOUSE CHIP_CHAT_PUBLISH_WH TO ROLE CHIP_CHAT_ADMIN;

USE ROLE CHIP_CHAT_ADMIN;

GRANT USAGE ON DATABASE CHIP_CHAT TO ROLE CHIP_CHAT_READ;
GRANT USAGE ON DATABASE CHIP_CHAT TO ROLE CHIP_CHAT_WRITE;
GRANT USAGE ON DATABASE CHIP_CHAT TO ROLE CHIP_CHAT_PUBLISH;

-- --------------------------------------------------------------------------
-- CHIP_CHAT_READ -- questions, and nothing but questions.
-- --------------------------------------------------------------------------

GRANT USAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT USAGE ON SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT USAGE ON SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;

GRANT SELECT ON ALL TABLES IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL TABLES IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL TABLES IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;

-- Views as well as tables: `09_audit.sql` creates two, and #42 may add more to
-- keep a join out of a generated query.
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;

-- A SEMANTIC VIEW IS NOT A VIEW. It is its own object type with its own
-- privilege, and the six grants above do not reach #45's ACCOUNT_LANE however
-- much its name suggests they should. These do -- and they are still not
-- sufficient on their own: `CREATE OR REPLACE` drops an object's grants and a
-- future grant does not re-apply to a replaced object, so `10_semantic_view.sql`
-- carries COPY GRANTS and an explicit grant of its own. All three, because the
-- failure they prevent is silent: the lane stops answering and nothing logs a
-- privilege.
GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;

-- --------------------------------------------------------------------------
-- CHIP_CHAT_WRITE -- the ops API, after a visitor has clicked confirm.
--
-- It reads CATALOGUE because an order line needs a price and a price lives on
-- a restaurant (docs/decisions/menu-pricing.md). It does not touch MARTS: a
-- write path has no reason to read what the recommender computed, and #46's
-- procedures will not ask it to.
-- --------------------------------------------------------------------------

GRANT USAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_WRITE;
GRANT USAGE ON SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_WRITE;

GRANT SELECT ON ALL TABLES    IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT ON ALL VIEWS     IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_WRITE;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT ON ALL VIEWS    IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;

-- #46 makes every write action a stored procedure. The privilege to call one is
-- granted ahead of the procedures existing, so that landing #46 does not also
-- mean re-running a grants file nobody remembers is required.
GRANT USAGE ON ALL PROCEDURES    IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;
GRANT USAGE ON FUTURE PROCEDURES IN SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_WRITE;

-- --------------------------------------------------------------------------
-- CHIP_CHAT_PUBLISH -- the nightly job out of Databricks (#39).
--
-- It creates tables, because a publish replaces a mart rather than merging into
-- one.
--
-- IT DOES NOT GET A SCHEMA GRANT ON ACCOUNTS, and it does write three tables in
-- it. Both halves are deliberate. #39's scope publishes the synthetic account
-- tables on the same schedule as the marts, and the three named below are
-- exactly the tables the marts are computed from -- `schema.MART_INPUTS`, and
-- `account.PUBLISHED_ACCOUNT_TABLES` asserts the two lists are one list.
--
-- The three that are missing are the point:
--
--   demo_visitors       Holds all three columns a visitor may edit, and is the
--                       one account table a visitor writes to. A nightly
--                       overwrite would delete every edit made that day. It is
--                       also what RFC-001 §04 rests its answer to PRD Q2 on --
--                       no editable field is an input to a mart, checkable
--                       because the publisher physically cannot read the table
--                       they live in. A schema-level grant here would have
--                       thrown that away to move three tables.
--   personas
--   persona_fixtures    Reference rows the generator emits once. They reach the
--                       account through `chip_chat.snowflake.load`, run by an
--                       operator as CHIP_CHAT_ADMIN.
--
-- No CREATE TABLE on ACCOUNTS either: the publisher replaces the rows of three
-- tables somebody else declared, and cannot make a fourth.
--
-- AND IT OWNS STAGING. That schema is the loading dock -- an incoming
-- generation is an unscoped copy of a visitor-scoped table with no row access
-- policy on it, so it lands somewhere neither lane a conversation runs on can
-- reach. 02_database.sql carries the argument; the grants are three lines and
-- the absence of six.
-- --------------------------------------------------------------------------

GRANT USAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT USAGE ON SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
GRANT USAGE ON SCHEMA CHIP_CHAT.STAGING   TO ROLE CHIP_CHAT_PUBLISH;

GRANT CREATE TABLE, CREATE VIEW, CREATE STAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT CREATE TABLE, CREATE VIEW, CREATE STAGE ON SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
GRANT CREATE TABLE, CREATE STAGE               ON SCHEMA CHIP_CHAT.STAGING   TO ROLE CHIP_CHAT_PUBLISH;

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES    IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES    IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES    IN SCHEMA CHIP_CHAT.STAGING   TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.STAGING   TO ROLE CHIP_CHAT_PUBLISH;

-- ACCOUNTS, three tables and no schema. USAGE reaches into the schema and
-- reaches nothing in it on its own -- a managed-access schema grants no object
-- privilege with it -- so the three GRANTs below are the whole of what the
-- publisher can touch here. TRUNCATE is what the swap's INSERT OVERWRITE needs;
-- UPDATE is not granted, because a publish replaces a generation and never
-- edits a row.
GRANT USAGE ON SCHEMA CHIP_CHAT.ACCOUNTS TO ROLE CHIP_CHAT_PUBLISH;

GRANT SELECT, INSERT, DELETE, TRUNCATE ON TABLE CHIP_CHAT.ACCOUNTS.orders         TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, DELETE, TRUNCATE ON TABLE CHIP_CHAT.ACCOUNTS.order_items    TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, DELETE, TRUNCATE ON TABLE CHIP_CHAT.ACCOUNTS.loyalty_ledger TO ROLE CHIP_CHAT_PUBLISH;
