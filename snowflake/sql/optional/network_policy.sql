-- Network policies. Opt-in, and outside the default apply on purpose.
--
-- Issue #41 asks for a network policy "if the trial edition supports it,
-- restricting to the app's egress". It does support it -- this account is an
-- Enterprise trial and `CREATE NETWORK POLICY` works. What it cannot support is
-- a policy written before anybody knows the egress addresses, which is why this
-- file is a template with no defaults and why `snowflake-apply` does not run
-- the files in this directory.
--
-- The policies attach to USERS, not to the account. An account-level policy is
-- the standard way to lock an entire Snowflake account out of the internet, and
-- also the standard way to lock yourself out of your own account at 11pm.
-- Attaching per-user gets the property the issue actually wants -- the ops API's
-- credentials are useless from anywhere but the ops API -- while leaving the
-- operator's own login on whatever address they happen to be at.
--
-- Two policies because there are two egress points. CHIP_CHAT_APP and
-- CHIP_CHAT_OPS both run in Azure eastus2; CHIP_CHAT_PUBLISHER runs from the
-- Databricks workspace, which NATs from its own addresses.
--
-- Where the addresses come from:
--
--   Azure       az containerapp env show -n <env> -g <rg> \
--                 --query properties.staticIp -o tsv
--               and the Function App's outbound set:
--               az functionapp show -n <app> -g <rg> \
--                 --query possibleOutboundIpAddresses -o tsv
--   Databricks  the workspace's NAT addresses, from the workspace's networking
--               page. On the default (Databricks-managed VNet) deployment these
--               are not pinned and can change; if they are not stable, leave
--               CHIP_CHAT_PUBLISHER unrestricted rather than pinning something
--               that will break the nightly job in a fortnight.
--
-- Run it deliberately, one variable per policy, CIDRs comma-separated:
--
--   snow sql -c chipchat -f snowflake/sql/optional/network_policy.sql \
--     -D "azure_cidrs='20.1.2.3/32','20.1.2.4/32'" \
--     -D "databricks_cidrs='52.5.6.7/32'"
--
-- To undo: ALTER USER <user> UNSET NETWORK_POLICY.
--
-- One warning worth having in advance. Snowflake refuses to activate a policy
-- that would block the session activating it, which protects you at the account
-- level and does NOT protect you here: nothing about the operator's address is
-- checked when a policy is attached to a service user. Get the addresses wrong
-- and the failure surfaces as the app losing its database, not as an error on
-- this statement.

USE ROLE SECURITYADMIN;

CREATE OR REPLACE NETWORK POLICY CHIP_CHAT_AZURE_EGRESS
    ALLOWED_IP_LIST = (<% azure_cidrs %>)
    COMMENT = 'Where the chat app and the ops API leave Azure from. Attached to CHIP_CHAT_APP and CHIP_CHAT_OPS, not to the account.';

CREATE OR REPLACE NETWORK POLICY CHIP_CHAT_DATABRICKS_EGRESS
    ALLOWED_IP_LIST = (<% databricks_cidrs %>)
    COMMENT = 'Where the nightly publish leaves the Databricks workspace from. Attached to CHIP_CHAT_PUBLISHER only.';

USE ROLE USERADMIN;

ALTER USER CHIP_CHAT_APP       SET NETWORK_POLICY = CHIP_CHAT_AZURE_EGRESS;
ALTER USER CHIP_CHAT_OPS       SET NETWORK_POLICY = CHIP_CHAT_AZURE_EGRESS;
ALTER USER CHIP_CHAT_PUBLISHER SET NETWORK_POLICY = CHIP_CHAT_DATABRICKS_EGRESS;
