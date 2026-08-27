-- The nightly demo-data reset. Issue #47, and it is an AGEING job rather than
-- a truncating one -- which is a decision somebody else made and this file
-- implements.
--
-- READ THIS FIRST: WHY IT DOES NOT TRUNCATE. Issue #9 decided that a visitor's
-- state PERSISTS between visits, via a cookie. A nightly `TRUNCATE` of the
-- visitor-scoped tables would therefore empty the account of a returning
-- visitor mid-story, which is the cold-start failure the PRD calls the single
-- largest threat to the demo -- an assistant with no history to be personal
-- about is the thing this whole system exists not to be. #9's own consequence
-- (2) names the resolution: expire on last-seen, then put that visitor back.
-- RFC-001 §13 Q4 anticipated exactly this and is answered by it.
--
-- SO WHAT DOES A VISITOR ACTUALLY CHANGE. Two things, and this file undoes
-- exactly those two:
--
--   ROWS THEY ADDED are all in the live identifier band. #46's procedures mint
--   ord-9000001 and loy-9000001 upward from two sequences, and data-gen
--   produces tens of thousands of rows numbered from one, so an identifier at
--   or above the band is one the demo wrote. 07_accounts.sql's sequence header
--   argues the band; this is the file that spends it. Plus every
--   action_receipts row, which only a live write creates.
--
--   COLUMNS THEY EDITED are all on demo_visitors: the three EDITABLE_COLUMNS,
--   plus thread_id and last_seen, which are the app's own session bookkeeping
--   on the same row.
--
-- Generated history is never touched. That is what makes "restores generated
-- state exactly" a restore rather than a reload, and it is why this job is
-- safe to run against a live account at all: the rows a conversation cites are
-- not rows this job can reach.
--
-- WHERE THE GENERATED STATE OF demo_visitors COMES FROM. ACCOUNTS.
-- demo_visitor_baseline, in 07_accounts.sql, loaded from the generator's own
-- demo_visitors.jsonl in the same run as demo_visitors itself. Three of the
-- five columns are generated NON-NULL for some customers, so restoring them to
-- NULL would be a reset that quietly edited customers it was meant to leave
-- alone; and display_name exists on no other table in this database, because
-- persona_fixtures deliberately carries a narrative and no name.
--
-- A VISITOR WITH NO BASELINE ROW IS NEVER AGED OUT. The cursor below INNER
-- JOINs the baseline, and the count of visitors it excluded for that reason is
-- on the receipt as `held_no_baseline`. The failure this forecloses is the one
-- that matters: a reset that ran against an unloaded baseline would delete
-- every live row and restore nothing, which looks like a working reset and is
-- a visitor whose name is now whatever the last stranger typed.
--
-- WHY IT DOES NOT TRUST last_seen ALONE, WHICH IS THE SUBTLE PART. #9 says
-- last_seen becomes load-bearing. Today only update_preferences writes it
-- (12_procedures.sql); place_order, redeem_points and cancel_order do not, and
-- a read-only conversational turn writes nothing anywhere. A job that aged on
-- last_seen alone would therefore age out a visitor who has been ordering all
-- afternoon, because their last_seen is still the timestamp of their last
-- GENERATED order eighteen months ago.
--
-- So a visitor's activity is the GREATEST of four clocks: last_seen, their
-- most recent action_receipts row, their most recent live-band order, and
-- their most recent live-band ledger entry. Receipts cover every write action
-- by construction -- #46 makes each of the four procedures claim a retry key
-- before it writes anything -- so any visitor who has DONE something has a
-- trustworthy clock.
--
-- AND A VISITOR WITH NO TRUSTWORTHY CLOCK IS HELD, NOT AGED. A visitor who has
-- only talked has a thread_id and nothing dated: last_seen is still the
-- baseline's, and there is no receipt and no live row. This job cannot tell
-- whether they left last week or are mid-sentence, so it does not guess. It
-- leaves them alone and puts the count on the receipt as `held_no_clock`.
-- A non-zero number there means the app tier is not touching last_seen when it
-- binds a session, which is the fix -- both columns are the same row's session
-- bookkeeping and whatever writes thread_id is the thing that should write
-- last_seen beside it.
--
-- Holding is affordable because the population of visitors is BOUNDED. Only a
-- persona_fixtures demo_id is ever handed to anybody, and switching personas
-- chooses another fixture rather than inventing a customer, so the number of
-- rows this job can ever have work to do about is the size of the roster. Demo
-- data grows in rows per visitor, which the ops API's rate limits bound, and
-- not in visitors.
--
-- WHY IT NEEDS THE MAINTENANCE ESCAPE, AND WHY IT REFUSES WITHOUT IT. #43's
-- row access policies filter SELECT, UPDATE and DELETE. An admin session that
-- has bound no visitor reads zero rows and DELETES zero rows -- default deny
-- survives for the owner, which is the property 10_policies.sql is proudest
-- of. So this sets ALL_VISITORS, exactly as `load.py` does, and 10_policies.sql
-- names this job as the second caller.
--
-- The important half is the check after the SET. Without it the failure mode is
-- a job that runs every night, deletes nothing, restores nothing, reports a
-- clean run and lets the personas drift for a month. A reset that silently does
-- nothing is worse than one that fails, so this one fails: it asks whether the
-- escape is actually in effect and returns MAINTENANCE_ESCAPE_UNAVAILABLE if it
-- is not. It also UNSETs on every exit path, including the exception path -- an
-- operator's next query in the same session should not be quietly
-- cross-visitor because a reset left the door open.
--
-- WHY ONE TRANSACTION PER VISITOR. Issue #47's third acceptance criterion is
-- that running this during live traffic does not produce an error for an active
-- visitor. Two things get that. An active visitor is out of scope by
-- construction, because activity is what the cutoff is measured on. And a
-- visitor who returns at the exact moment their own reset is running sees
-- either their whole live state or none of it, never four deletes' worth --
-- one transaction per visitor is what buys that, and it is why this is a loop
-- rather than five set-based statements over the whole aged cohort.
--
-- The cost of the loop is that a run is not atomic ACROSS visitors: a failure
-- halfway leaves the earlier ones reset and the later ones not. That is the
-- right trade. Every visitor is independently correct, the job is idempotent,
-- and tomorrow's run finishes the list -- whereas one transaction over the
-- whole cohort would hold locks on orders and loyalty_ledger for the length of
-- the run, and the ops API writes those tables.
--
-- WHAT IT DELIBERATELY DOES NOT TOUCH. The catalogue and the four gold marts,
-- which #39's nightly publish owns; #47's scope says so and the marts are
-- recomputed from generated history anyway, which this job leaves alone. And
-- the Foundry thread behind a retired thread_id: nothing in Snowflake can call
-- Azure, so this clears the POINTER and returns the ids it cleared on the
-- receipt, under `threads_retired`, for whoever can.
--
-- THE ONE OPEN CONFLICT, WHICH IS #39's AND NOT THIS FILE'S. The nightly
-- publish does INSERT OVERWRITE on orders, order_items and loyalty_ledger, so
-- it erases live-band rows for EVERY visitor, aged out or not.
-- docs/nightly-publish.md §7 hands that decision here, and the decision is: a
-- visitor's live rows survive until that visitor ages out, because that is what
-- #9 bought and a demo where yesterday's order vanished overnight has not
-- bought it. The publish is what has to change; docs/demo-reset.md §6 states
-- it and it is tracked separately. This job is correct either way -- it deletes
-- what is there -- but with the publish unchanged, `orders_deleted` will read
-- zero most mornings for a reason that is not "nothing happened".

-- The one privilege this feature needs, and it is account-level rather than a
-- lane boundary, which is why it is here and not in 03_grants.sql. A task
-- cannot be resumed by a role that does not hold EXECUTE TASK, and the failure
-- is at RESUME rather than at CREATE -- an apply without this line installs a
-- schedule that never fires. It goes to CHIP_CHAT_ADMIN, which owns everything
-- in this database and which no service user holds. Deleting this feature is
-- deleting this file: `optional/reset.sql` drops the database, which takes the
-- task, and drops the role, which takes the grant.
USE ROLE ACCOUNTADMIN;

GRANT EXECUTE TASK ON ACCOUNT TO ROLE CHIP_CHAT_ADMIN;

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

CREATE OR REPLACE PROCEDURE reset_demo_sessions(
    TTL_DAYS NUMBER,
    DRY_RUN BOOLEAN
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Age demo sessions out and put those visitors back the way the generator made them. Issue #47. Deletes only what a visitor added -- live-band orders, their lines, live-band ledger entries and every action receipt -- and restores only what a visitor could edit, from ACCOUNTS.demo_visitor_baseline. Generated history is never touched, so this is a restore rather than a reload. A visitor counts as active on the LATEST of four clocks: demo_visitors.last_seen, their newest action receipt, their newest live order and their newest live ledger entry; one with no dated activity at all is held rather than guessed about, and the count is on the receipt. One transaction per visitor, so nobody is ever half-reset. Needs CHIP_CHAT_ADMIN and sets #43''s ALL_VISITORS escape itself, then refuses to run if the escape did not take -- a reset that silently deletes nothing is worse than one that fails. Pass DRY_RUN => TRUE to see what a run would do without doing it. Rejections: TTL_TOO_SHORT, BASELINE_NOT_LOADED, MAINTENANCE_ESCAPE_UNAVAILABLE, RESET_FAILED.'
EXECUTE AS CALLER
AS
$$
DECLARE
    -- The same number as `chip_chat.snowflake.procedures.LIVE_ID_BAND`, and
    -- `tests/test_demo_reset.py` fails if the two drift. An identifier at or
    -- above this is one the demo wrote; below it is generated history, which
    -- this job must not be able to reach even by accident.
    LIVE_ID_BAND NUMBER DEFAULT 9000001;

    -- The shortest TTL this procedure will accept, in days. Not a preference:
    -- api/src/chip_chat/api/app.py sets the session cookie with
    -- max_age=86_400, so a visitor can return to their demo_id for a day and
    -- then never again. A TTL below that would age out visitors who could
    -- still come back, which is the failure #9 was decided to avoid.
    MIN_TTL_DAYS NUMBER DEFAULT 2;

    cutoff              TIMESTAMP_NTZ;
    started_at          TIMESTAMP_NTZ;
    escape_set          BOOLEAN DEFAULT FALSE;
    baseline_rows       NUMBER  DEFAULT 0;
    dirty_total         NUMBER  DEFAULT 0;
    held_no_clock       NUMBER  DEFAULT 0;
    held_no_baseline    NUMBER  DEFAULT 0;
    visitors_aged       NUMBER  DEFAULT 0;
    orders_deleted      NUMBER  DEFAULT 0;
    lines_deleted       NUMBER  DEFAULT 0;
    ledger_deleted      NUMBER  DEFAULT 0;
    receipts_deleted    NUMBER  DEFAULT 0;
    threads_retired     ARRAY   DEFAULT ARRAY_CONSTRUCT();
    in_txn              BOOLEAN DEFAULT FALSE;
    who                 VARCHAR;
    thread              VARCHAR;
BEGIN
    started_at := SYSDATE();

    IF (TTL_DAYS IS NULL OR TTL_DAYS < :MIN_TTL_DAYS) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'RESET_DEMO_SESSIONS', 'rejection', 'TTL_TOO_SHORT',
            'ttl_days', :TTL_DAYS, 'minimum_days', :MIN_TTL_DAYS,
            'detail', 'the session cookie lives 86400 seconds, so a visitor can return for a day. A TTL shorter than that ages out visitors who could still come back, which is the persistence #9 decided on being undone by the job that was supposed to respect it');
    END IF;

    -- #43's escape, asked for by name. Setting it is not a widening: the policy
    -- honours it only for a session whose PRIMARY role is CHIP_CHAT_ADMIN,
    -- which no service user holds and which can detach the policy outright
    -- anyway. Anybody who can call this could already have SET it themselves.
    EXECUTE IMMEDIATE 'SET ALL_VISITORS = ''#47 ageing demo sessions out''';
    escape_set := TRUE;

    IF (CURRENT_ROLE() <> 'CHIP_CHAT_ADMIN' OR GETVARIABLE('ALL_VISITORS') IS NULL) THEN
        EXECUTE IMMEDIATE 'UNSET ALL_VISITORS';
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'RESET_DEMO_SESSIONS',
            'rejection', 'MAINTENANCE_ESCAPE_UNAVAILABLE',
            'current_role', CURRENT_ROLE(),
            'detail', 'this session is not CHIP_CHAT_ADMIN with ALL_VISITORS set, so #43''s row access policies hide every visitor from this job. It would then delete nothing, restore nothing and report a clean run, which is the one failure worth refusing outright');
    END IF;

    SELECT COUNT(*) INTO :baseline_rows FROM CHIP_CHAT.ACCOUNTS.demo_visitor_baseline;
    IF (:baseline_rows = 0) THEN
        EXECUTE IMMEDIATE 'UNSET ALL_VISITORS';
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'RESET_DEMO_SESSIONS', 'rejection', 'BASELINE_NOT_LOADED',
            'detail', 'ACCOUNTS.demo_visitor_baseline is empty, so there is no generated state to restore anybody to. Run chip_chat.snowflake.load over the generated landing zone: it fills this table from the same demo_visitors.jsonl it fills demo_visitors from');
    END IF;

    -- Everybody the baseline does not carry, counted once and acted on never.
    SELECT COUNT(*) INTO :held_no_baseline
      FROM CHIP_CHAT.ACCOUNTS.demo_visitors v
      LEFT JOIN CHIP_CHAT.ACCOUNTS.demo_visitor_baseline b ON b.demo_id = v.demo_id
     WHERE b.demo_id IS NULL;

    cutoff := DATEADD('day', -:TTL_DAYS, :started_at);

    -- Every visitor this run may act on, and the two facts that decide what
    -- happens to each. Joining the baseline rather than outer-joining it is
    -- load-bearing: a visitor the baseline does not carry is one this job
    -- cannot restore, so it must not delete for them either.
    LET dirty RESULTSET := (
        WITH live_orders AS (
            SELECT demo_id, COUNT(*) AS n, MAX(placed_at) AS at
              FROM CHIP_CHAT.ACCOUNTS.orders
             WHERE TRY_TO_NUMBER(SPLIT_PART(order_id, '-', 2)) >= :LIVE_ID_BAND
             GROUP BY demo_id
        ),
        live_ledger AS (
            SELECT demo_id, COUNT(*) AS n, MAX(created_at) AS at
              FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger
             WHERE TRY_TO_NUMBER(SPLIT_PART(entry_id, '-', 2)) >= :LIVE_ID_BAND
             GROUP BY demo_id
        ),
        receipts AS (
            SELECT demo_id, COUNT(*) AS n, MAX(created_at) AS at
              FROM CHIP_CHAT.ACCOUNTS.action_receipts
             GROUP BY demo_id
        )
        SELECT
            v.demo_id   AS demo_id,
            v.thread_id AS thread_id,
            GREATEST(
                COALESCE(v.last_seen, v.created_at),
                COALESCE(o.at, v.created_at),
                COALESCE(l.at, v.created_at),
                COALESCE(r.at, v.created_at)
            ) AS last_active,
            -- A clock is trustworthy when something dated it. last_seen counts
            -- only once it has MOVED off the baseline, because until then it is
            -- the generator's timestamp rather than a visit.
            (COALESCE(o.n, 0) > 0
             OR COALESCE(l.n, 0) > 0
             OR COALESCE(r.n, 0) > 0
             OR NOT EQUAL_NULL(v.last_seen, b.last_seen)) AS has_clock
        FROM CHIP_CHAT.ACCOUNTS.demo_visitors v
        JOIN CHIP_CHAT.ACCOUNTS.demo_visitor_baseline b ON b.demo_id = v.demo_id
        LEFT JOIN live_orders o ON o.demo_id = v.demo_id
        LEFT JOIN live_ledger l ON l.demo_id = v.demo_id
        LEFT JOIN receipts    r ON r.demo_id = v.demo_id
        -- Dirty, in any of the seven ways a visitor can be. A clean visitor is
        -- one nobody has ever been assigned, and there are five hundred of them.
        WHERE COALESCE(o.n, 0) > 0
           OR COALESCE(l.n, 0) > 0
           OR COALESCE(r.n, 0) > 0
           OR v.thread_id IS NOT NULL
           OR NOT EQUAL_NULL(v.display_name, b.display_name)
           OR NOT EQUAL_NULL(v.home_store_override, b.home_store_override)
           OR NOT EQUAL_NULL(v.stated_preferences, b.stated_preferences)
           OR NOT EQUAL_NULL(v.last_seen, b.last_seen)
        ORDER BY v.demo_id);

    LET aged CURSOR FOR dirty;

    FOR visitor IN aged DO
        dirty_total := :dirty_total + 1;

        IF (NOT visitor.has_clock) THEN
            held_no_clock := :held_no_clock + 1;
            CONTINUE;
        END IF;
        IF (visitor.last_active >= :cutoff) THEN
            CONTINUE;
        END IF;

        who := visitor.demo_id;
        thread := visitor.thread_id;
        visitors_aged := :visitors_aged + 1;

        IF (:DRY_RUN) THEN
            IF (:thread IS NOT NULL) THEN
                threads_retired := ARRAY_APPEND(:threads_retired, :thread);
            END IF;
            CONTINUE;
        END IF;

        in_txn := TRUE;
        BEGIN TRANSACTION;

        -- Lines before orders. The foreign key is declared rather than enforced
        -- here, but a reader who finds order_items pointing at an order that is
        -- gone cannot tell a half-run from a bug.
        DELETE FROM CHIP_CHAT.ACCOUNTS.order_items
              WHERE demo_id = :who
                AND TRY_TO_NUMBER(SPLIT_PART(order_id, '-', 2)) >= :LIVE_ID_BAND;
        lines_deleted := :lines_deleted + SQLROWCOUNT;

        DELETE FROM CHIP_CHAT.ACCOUNTS.orders
              WHERE demo_id = :who
                AND TRY_TO_NUMBER(SPLIT_PART(order_id, '-', 2)) >= :LIVE_ID_BAND;
        orders_deleted := :orders_deleted + SQLROWCOUNT;

        -- Every live ledger row goes: the accrual from a live order, the
        -- negative row cancel_order appends, and a redemption. None of them can
        -- reference a GENERATED order -- cancel_order acts only on PENDING,
        -- which generated history never contains -- so nothing below the band
        -- is left pointing at something that is no longer there.
        DELETE FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger
              WHERE demo_id = :who
                AND TRY_TO_NUMBER(SPLIT_PART(entry_id, '-', 2)) >= :LIVE_ID_BAND;
        ledger_deleted := :ledger_deleted + SQLROWCOUNT;

        -- All of them, not a band: a receipt exists only because a live write
        -- made it, and a spent retry key outliving the order it made replays a
        -- receipt for an order nobody can find.
        DELETE FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :who;
        receipts_deleted := :receipts_deleted + SQLROWCOUNT;

        -- The five columns a live demo can move, and no others. persona_id is
        -- not among them: switching personas chooses a different fixture rather
        -- than editing this row, so a demo_id's archetype is not something a
        -- reset has to put back.
        UPDATE CHIP_CHAT.ACCOUNTS.demo_visitors v
           SET display_name        = b.display_name,
               home_store_override = b.home_store_override,
               stated_preferences  = b.stated_preferences,
               last_seen           = b.last_seen,
               thread_id           = NULL
          FROM CHIP_CHAT.ACCOUNTS.demo_visitor_baseline b
         WHERE v.demo_id = :who
           AND b.demo_id = v.demo_id;

        COMMIT;
        in_txn := FALSE;

        IF (:thread IS NOT NULL) THEN
            threads_retired := ARRAY_APPEND(:threads_retired, :thread);
        END IF;
    END FOR;

    EXECUTE IMMEDIATE 'UNSET ALL_VISITORS';
    escape_set := FALSE;

    RETURN OBJECT_CONSTRUCT_KEEP_NULL(
        'ok', TRUE,
        'action', 'RESET_DEMO_SESSIONS',
        'dry_run', :DRY_RUN,
        'ttl_days', :TTL_DAYS,
        'cutoff', :cutoff,
        'started_at', :started_at,
        'finished_at', SYSDATE(),
        'dirty_visitors', :dirty_total,
        'visitors_aged', :visitors_aged,
        'orders_deleted', :orders_deleted,
        'order_items_deleted', :lines_deleted,
        'ledger_entries_deleted', :ledger_deleted,
        'receipts_deleted', :receipts_deleted,
        -- Foundry threads this job detached and cannot delete: nothing in
        -- Snowflake can call Azure. The pointer is cleared here so that a
        -- returning visitor cannot resume somebody else's conversation;
        -- deleting the thread itself is whoever holds the credential's job.
        'threads_retired', :threads_retired,
        -- Non-zero means the app tier is not touching last_seen when it binds a
        -- session, so visitors who only talked cannot be dated and are being
        -- held rather than guessed about. This file's header has the argument.
        'held_no_clock', :held_no_clock,
        -- Non-zero means demo_visitors carries a customer the baseline does
        -- not, which is a load that ran one half of itself.
        'held_no_baseline', :held_no_baseline);

EXCEPTION
    WHEN OTHER THEN
        IF (in_txn) THEN
            ROLLBACK;
        END IF;
        IF (escape_set) THEN
            EXECUTE IMMEDIATE 'UNSET ALL_VISITORS';
        END IF;
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'RESET_DEMO_SESSIONS', 'rejection', 'RESET_FAILED',
            'sqlcode', SQLCODE, 'sqlerrm', SQLERRM, 'rolled_back', in_txn,
            'visitors_aged', :visitors_aged,
            'detail', 'visitors reset before the failure stay reset. One transaction per visitor means a half-run is a short run rather than a corrupt one, and the next run finishes the list');
END;
$$;

-- --------------------------------------------------------------------------
-- The schedule.
--
-- 09:00 UTC, two hours after #39's publish starts at 07:00. Not because the
-- two jobs need ordering -- this one restores demo_visitors, which the publish
-- cannot even see -- but because both write ACCOUNTS.orders, and a DELETE
-- landing inside an INSERT OVERWRITE is a lock wait at best. Two hours is
-- comfortably past the publish's own timeout.
--
-- It runs on the PUBLISH warehouse. A reset is a batch, and the two warehouses
-- exist so that a batch cannot queue in front of a conversation; the serving
-- warehouse also cancels anything still running after sixty seconds, which a
-- loop over the roster could plausibly exceed.
--
-- CREATE OR ALTER, never CREATE OR REPLACE: replacing a task drops its history,
-- and the history is the only place a run's receipt survives. The RESUME after
-- it is not decoration -- a newly created task is SUSPENDED, so an apply
-- without it installs a schedule that never fires and reports success.
-- --------------------------------------------------------------------------

CREATE OR ALTER TASK reset_demo_sessions_nightly
    WAREHOUSE = CHIP_CHAT_PUBLISH_WH
    SCHEDULE = 'USING CRON 0 9 * * * UTC'
    USER_TASK_TIMEOUT_MS = 900000
    COMMENT = 'Issue #47''s nightly demo-data reset. Calls CHIP_CHAT.ACCOUNTS.reset_demo_sessions with the session TTL, which is two days: the app''s session cookie lives one day, so nobody can return to a demo_id after that, and the second day is slack. 09:00 UTC, two hours after the nightly publish, on the publish warehouse so a batch cannot queue in front of a conversation. The manual trigger is `make snowflake-demo-reset`, which is the same procedure with the same arguments -- the first time a demo goes badly is when it is most needed. docs/demo-reset.md.'
AS
    CALL CHIP_CHAT.ACCOUNTS.reset_demo_sessions(2, FALSE);

ALTER TASK reset_demo_sessions_nightly RESUME;
