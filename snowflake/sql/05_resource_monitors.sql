-- Two resource monitors, and an asymmetry between them that is the whole point.
--
-- 01_warehouses.sql is the per-query half of the cost story: X-Small, sixty
-- second auto-suspend, no query acceleration, a statement timeout on each lane.
-- All of it bounds what ONE query costs. None of it bounds the TOTAL. Nothing
-- there stops a nightly publish that loops, a Snowsight worksheet left open on
-- a Friday, or a Cortex Analyst query that a language model wrote badly from
-- spending the trial in a weekend. A resource monitor is the only object in
-- Snowflake that can, because it is the only one that counts credits rather
-- than seconds.
--
-- The account is a 30-day Enterprise trial from 2026-08-25, capped at $400 of
-- credits, whichever comes first. Around $3 a credit at Enterprise list, so
-- roughly 130 credits, or an even burn of about 4.4 credits a day. Both quotas
-- below are read off that daily figure and off what the workload can plausibly
-- cost -- deliberately NOT off the remaining balance, which changes every day
-- and which no checked-in file can know. The cap that IS read off the balance
-- is the account-wide one, and it lives in optional/trial_credit_cap.sql
-- because choosing its number needs an operator and a look at the bill.
--
--   CHIP_CHAT_SERVING_MONITOR   4 credits/day   notify 50/80/100, suspend 300
--   CHIP_CHAT_PUBLISH_MONITOR   2 credits/day   notify 80,       suspend 100
--
-- The asymmetry is the design. A suspended publish costs a stale mart until
-- tomorrow. A suspended serving warehouse costs the demo, mid-conversation, in
-- front of whoever was being shown it. So the publish warehouse is suspended AT
-- its quota, and the serving warehouse is only suspended at three times its --
-- twelve credits in one day, when every statement on it times out at sixty
-- seconds and the warehouse goes idle sixty seconds after the last one. There
-- is no legitimate demo day on the other side of that line; there is only
-- something that has come loose. Both quotas reset DAILY, so a suspension is
-- over by tomorrow rather than for the rest of the trial.
--
-- Between the two thresholds the serving monitor only notifies, which is the
-- honest thing for a number that a genuinely busy day can reach: 4 credits of
-- serving in a day is a thirtieth of the trial, which is worth being told about
-- and is not worth ending a conversation over.
--
-- Two things about resource monitors that this file is shaped around:
--
-- NOTIFY GOES NOWHERE BY DEFAULT. A trigger whose action is NOTIFY sends email
-- to the users in NOTIFY_USERS, and to account administrators who have both a
-- verified email address and notifications switched on. A fresh trial account
-- has neither. Nothing here sets NOTIFY_USERS -- a checked-in file cannot know
-- the operator's Snowflake user name, and re-asserting the list on every apply
-- would revoke whoever the operator added, which is the same argument that
-- keeps RSA_PUBLIC_KEY out of 04_users.sql. Turn the notifications on by hand,
-- once, and docs/snowflake-account.md section 8 item 8 says how. The SUSPEND
-- triggers work whether or not anybody is reading email, which is why the
-- guardrail does not rest on them.
--
-- A MONITOR CANNOT BE MADE IDEMPOTENT THE WAY A WAREHOUSE CAN. 01_warehouses
-- re-asserts every property on every apply, so a setting somebody widened in
-- the UI is narrowed again. That trick is unsafe here: FREQUENCY may only be
-- set together with START_TIMESTAMP, and setting START_TIMESTAMP restarts the
-- counting period -- so an apply that re-asserted the frequency would zero
-- used_credits and hand a runaway a fresh quota every time somebody ran make.
-- CREATE ... IF NOT EXISTS therefore owns the frequency and the start, and the
-- ALTER re-asserts only the quota and the triggers, which is everything that
-- can be widened by hand. The one property an apply cannot narrow again is the
-- frequency, and `make snowflake-verify` checks it against account.py instead.
--
-- Resource monitors are ACCOUNTADMIN objects: there is no SYSADMIN path to
-- creating one, and assigning one to a warehouse needs the same role.

USE ROLE ACCOUNTADMIN;

CREATE RESOURCE MONITOR IF NOT EXISTS CHIP_CHAT_SERVING_MONITOR
    WITH CREDIT_QUOTA = 4
    FREQUENCY = DAILY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 50 PERCENT DO NOTIFY
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO NOTIFY
        ON 300 PERCENT DO SUSPEND
        ON 400 PERCENT DO SUSPEND_IMMEDIATE;

ALTER RESOURCE MONITOR CHIP_CHAT_SERVING_MONITOR SET
    CREDIT_QUOTA = 4
    TRIGGERS
        ON 50 PERCENT DO NOTIFY
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO NOTIFY
        ON 300 PERCENT DO SUSPEND
        ON 400 PERCENT DO SUSPEND_IMMEDIATE;

CREATE RESOURCE MONITOR IF NOT EXISTS CHIP_CHAT_PUBLISH_MONITOR
    WITH CREDIT_QUOTA = 2
    FREQUENCY = DAILY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 120 PERCENT DO SUSPEND_IMMEDIATE;

ALTER RESOURCE MONITOR CHIP_CHAT_PUBLISH_MONITOR SET
    CREDIT_QUOTA = 2
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 120 PERCENT DO SUSPEND_IMMEDIATE;

-- One monitor per warehouse, assigned rather than shared. A monitor covering
-- both would let the publish spend the serving lane's quota and suspend a
-- conversation for a batch job's mistake, which is the failure 01_warehouses
-- spent two separate warehouses avoiding.
ALTER WAREHOUSE CHIP_CHAT_SERVING_WH SET RESOURCE_MONITOR = CHIP_CHAT_SERVING_MONITOR;
ALTER WAREHOUSE CHIP_CHAT_PUBLISH_WH SET RESOURCE_MONITOR = CHIP_CHAT_PUBLISH_MONITOR;
