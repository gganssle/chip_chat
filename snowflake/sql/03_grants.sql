-- Privileges. This file IS the security boundary; everything else is furniture.
--
-- Read it as three columns. CHIP_CHAT_READ may SELECT everywhere and do nothing
-- else anywhere. CHIP_CHAT_WRITE may change ACCOUNTS, may read CATALOGUE to
-- price what it writes, and cannot see MARTS at all. CHIP_CHAT_PUBLISH may
-- replace CATALOGUE and MARTS wholesale and cannot see ACCOUNTS at all.
--
--                     CATALOGUE      ACCOUNTS       MARTS       warehouse
--   READ              select         select         select      serving
--   WRITE             select         select+DML     --          serving
--   PUBLISH           select+DML     --             select+DML  publish
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

-- Views as well as tables: the semantic view Cortex Analyst needs (#45) is a
-- view, and so is anything #42 adds to keep a join out of a generated query.
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.ACCOUNTS  TO ROLE CHIP_CHAT_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_READ;

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
-- one. It cannot see ACCOUNTS: the marts are computed in the lakehouse from
-- data that got there another way, and RFC-001 §04's containment argument --
-- that no visitor-editable field is an input to a mart -- is easier to keep
-- when the publisher physically cannot read demo_visitors.
-- --------------------------------------------------------------------------

GRANT USAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT USAGE ON SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;

GRANT CREATE TABLE, CREATE VIEW, CREATE STAGE ON SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT CREATE TABLE, CREATE VIEW, CREATE STAGE ON SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES    IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.CATALOGUE TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES    IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON FUTURE TABLES IN SCHEMA CHIP_CHAT.MARTS     TO ROLE CHIP_CHAT_PUBLISH;
