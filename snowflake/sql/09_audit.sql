-- The invariant, as two views: every visitor-scoped table carries demo_id.
--
-- Issue #42's second acceptance criterion asks for this as "a query that fails
-- if one is added without it", and a query is the right shape for it. A test
-- over the checked-in DDL can only see the tables somebody wrote down;
-- `CREATE TABLE` is a thing a person can type into Snowsight at four in the
-- afternoon, and the table they create is exactly as invisible to a row access
-- policy as one that was reviewed.
--
-- IT DEFAULTS TO DENY, which is the only direction that survives the schema
-- growing. A table appearing in ACCOUNTS or MARTS is presumed visitor-scoped
-- until somebody exempts it BY NAME and writes down why, so the failure mode of
-- forgetting is a loud one. The opposite arrangement -- an allow-list of tables
-- to check -- fails silently every time it is not updated, which is every time
-- it matters.
--
-- Two tables are exempt today and both reasons are in the VALUES list below,
-- where they are read alongside the exemption rather than in a comment
-- somewhere else.
--
--   visitor_scoped_tables      every table the demo_id rule applies to. This is
--                              also the list #43's coverage test needs: each of
--                              these must carry a row access policy, and a
--                              table that appears here without one is the
--                              isolation guarantee developing a hole.
--   tables_missing_demo_id     the ones that break the rule. MUST BE EMPTY.
--
--        SELECT * FROM CHIP_CHAT.ACCOUNTS.tables_missing_demo_id;
--
-- Empty is a pass. `make snowflake-verify` runs exactly that and fails on any
-- row, and `test_schema_layout.py` asks the same question of the checked-in DDL
-- for free, offline, in `make ci`. The two are not redundant: the test catches
-- the table somebody wrote and did not think about, the view catches the table
-- nobody wrote down at all.
--
-- ONE TRAP, and it is the reason `verify` runs this as CHIP_CHAT_ADMIN.
-- INFORMATION_SCHEMA shows a session only the objects its role may see, and
-- that filtering is applied even through a view. A role with narrower reach
-- than the admin therefore gets a shorter list and a cleaner-looking answer --
-- the audit would pass by not looking. docs/snowflake-schema.md §6 has the
-- transcript.
--
-- CREATE OR REPLACE is safe here in a way it is not for a table: a view holds
-- no rows, so replacing one destroys nothing, and re-asserting the definition
-- on every apply is how a reworded exemption reaches the account at all.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

CREATE OR REPLACE VIEW visitor_scoped_tables
COMMENT = 'Every table in ACCOUNTS and MARTS that the demo_id rule applies to, which is all of them except the two exempted by name in this view''s own definition. Each must carry a demo_id column (checked by tables_missing_demo_id) and, from #43, a row access policy keyed to the session variable that holds it. Defaults to deny: a new table is on this list until somebody takes it off on purpose.'
AS
WITH exempt AS (
    SELECT * FROM VALUES
        (
            'ACCOUNTS',
            'PERSONAS',
            'Seven archetypes shared by five hundred customers. A row is a kind '
            || 'of person, not a person, so there is no visitor to scope it to '
            || 'and nothing in it is anybody''s.'
        ),
        (
            'MARTS',
            'ITEM_AFFINITY',
            'Which items are ordered together, aggregated over the whole '
            || 'population. A fact about two items and about nobody. It is also '
            || 'the only personalization input a visitor with no history has.'
        )
        AS e (table_schema, table_name, why)
)
SELECT
    t.table_schema,
    t.table_name
FROM CHIP_CHAT.INFORMATION_SCHEMA.TABLES t
WHERE t.table_schema IN ('ACCOUNTS', 'MARTS')
  AND t.table_type = 'BASE TABLE'
  AND NOT EXISTS (
      SELECT 1 FROM exempt e
      WHERE e.table_schema = t.table_schema
        AND e.table_name = t.table_name
  );

CREATE OR REPLACE VIEW tables_missing_demo_id
COMMENT = 'Visitor-scoped tables with no demo_id column. Issue #42''s second acceptance criterion: this view must return zero rows, and a row in it names a table that no row access policy can be attached to -- which is the isolation guarantee of RFC-001 §05 with a hole in it. Query it as CHIP_CHAT_ADMIN: INFORMATION_SCHEMA hides objects a narrower role cannot see, so a lesser role gets a shorter list and a false pass.'
AS
SELECT
    v.table_schema,
    v.table_name
FROM visitor_scoped_tables v
WHERE NOT EXISTS (
    SELECT 1
    FROM CHIP_CHAT.INFORMATION_SCHEMA.COLUMNS c
    WHERE c.table_schema = v.table_schema
      AND c.table_name = v.table_name
      AND c.column_name = 'DEMO_ID'
);
