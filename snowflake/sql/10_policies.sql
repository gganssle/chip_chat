-- The isolation mechanism. Two row access policies, and the eight tables they
-- are attached to. RFC-001 §05 is the section this file implements, and it is
-- the section to read first.
--
-- The agent is a program that takes untrusted input -- visitor text, harvested
-- web documents, uploaded photographs -- and produces output that is itself
-- untrusted. Any design where that program is also responsible for asserting
-- WHOSE data to return is one prompt away from a disclosure. So it is not
-- asked to. Identity originates in the app's server-side session, is applied to
-- the Snowflake connection as the DEMO_ID session variable, and is enforced
-- here. No tool signature accepts a visitor identifier; that absence is the
-- enforcement, and this file is what makes the absence safe.
--
-- WHY A POLICY AND NOT MIDDLEWARE. A row access policy applies regardless of
-- what SQL Cortex Analyst generates. The account lane hands natural language to
-- a text-to-SQL system and executes what comes back, so `SELECT * FROM orders`
-- is a query this system will genuinely run, and the only place to answer it
-- correctly is under the query rather than in front of it.
--
-- WHY IT APPLIES TO EVERY ROLE. Snowflake has no owner exemption: a row access
-- policy filters the table for whoever reads it, ownership included. The ops
-- API's CHIP_CHAT_WRITE is bound by exactly the same policy as the read lane,
-- which is issue #41's third criterion and is checked by `snowflake-verify`.
-- Neither policy body names a lane role, and `test_row_access_policies.py`
-- fails if one ever does -- a single OR clause is all it would take to exempt
-- the ops API, and it would read like a convenience.
--
-- THE TWO POLICIES
--
--   visitor_isolation   demo_id must equal the bound visitor. DEFAULT DENY: an
--                       unset session variable returns ZERO rows, never all of
--                       them. Seven tables.
--   entry_roster        persona_fixtures only, and it is the one inversion in
--                       this file. Argued below.
--
-- DEFAULT DENY IS WRITTEN OUT RATHER THAN INHERITED. `row_demo_id =
-- GETVARIABLE('DEMO_ID')` already denies an unbound session, because an unset
-- variable is NULL and a comparison against NULL is NULL rather than TRUE. That
-- is correct and it is also a fact about three-valued logic that a reader has
-- to know before this file is safe to review. The IS NOT NULL is therefore
-- redundant on purpose: the most important property of the mechanism is legible
-- in the policy body instead of being a consequence of it.
--
-- THE ONE INVERSION, AND WHY IT IS A SECOND POLICY RATHER THAN A CLAUSE.
-- persona_fixtures is the roster the app chooses a visitor's synthetic customer
-- FROM, which means it is read on a connection that has bound nobody yet --
-- entry (#67), and "switch persona", which 07_accounts.sql describes as
-- minting a new demo_id on a clean connection. Under visitor_isolation that
-- read returns nothing and entry has no roster to choose from.
--
-- So entry_roster inverts the unbound case and ONLY the unbound case: a session
-- with no visitor bound sees the whole roster, and a session with a visitor
-- bound sees that visitor's fixture and no other. That is strictly narrower
-- than leaving the table unprotected, which was the other option: the reachable
-- state under this policy is the entry lane, which has no visitor to leak to,
-- rather than every bound conversation for the life of the demo.
--
-- It is a separate policy with its own name because a clause bolted onto
-- visitor_isolation would widen all seven other tables the day somebody edited
-- the wrong line. Two policies cannot be widened by accident; one policy with
-- an exception in it can. `test_row_access_policies.py` requires that exactly
-- one policy is open when unbound, that it guards exactly one table, and that
-- the reason is written down.
--
-- THE MAINTENANCE ESCAPE, AND WHY IT GIVES NOTHING AWAY. visitor_isolation has
-- a second clause: CHIP_CHAT_ADMIN, and only when it has also set ALL_VISITORS.
-- Two conditions, and the demo can produce neither.
--
--   The role. No service user holds CHIP_CHAT_ADMIN -- 04_users.sql grants each
--   of the three exactly one lane role and sets DEFAULT_SECONDARY_ROLES = (),
--   so a session cannot pick it up implicitly. And CHIP_CHAT_ADMIN already
--   holds APPLY ROW ACCESS POLICY, which nothing else does: it can detach the
--   policy outright. A role that can remove the lock is not further empowered
--   by being given a key.
--
--   The variable. The escape is not the absence of DEMO_ID, it is the presence
--   of ALL_VISITORS. Default deny therefore survives intact for every role
--   including the owner: an admin session that forgot to bind a visitor reads
--   zero rows exactly like everybody else, and a cross-visitor read has to be
--   asked for by name. `load.py` asks for it -- counting the rows it just
--   landed is precisely a cross-visitor question -- and issue #47's nightly
--   reset will be the second caller.
--
--   CURRENT_ROLE(), not IS_ROLE_IN_SESSION(). The latter is true for secondary
--   roles and for roles reached through the hierarchy, which is a wider door
--   than this needs. Only a session whose PRIMARY role is CHIP_CHAT_ADMIN qualifies.
--
-- WHY THE ATTACHMENTS ARE IN A SCRIPTING BLOCK. `ALTER TABLE ... ADD ROW ACCESS
-- POLICY` fails if the table already has one, and `DROP` fails if it does not,
-- so neither statement alone is re-runnable and every file here has to be. The
-- block asks POLICY_REFERENCES which tables already carry the policy and emits
-- the DROP+ADD swap for those and a plain ADD for the rest. The swap is a
-- single ALTER on purpose: the two-statement version leaves a window in which
-- the table is unprotected, and a window is a thing somebody eventually queries
-- through.
--
-- A table carrying some OTHER row access policy matches neither branch, so the
-- ADD fails and the apply stops. That is the intended direction: a policy this
-- file did not put there is a finding, not a thing to overwrite quietly.
--
-- WHY THE LIST IS SPELLED OUT. The VALUES list below is what
-- `tests/test_row_access_policies.py` holds against
-- `chip_chat.snowflake.schema.visitor_scoped()`, in both directions, in
-- `make ci`. A new visitor-scoped table added to the schema and not to this
-- list fails the build; a line here for a table nobody declared visitor-scoped
-- fails it too. `snowflake-verify` asks the live account the same question and
-- then proves the check is not asleep, by creating an unprotected table and
-- requiring it to be named.
--
-- CREATE ... IF NOT EXISTS with a body of FALSE, then ALTER ... SET BODY.
-- Snowflake refuses CREATE OR REPLACE on a policy that is attached to anything,
-- so re-asserting the real body has to be an ALTER -- which is also the only
-- form that changes what a policy means without ever detaching it. The created
-- body denies everything, so the half-second between the two statements on a
-- first apply, and any apply that fails between them, leaves the account CLOSED
-- rather than open.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

-- --------------------------------------------------------------------------
-- The policies. Created denying everything; the real bodies are set below.
-- --------------------------------------------------------------------------

CREATE ROW ACCESS POLICY IF NOT EXISTS visitor_isolation
    AS (row_demo_id VARCHAR) RETURNS BOOLEAN -> FALSE;

CREATE ROW ACCESS POLICY IF NOT EXISTS entry_roster
    AS (row_demo_id VARCHAR) RETURNS BOOLEAN -> FALSE;

ALTER ROW ACCESS POLICY visitor_isolation SET BODY ->
    (GETVARIABLE('DEMO_ID') IS NOT NULL AND row_demo_id = GETVARIABLE('DEMO_ID'))
    OR (CURRENT_ROLE() = 'CHIP_CHAT_ADMIN' AND GETVARIABLE('ALL_VISITORS') IS NOT NULL);

ALTER ROW ACCESS POLICY visitor_isolation SET COMMENT =
    'A row belongs to the visitor bound to the session, and to nobody else. Default deny: an unset DEMO_ID returns zero rows rather than all of them, which is the difference between a bug and a breach. Applies to every role -- Snowflake has no owner exemption -- so the ops API''s CHIP_CHAT_WRITE is bound by it exactly as the read lane is. The second clause is the maintenance escape, and it needs BOTH the owner role, which no service user holds, and an ALL_VISITORS variable set on purpose; CHIP_CHAT_ADMIN can already detach this policy, so the clause hands it nothing it did not have. RFC-001 §05.';

ALTER ROW ACCESS POLICY entry_roster SET BODY ->
    GETVARIABLE('DEMO_ID') IS NULL
    OR row_demo_id = GETVARIABLE('DEMO_ID');

ALTER ROW ACCESS POLICY entry_roster SET COMMENT =
    'persona_fixtures only, and the one table in this database whose policy is open when nothing is bound. The roster is what the entry flow chooses a visitor''s synthetic customer FROM, so it is read on a connection that has bound nobody -- and a policy that denied that read would break entry rather than protect it. A session that HAS bound a visitor sees that visitor''s fixture and no other, which is strictly narrower than leaving the table unprotected. snowflake/sql/10_policies.sql argues it in full.';

-- --------------------------------------------------------------------------
-- The attachments. Every visitor-scoped table, and nothing else.
-- --------------------------------------------------------------------------

EXECUTE IMMEDIATE $$
DECLARE
    attachments CURSOR FOR
        WITH wanted AS (
            SELECT * FROM VALUES
                ('ACCOUNTS', 'DEMO_VISITORS',    'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('ACCOUNTS', 'PERSONA_FIXTURES', 'CHIP_CHAT.ACCOUNTS.ENTRY_ROSTER'),
                ('ACCOUNTS', 'ORDERS',           'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('ACCOUNTS', 'ORDER_ITEMS',      'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('ACCOUNTS', 'LOYALTY_LEDGER',   'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('MARTS',    'CUSTOMER_360',     'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('MARTS',    'USUAL_ORDER',      'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'),
                ('MARTS',    'SPEND_SUMMARY',    'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION')
                AS w (table_schema, table_name, policy_name)
        ),
        attached AS (
            SELECT ref_schema_name AS table_schema, ref_entity_name AS table_name
            FROM TABLE(CHIP_CHAT.INFORMATION_SCHEMA.POLICY_REFERENCES(
                POLICY_NAME => 'CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION'))
            WHERE ref_entity_domain = 'TABLE'
            UNION ALL
            SELECT ref_schema_name, ref_entity_name
            FROM TABLE(CHIP_CHAT.INFORMATION_SCHEMA.POLICY_REFERENCES(
                POLICY_NAME => 'CHIP_CHAT.ACCOUNTS.ENTRY_ROSTER'))
            WHERE ref_entity_domain = 'TABLE'
        )
        SELECT
            'ALTER TABLE CHIP_CHAT.' || w.table_schema || '.' || w.table_name
            || CASE
                   WHEN a.table_name IS NULL THEN ''
                   ELSE ' DROP ROW ACCESS POLICY ' || w.policy_name || ','
               END
            || ' ADD ROW ACCESS POLICY ' || w.policy_name || ' ON (demo_id)'
                AS statement
        FROM wanted w
        LEFT JOIN attached a
               ON a.table_schema = w.table_schema
              AND a.table_name = w.table_name
        ORDER BY w.table_schema, w.table_name;
BEGIN
    FOR attachment IN attachments DO
        EXECUTE IMMEDIATE attachment.statement;
    END FOR;
    RETURN 'every visitor-scoped table carries its row access policy';
END;
$$;
