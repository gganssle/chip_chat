-- Three service users, none of which this file can log in as.
--
-- Issue #41: "only the ops API gets the write role, and it gets it through its
-- own credentials". One user per lane is what makes that sentence checkable --
-- a shared user with two roles granted to it is a boundary that exists only in
-- whoever remembers to pass the right --role.
--
--   CHIP_CHAT_APP        the chat app and the agent      CHIP_CHAT_READ
--   CHIP_CHAT_OPS        the Azure Functions ops API     CHIP_CHAT_WRITE
--   CHIP_CHAT_PUBLISHER  the nightly Databricks job      CHIP_CHAT_PUBLISH
--
-- TYPE = SERVICE. A service user cannot sign in to Snowsight and cannot use a
-- password: Snowflake blocks single-factor password authentication for
-- programmatic users, so these authenticate with a key pair and nothing else.
--
-- DEFAULT_SECONDARY_ROLES = () is the setting that makes the split real, and it
-- is the one that is easy to lose. Snowflake's default is ('ALL'), which gives a
-- session every role its user holds, as secondary roles, alongside whichever
-- role it asked for. A user with one role granted is unaffected -- but the day
-- someone grants CHIP_CHAT_WRITE to CHIP_CHAT_APP "temporarily", ALL is what
-- turns that into a read lane that can write, silently, without the connection
-- string changing. Empty means the session has exactly the role it named.
--
-- NO CREDENTIALS ARE SET HERE. A user created by this file has no password and
-- no public key, and therefore cannot connect at all until an operator attaches
-- one. That is deliberate: a credential in a checked-in file is a credential in
-- everyone's clone. docs/snowflake-account.md §5 has the two commands.
--
-- Which is also why this file is CREATE IF NOT EXISTS and why the ALTERs below
-- never mention RSA_PUBLIC_KEY: re-running an apply must not revoke the keys the
-- operator attached. A rebuild from nothing re-creates keyless users, and says
-- so.

USE ROLE USERADMIN;

CREATE USER IF NOT EXISTS CHIP_CHAT_APP
    TYPE = SERVICE
    COMMENT = 'The chat app and the Foundry agent. Questions only. Key-pair auth; no key is set by snowflake/sql.';

CREATE USER IF NOT EXISTS CHIP_CHAT_OPS
    TYPE = SERVICE
    COMMENT = 'The Azure Functions ops API. The only identity in the account that may write demo account data.';

CREATE USER IF NOT EXISTS CHIP_CHAT_PUBLISHER
    TYPE = SERVICE
    COMMENT = 'The nightly Databricks publish (#39). Writes CATALOGUE and MARTS on the publish warehouse.';

ALTER USER CHIP_CHAT_APP SET
    DEFAULT_ROLE = CHIP_CHAT_READ
    DEFAULT_SECONDARY_ROLES = ()
    DEFAULT_WAREHOUSE = CHIP_CHAT_SERVING_WH
    DEFAULT_NAMESPACE = CHIP_CHAT;

ALTER USER CHIP_CHAT_OPS SET
    DEFAULT_ROLE = CHIP_CHAT_WRITE
    DEFAULT_SECONDARY_ROLES = ()
    DEFAULT_WAREHOUSE = CHIP_CHAT_SERVING_WH
    DEFAULT_NAMESPACE = CHIP_CHAT.ACCOUNTS;

ALTER USER CHIP_CHAT_PUBLISHER SET
    DEFAULT_ROLE = CHIP_CHAT_PUBLISH
    DEFAULT_SECONDARY_ROLES = ()
    DEFAULT_WAREHOUSE = CHIP_CHAT_PUBLISH_WH
    DEFAULT_NAMESPACE = CHIP_CHAT.MARTS;

USE ROLE SECURITYADMIN;

GRANT ROLE CHIP_CHAT_READ    TO USER CHIP_CHAT_APP;
GRANT ROLE CHIP_CHAT_WRITE   TO USER CHIP_CHAT_OPS;
GRANT ROLE CHIP_CHAT_PUBLISH TO USER CHIP_CHAT_PUBLISHER;
