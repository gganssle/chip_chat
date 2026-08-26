-- Roles. Four of them, and the shape of the graph is the security argument.
--
-- Issue #41: "a read-only role for questions and a separate role for writes".
-- The split is a boundary, not a filing convention. CHIP_CHAT_READ is what
-- Cortex Analyst runs as on the account lane, over SQL a language model wrote,
-- against text a stranger typed. It has to be structurally incapable of
-- writing, so that #83's attempt to talk the model into a write without a
-- confirmation meets a privilege error rather than a prompt.
--
-- READ, WRITE and PUBLISH are SIBLINGS, deliberately. The obvious hierarchy --
-- write inherits read -- is wrong here, because the write role's read surface
-- is meant to be NARROWER than the read role's, not wider: the ops API has no
-- business reading the personalization marts. A ladder cannot express that. Three
-- disjoint grants can.
--
-- All three are granted to CHIP_CHAT_ADMIN, and CHIP_CHAT_ADMIN to SYSADMIN, so
-- that an operator can assume any lane's role to check what it can do. That is
-- an administrative path, not a privilege escalation: a role can only assume
-- roles granted TO it, so READ still cannot reach WRITE. No service user is
-- granted more than one of them -- see 04_users.sql.
--
-- Idempotent: CREATE ROLE IF NOT EXISTS, never OR REPLACE. Replacing a role
-- drops every grant made to it, which on a re-run would silently strip the
-- account down to whatever this file happens to say today.

USE ROLE USERADMIN;

CREATE ROLE IF NOT EXISTS CHIP_CHAT_ADMIN
    COMMENT = 'Owns the CHIP_CHAT database, its schemas and everything in them. The role a rebuild runs as. Not a runtime identity.';

CREATE ROLE IF NOT EXISTS CHIP_CHAT_READ
    COMMENT = 'Questions. The account and personalization lanes, and Cortex Analyst. SELECT only, on all three schemas. Issue #41 acceptance criterion 2 is that this role is refused a write.';

CREATE ROLE IF NOT EXISTS CHIP_CHAT_WRITE
    COMMENT = 'Writes. The ops API and nothing else. DML on ACCOUNTS, SELECT on CATALOGUE for pricing, and nothing at all on MARTS.';

CREATE ROLE IF NOT EXISTS CHIP_CHAT_PUBLISH
    COMMENT = 'The nightly Databricks publish (#39). Writes CATALOGUE and MARTS, cannot see ACCOUNTS, and runs on its own warehouse so a batch job cannot slow a conversation.';

USE ROLE SECURITYADMIN;

GRANT ROLE CHIP_CHAT_READ    TO ROLE CHIP_CHAT_ADMIN;
GRANT ROLE CHIP_CHAT_WRITE   TO ROLE CHIP_CHAT_ADMIN;
GRANT ROLE CHIP_CHAT_PUBLISH TO ROLE CHIP_CHAT_ADMIN;
GRANT ROLE CHIP_CHAT_ADMIN   TO ROLE SYSADMIN;
