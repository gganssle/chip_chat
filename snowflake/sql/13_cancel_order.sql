-- cancel_order, alone in its own file, and the file is the point.
--
-- READ docs/action-surface.md §3 BEFORE READING THE SQL. This procedure models
-- something the real product does not do, and it is the only one of the four
-- that does. The other three are in `12_procedures.sql`; this one is here so
-- that removing it is deleting a file and one entry in `procedures.py` rather
-- than untangling it from three procedures that should survive it.
--
-- WHAT THE PUBLISHED RECORD ACTUALLY SAYS. Not "the window is zero" -- an
-- affirmative refusal, twice, in Chipotle's own FAQ:
--
--   Ordering / General
--     Q: "Can I cancel my online or mobile order after I've submitted it?"
--     A: "When you submit an order, it's sent directly to our restaurant crew,
--        so we're unable to cancel."
--
--   Delivery / Delivery - General
--     A: the order goes to the courier team and the crew; a customer who really
--        must cancel is told to contact Customer Service, and "you may incur a
--        cancelation fee".
--
-- So there are two divergences here and not one: self-service cancellation does
-- not exist, and the escape hatch that does exist is a human and possibly a fee.
-- Both are named on the receipt below, because a demo that cancels cleanly and
-- for free teaches a visitor something about the business that is not true.
--
-- WHY IT EXISTS ANYWAY. PRD T1 lists cancelling a pending order among the six
-- supported actions, and RFC-001 §06 fixes cancel_order(order_id). PRD T5 says
-- every action is simulated and every card says so. docs/action-surface.md §3
-- considered dropping the tool, simulating it silently, and simulating it out
-- loud, and recommended the third. This file is the third.
--
-- THE ONE INVENTED CONSTANT IS :data:`CANCELLATION_WINDOW_MINUTES`, below, and
-- it is invention #1 in docs/action-surface.md §10. Its stated exit is a PRD
-- change dropping T1's cancellation clause.
--
-- WHAT REMOVING THIS WOULD COST, measured rather than guessed, because the
-- operator decision this is waiting on has never had a number attached to it:
--
--   * delete this file, and its name from nothing -- `apply.ordered_files()`
--     globs the directory, so no list mentions it;
--   * delete one entry from `PROCEDURES` in
--     `src/chip_chat/snowflake/procedures.py`, and its four tests in
--     `tests/test_procedure_layout.py` stop having a subject rather than
--     failing -- they iterate `procedures.separable()`;
--   * delete the cancel_order checks from `verify.py`;
--   * run `DROP PROCEDURE CHIP_CHAT.ACCOUNTS.cancel_order(VARCHAR, VARCHAR)`
--     once against the live account. `optional/reset.sql` needs no edit: it
--     drops the database, which takes every procedure with it;
--   * and no migration. No table, no column and no row exists for cancel_order
--     alone: it writes a status value into orders.status that generated
--     history already contains, and a negative row into loyalty_ledger, which
--     is append-only and whose reversal reason is published (the terms reserve
--     Chipotle's right to deduct the points from a purchase that is later
--     voided or cancelled). Orders already cancelled stay cancelled and stay
--     correct, because CANCELLED is a status the demo's own data uses.
--
-- That is the whole cost: three deletions, one DROP, and no data change. It
-- stays that way only as long as nothing else grows a dependency on the
-- procedure, which is what this file being one file is for.
--
-- THE ONE PART OF THIS THAT IS NOT INVENTED is the effect on the ledger. The
-- rewards terms reserve Chipotle's right to deduct the points from a qualifying
-- purchase that is later voided or cancelled, so the reversal is published even
-- though the cancellation is not. It is appended as a negative row rather than
-- applied as an edit to the original, because #27's reconciliation reads an
-- append-only ledger.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

CREATE OR REPLACE PROCEDURE cancel_order(
    RETRY_KEY VARCHAR,
    ORDER_ID VARCHAR
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Cancel one pending order of the visitor bound to this session. THIS MODELS AN AFFORDANCE CHIPOTLE DOES NOT OFFER: the published FAQ answers that a submitted order goes straight to the restaurant crew, so it cannot be cancelled, and a delivery order can only be cancelled by contacting Customer Service, possibly for a fee. Both sentences are on the receipt. The cancellation window is invented and named as such -- docs/action-surface.md section 3 and section 10 row 1. Takes no visitor identifier; an order id belonging to somebody else is ORDER_NOT_FOUND, the same answer an id that never existed gets. Reverses the points the order earned, as a new negative ledger row rather than an edit, which is the one part of this the terms do publish. Idempotent on RETRY_KEY. Rejections: ORDER_NOT_FOUND, ORDER_NOT_CANCELLABLE, CANCELLATION_WINDOW_CLOSED.'
EXECUTE AS CALLER
AS
$$
DECLARE
    -- INVENTED, and the only invented number in the write path. Ten minutes,
    -- chosen so that a visitor who places an order and then asks to cancel it
    -- in the same conversation can, and one who comes back tomorrow cannot --
    -- which is the shape the demo needs and is not a claim about Chipotle. The
    -- real product's self-service window does not exist. `procedures.py` holds
    -- the same number and `test_procedure_layout.py` fails if the two drift,
    -- so the constant cannot quietly become two constants.
    CANCELLATION_WINDOW_MINUTES NUMBER DEFAULT 10;

    visitor       VARCHAR;
    known         NUMBER;
    status        VARCHAR;
    placed_at     TIMESTAMP_NTZ;
    order_total   NUMBER(10,2);
    earned        NUMBER;
    new_entry     VARCHAR;
    cancelled_at  TIMESTAMP_NTZ;
    balance       NUMBER;
    claimed       NUMBER;
    in_txn        BOOLEAN DEFAULT FALSE;
    stored        VARIANT;
    spent_on      VARCHAR;
    receipt       VARIANT;
BEGIN
    visitor := GETVARIABLE('DEMO_ID');
    IF (visitor IS NULL OR visitor = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'SESSION_NOT_BOUND',
            'detail', 'this session has no DEMO_ID set, so there is no visitor whose order this could be');
    END IF;
    IF (RETRY_KEY IS NULL OR RETRY_KEY = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'RETRY_KEY_REQUIRED',
            'detail', 'every write action takes one, so that the four are retried the same way');
    END IF;
    SELECT COUNT(*) INTO :known FROM CHIP_CHAT.ACCOUNTS.demo_visitors WHERE demo_id = :visitor;
    IF (:known = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'VISITOR_NOT_FOUND',
            'detail', 'the session names a visitor this database has no row for');
    END IF;

    -- ---- has this key already been spent? --------------------------------
    --
    -- Read before validating, not after. A retry of a call that SUCCEEDED must
    -- return that call's receipt, and some of the validation below is about
    -- state the first call itself changed -- cancelling an order twice is the
    -- obvious one, where the second attempt would otherwise be told the order
    -- is not cancellable, which is true and is not the answer a retry deserves.
    -- This read sees committed rows only, so two simultaneous first attempts
    -- both miss it; that race is what the MERGE inside the transaction is for.
    SELECT receipt, action INTO :stored, :spent_on
      FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;
    IF (:stored IS NOT NULL) THEN
        IF (:spent_on <> 'CANCEL_ORDER') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- The demo_id predicate is here as well as in #43's policy, and on purpose.
    -- A policy that is detached, or a table that has not had one attached yet,
    -- must not turn this into a procedure that cancels a stranger's order. Two
    -- independent reasons the answer is ORDER_NOT_FOUND is the right number.
    SELECT status, placed_at, total INTO :status, :placed_at, :order_total
      FROM CHIP_CHAT.ACCOUNTS.orders WHERE order_id = :ORDER_ID AND demo_id = :visitor;
    IF (:status IS NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'ORDER_NOT_FOUND',
            'subject', :ORDER_ID,
            'detail', 'no such order for this visitor. A well-formed id belonging to somebody else gets this same answer, which is not a leak and is not a forbidden');
    END IF;
    IF (:status <> 'PENDING') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'ORDER_NOT_CANCELLABLE',
            'subject', :ORDER_ID, 'status', :status,
            'detail', 'only an order still in the demo''s own pre-handoff state can be cancelled here');
    END IF;
    IF (DATEDIFF('minute', :placed_at, SYSDATE()) >= :CANCELLATION_WINDOW_MINUTES) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'CANCELLATION_WINDOW_CLOSED',
            'subject', :ORDER_ID, 'window_minutes', :CANCELLATION_WINDOW_MINUTES,
            'placed_at', :placed_at,
            'detail', 'the demo''s own simulated window has lapsed. Chipotle''s real window is not longer than this one -- there is no self-service cancellation at all, and this window is ours',
            'reality', 'Chipotle cannot normally cancel an order once it has been submitted -- it goes straight to the restaurant crew. This demo can, because nothing here is real.');
    END IF;

    in_txn := TRUE;
    BEGIN TRANSACTION;

    MERGE INTO CHIP_CHAT.ACCOUNTS.action_receipts t
      USING (SELECT :visitor AS demo_id, :RETRY_KEY AS retry_key) s
         ON t.demo_id = s.demo_id AND t.retry_key = s.retry_key
       WHEN NOT MATCHED THEN INSERT (demo_id, retry_key, action, subject_id, receipt, created_at)
            VALUES (s.demo_id, s.retry_key, 'CANCEL_ORDER', NULL, TO_VARIANT('CLAIMED'), SYSDATE());
    claimed := SQLROWCOUNT;

    IF (:claimed = 0) THEN
        SELECT receipt, action INTO :stored, :spent_on
          FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;
        COMMIT;
        IF (:spent_on <> 'CANCEL_ORDER') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    cancelled_at := SYSDATE();

    -- What this order actually earned, rather than what it would earn today.
    -- The rate can move between the order and the cancellation, and reversing
    -- an accrual at a rate that was never applied is a way to mint or destroy
    -- points by cancelling.
    SELECT COALESCE(SUM(delta), 0) INTO :earned
      FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger WHERE demo_id = :visitor AND order_id = :ORDER_ID AND delta > 0;

    UPDATE CHIP_CHAT.ACCOUNTS.orders SET status = 'CANCELLED' WHERE order_id = :ORDER_ID AND demo_id = :visitor;

    IF (:earned > 0) THEN
        SELECT 'loy-' || TO_VARCHAR(CHIP_CHAT.ACCOUNTS.live_ledger_seq.NEXTVAL) INTO :new_entry;
        INSERT INTO CHIP_CHAT.ACCOUNTS.loyalty_ledger (entry_id, demo_id, delta, reason, order_id, reward_name, created_at)
        SELECT :new_entry, :visitor, -:earned, 'ORDER_CANCELLED', :ORDER_ID, NULL, :cancelled_at;
    END IF;

    SELECT COALESCE(SUM(delta), 0) INTO :balance FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger WHERE demo_id = :visitor;

    receipt := OBJECT_CONSTRUCT_KEEP_NULL(
        'ok', TRUE,
        'action', 'CANCEL_ORDER',
        'order_id', :ORDER_ID,
        'status', 'CANCELLED',
        'total', :order_total,
        'points_reversed', :earned,
        'points_balance', :balance,
        'cancelled_at', :cancelled_at,
        'window_minutes', :CANCELLATION_WINDOW_MINUTES,
        'reality', 'Chipotle cannot normally cancel an order once it has been submitted -- it goes straight to the restaurant crew. This demo can, because nothing here is real.',
        'real_delivery_path', 'For a real delivery order Chipotle directs you to its Customer Service team, and says you may incur a cancelation fee. Nothing was charged here and no fee was applied, because nothing here is real.',
        'simulation', 'Simulated cancellation. No order was ever sent to a restaurant, so none was recalled.');

    UPDATE CHIP_CHAT.ACCOUNTS.action_receipts
       SET subject_id = :ORDER_ID, receipt = :receipt
     WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;

    COMMIT;
    RETURN :receipt;

EXCEPTION
    WHEN OTHER THEN
        IF (in_txn) THEN
            ROLLBACK;
        END IF;
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'CANCEL_ORDER', 'rejection', 'WRITE_FAILED',
            'sqlcode', SQLCODE, 'detail', SQLERRM, 'rolled_back', in_txn);
END;
$$;
