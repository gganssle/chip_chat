-- Two warehouses, both X-Small, both suspending after sixty seconds.
--
-- Issue #41 asks for the 60 second auto-suspend as a REQUIREMENT rather than a
-- default, and for serving and the nightly publish to be separated "so a heavy
-- batch job cannot make a conversation slow". The account is a 30-day trial
-- capped at $400 of credits, whichever comes first, so an idle warehouse does
-- not merely cost money -- it shortens the trial.
--
-- The separation is enforced by the warehouse GRANTS in 03_grants.sql, not by
-- convention: CHIP_CHAT_READ and CHIP_CHAT_WRITE hold USAGE on the serving
-- warehouse only, CHIP_CHAT_PUBLISH on the publish warehouse only. Neither can
-- name the other's compute, so "the batch job used the serving warehouse" is
-- not a mistake anybody is in a position to make.
--
-- Three settings here are not the default and each one is a credit story:
--
--   AUTO_SUSPEND = 60             The account's own COMPUTE_WH ships at 600.
--                                 Ten minutes of idle X-Small per conversation
--                                 is most of what a demo account spends.
--   ENABLE_QUERY_ACCELERATION     Ships TRUE on this account's warehouses.
--     = FALSE                     It bills to a separate serverless pool, which
--                                 is exactly the kind of spend that does not
--                                 show up where you look for it. An X-Small
--                                 serving a single conversation has nothing to
--                                 accelerate.
--   STATEMENT_TIMEOUT_IN_SECONDS  A turn that has not answered in a minute has
--     = 60 (serving)              already failed as a conversation; letting its
--                                 query run on is pure cost. The publish
--                                 warehouse gets an hour, because a nightly
--                                 batch legitimately takes minutes.
--
-- INITIALLY_SUSPENDED = TRUE so that a rebuild does not start the meter.
--
-- Idempotent by CREATE IF NOT EXISTS followed by ALTER ... SET: replacing a
-- warehouse would drop the USAGE grants that make the separation real, so the
-- properties are re-asserted on every run instead. A warehouse someone widened
-- by hand in the UI is narrowed again by the next apply, which is the point.

USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS CHIP_CHAT_SERVING_WH
    WAREHOUSE_SIZE = XSMALL
    INITIALLY_SUSPENDED = TRUE
    COMMENT = 'Every turn of every conversation. X-Small, suspends after 60s idle.';

ALTER WAREHOUSE CHIP_CHAT_SERVING_WH SET
    WAREHOUSE_SIZE = XSMALL
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    MIN_CLUSTER_COUNT = 1
    MAX_CLUSTER_COUNT = 1
    SCALING_POLICY = STANDARD
    ENABLE_QUERY_ACCELERATION = FALSE
    STATEMENT_TIMEOUT_IN_SECONDS = 60
    STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 15;

CREATE WAREHOUSE IF NOT EXISTS CHIP_CHAT_PUBLISH_WH
    WAREHOUSE_SIZE = XSMALL
    INITIALLY_SUSPENDED = TRUE
    COMMENT = 'The nightly publish out of Databricks (#39). Separate compute so a batch cannot queue behind, or in front of, a conversation.';

ALTER WAREHOUSE CHIP_CHAT_PUBLISH_WH SET
    WAREHOUSE_SIZE = XSMALL
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    MIN_CLUSTER_COUNT = 1
    MAX_CLUSTER_COUNT = 1
    SCALING_POLICY = STANDARD
    ENABLE_QUERY_ACCELERATION = FALSE
    STATEMENT_TIMEOUT_IN_SECONDS = 3600
    STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 300;
