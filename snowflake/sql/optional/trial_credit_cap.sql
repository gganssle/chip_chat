-- The cap on the whole trial. Opt-in, and outside the default apply on purpose.
--
-- 05_resource_monitors.sql bounds what each of the two CHIP_CHAT warehouses can
-- spend in a DAY, off numbers that come from the shape of the workload. This
-- file bounds what the ACCOUNT can spend in total, off a number that comes from
-- the bill -- and there is no honest default for that, which is the entire
-- reason it sits here next to network_policy.sql rather than in the numbered
-- sequence. Guess it low and the demo suspends mid-conversation. Guess it high
-- and it does nothing at all, which is worse, because then it looks handled.
--
-- Two things only an account-level monitor can do:
--
--   It counts COMPUTE_WH. The warehouse Snowflake created at signup is still
--   the default in the `snow` connection, still auto-suspends after 600
--   seconds, and still has query acceleration on. `snowflake/sql/` does not
--   touch it because it did not create it -- see docs/snowflake-account.md
--   section 8 item 7 -- so a Snowsight worksheet left open on a Friday spends
--   ten minutes of idle at a time on a warehouse no monitor in the numbered
--   files is watching. This one watches it, and every warehouse anybody adds
--   later without remembering to write a monitor for it.
--
--   It counts against a total rather than a day. FREQUENCY = NEVER means the
--   used credits are never reset, so this is a floor under the balance rather
--   than a speed limit. The daily monitors cannot express that: at 6 credits a
--   day between them they would let the trial go in three weeks without ever
--   firing a single trigger.
--
-- What it does NOT count: serverless. Resource monitors watch virtual warehouse
-- credits, not Snowpipe, tasks, materialized view maintenance, search
-- optimization, Cortex or query acceleration. That last one is why
-- 01_warehouses.sql sets ENABLE_QUERY_ACCELERATION = FALSE on both warehouses
-- rather than trusting a cap to catch it -- a serverless pool is exactly the
-- spend that does not show up where you look for it, and this file is one of
-- the places you would look.
--
-- CHOOSING THE NUMBER. The quota is counted from the moment this monitor is
-- first created, not from the start of the trial, so what to pass is the
-- credits you are prepared to spend FROM NOW -- the remaining balance, less
-- whatever you want left over to rebuild with. Two ways to find where you are:
--
--   Snowsight → Admin → Cost Management shows the remaining trial balance in
--   DOLLARS. Divide by the account's credit price to get credits: Enterprise
--   list is about $3, so the $400 trial is roughly 130 credits.
--
--   From SQL, what has been spent so far, in credits:
--     SELECT ROUND(SUM(credits_used), 1) AS credits_used_to_date
--       FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY;
--   ACCOUNT_USAGE lags by up to two hours, so treat it as a floor.
--
-- `make snowflake-cap QUOTA=<credits>` runs this file and refuses a quota the
-- account has already spent past, which is the one wrong number that suspends
-- everything the moment you press return.
--
--   make snowflake-cap QUOTA=60
--
-- or by hand:
--
--   snow sql -c chipchat -f snowflake/sql/optional/trial_credit_cap.sql \
--     -D "trial_credit_quota=60"
--
-- To undo: ALTER ACCOUNT UNSET RESOURCE_MONITOR; and then DROP RESOURCE MONITOR
-- CHIP_CHAT_TRIAL_MONITOR. Unset first -- a monitor assigned to the account
-- cannot be dropped.
--
-- A rebuild removes it. optional/reset.sql drops this monitor along with
-- everything else, and the numbered apply does not put it back, so
-- `make snowflake-rebuild` leaves the account uncapped until this is re-run.
-- `make snowflake-verify` fails on exactly that, by name, rather than letting a
-- missing cap be quiet.
--
-- Re-running is safe and does not reset the count: CREATE ... IF NOT EXISTS
-- owns FREQUENCY and START_TIMESTAMP, which are the two properties that restart
-- the counting period when they are set, and the ALTER touches neither.

USE ROLE ACCOUNTADMIN;

CREATE RESOURCE MONITOR IF NOT EXISTS CHIP_CHAT_TRIAL_MONITOR
    WITH CREDIT_QUOTA = <% trial_credit_quota %>
    FREQUENCY = NEVER
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 50 PERCENT DO NOTIFY
        ON 75 PERCENT DO NOTIFY
        ON 90 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

ALTER RESOURCE MONITOR CHIP_CHAT_TRIAL_MONITOR SET
    CREDIT_QUOTA = <% trial_credit_quota %>
    TRIGGERS
        ON 50 PERCENT DO NOTIFY
        ON 75 PERCENT DO NOTIFY
        ON 90 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

-- The account level, not a warehouse. A warehouse keeps its own monitor from
-- 05_resource_monitors.sql; Snowflake applies both, and the stricter one wins.
ALTER ACCOUNT SET RESOURCE_MONITOR = CHIP_CHAT_TRIAL_MONITOR;
