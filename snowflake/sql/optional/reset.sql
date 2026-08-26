-- Take the account back to the morning of the trial. Destructive, on purpose.
--
-- Issue #41's fourth acceptance criterion is that the entire account is
-- rebuildable from `snowflake/` in one run. A claim like that is only worth
-- anything if somebody has actually torn the account down and put it back, so
-- this file exists to make that possible -- and `make snowflake-rebuild` does
-- exactly that sequence, which is how the criterion was checked rather than
-- asserted.
--
-- DROP DATABASE CHIP_CHAT drops every table in it. After the trial that is a
-- rebuild; during it, it is the demo data. `snowflake-reset` asks before it runs
-- for that reason. Snowflake's DROP is a soft delete -- UNDROP DATABASE
-- CHIP_CHAT works for the retention period, which on this account is one day --
-- so this is recoverable for about as long as it takes to notice.
--
-- Things this deliberately does NOT drop: COMPUTE_WH and the other warehouses
-- Snowflake created at signup, the GRAM user, and anything under SNOWFLAKE_ or
-- SYSTEM$. Nothing in snowflake/sql created them and a reset has no business
-- removing what it did not make.

USE ROLE USERADMIN;

DROP USER IF EXISTS CHIP_CHAT_APP;
DROP USER IF EXISTS CHIP_CHAT_OPS;
DROP USER IF EXISTS CHIP_CHAT_PUBLISHER;

USE ROLE SECURITYADMIN;

-- Network policies before the users that referenced them are gone, in case an
-- apply attached them; a policy in use cannot be dropped, and by here nothing
-- uses these.
DROP NETWORK POLICY IF EXISTS CHIP_CHAT_AZURE_EGRESS;
DROP NETWORK POLICY IF EXISTS CHIP_CHAT_DATABRICKS_EGRESS;

USE ROLE SYSADMIN;

DROP WAREHOUSE IF EXISTS CHIP_CHAT_SERVING_WH;
DROP WAREHOUSE IF EXISTS CHIP_CHAT_PUBLISH_WH;

USE ROLE ACCOUNTADMIN;

DROP DATABASE IF EXISTS CHIP_CHAT;

USE ROLE USERADMIN;

DROP ROLE IF EXISTS CHIP_CHAT_READ;
DROP ROLE IF EXISTS CHIP_CHAT_WRITE;
DROP ROLE IF EXISTS CHIP_CHAT_PUBLISH;
DROP ROLE IF EXISTS CHIP_CHAT_ADMIN;
