-- The write path. Three procedures here, and cancel_order alone in the next
-- file for a reason that file explains.
--
-- RFC-001 §06 fixes four write tools and issue #46 says the ops API writes only
-- through these procedures, never through ad-hoc SQL, so that the invariants
-- live next to the data. A visitor never reaches them: the agent proposes, the
-- app shows a card, the visitor presses Confirm, and the ops API calls one of
-- these with a retry key. That is the second launch gate -- zero account writes
-- executed without explicit confirmation -- and the half of it that lives here
-- is that there is no other way in.
--
-- FIVE PROPERTIES, EACH OF WHICH IS A CHOICE MADE IN THE TEXT BELOW.
--
-- 1. EXECUTE AS CALLER, on every one of them. This is the single most important
--    line in the file. Snowflake's default is owner's rights, and an owner's-
--    rights procedure runs as CHIP_CHAT_ADMIN: GETVARIABLE('DEMO_ID') reads the
--    owner's session rather than the caller's, and #43's row access policies are
--    evaluated against the owner, who is exempt from nothing but is also not the
--    visitor. A write path that ran as its owner would undo RFC-001 §05 from the
--    inside, quietly, while every test that does not open a second session
--    passes. `tests/test_procedure_layout.py` fails if the words go missing.
--
-- 2. NO PROCEDURE TAKES A VISITOR IDENTIFIER. The same absence the tool surface
--    is built on (agent/src/chip_chat/agent/surface.py), carried one tier down.
--    Identity arrives as a session variable set by the connection pool (#44) and
--    nothing in an argument list can contradict it. A procedure that took a
--    demo_id would be a field for a compromised ops API to fill in.
--
-- 3. TRANSACTIONAL. Everything a procedure writes happens between BEGIN
--    TRANSACTION and COMMIT, and every exit path that is not a COMMIT is a
--    ROLLBACK. #46's failure-mode requirement is that nothing is half-written:
--    an order with lines and no accrual, or an accrual with no order, is worse
--    than a rejection, because it is a wrong balance that looks like a right one.
--
-- 4. IDEMPOTENT ON A RETRY KEY. Each procedure claims its key in
--    `action_receipts` with a MERGE as the first statement inside its
--    transaction. A MERGE locks the target table for the rest of the
--    transaction, which is what makes two simultaneous retries serialise; a
--    SELECT-then-INSERT would not, and both would write. A second call with a
--    spent key returns the stored receipt and writes nothing. A call that fails
--    rolls the claim back with everything else, so a genuine failure leaves the
--    key spendable and the visitor can press the button again.
--
-- 5. REJECT, NEVER REPAIR. A rejection is a returned object -- ok false, a code
--    from docs/action-surface.md §7, and a detail naming the thing that failed
--    -- not an exception and not a silently corrected call. The agent does not
--    get to round a draft into validity, and neither does this tier. Every
--    validation happens BEFORE the transaction opens, so a rejection has nothing
--    to roll back; the EXCEPTION handler exists for the failures nobody
--    predicted, and it rolls back and re-raises rather than swallowing.
--
-- WHAT THIS TIER CAN AND CANNOT VALIDATE, stated once because the gap matters.
-- docs/action-surface.md §7.1 lists twelve rules. Rules 1, 2, 3, 4 and 7 and the
-- pricing are here, and they are the ones #46 asks for by name: no SKU in any
-- response that does not exist in the catalogue, enforced at the database rather
-- than at the matcher. Rules 6, 8 and 9 -- required modifier slots, per-pair
-- portion permissions and the six per-item caps -- CANNOT be checked here,
-- because the serving projection of the catalogue does not carry the columns
-- they are about: CHIP_CHAT.CATALOGUE.modifiers is (modifier_id, item_id,
-- modifier_item_id, name, delta_calories), with no group, no min or max, and no
-- portion table beside it. Those rules are enforced at proposal time in
-- `api/src/chip_chat/api/drafts.py` against `chip_chat.catalog`, which does
-- carry them. Rules 11 and 12 -- the draft is confirmed and unexpired, and the
-- session has spend-cap headroom -- are the ops API's by design: a confirmation
-- flag the model can reach is not a confirmation. That division is deliberate
-- and it is not free, and the bead filed against this ticket says what it would
-- take to close it.
--
-- EVERY OBJECT NAME IN EVERY BODY IS FULLY QUALIFIED, and that is not house
-- style. A SQL procedure resolves an unqualified name against the schema the
-- SESSION is in when it is CALLED, not the schema it was created in. So
-- `FROM demo_visitors` works from a session that happens to have run
-- `USE SCHEMA CHIP_CHAT.ACCOUNTS` and fails with "Object 'DEMO_VISITORS' does
-- not exist or not authorized" from one that has not -- which is a write path
-- whose correctness depends on what the caller did before calling it. Verified
-- on this account: calling place_order from a session sitting in CATALOGUE
-- failed exactly that way before the names were qualified.
--
-- CREATE OR REPLACE PROCEDURE is safe in the way CREATE OR REPLACE TABLE is not.
-- A procedure holds no rows and no credential, so replacing one destroys
-- nothing, and re-asserting the body on every apply is the only way an edited
-- procedure reaches the account at all. `test_account_layout.py` bans the word
-- REPLACE next to DATABASE, SCHEMA, WAREHOUSE, USER, ROLE, TABLE and RESOURCE
-- MONITOR, and deliberately not next to PROCEDURE or VIEW.

USE ROLE CHIP_CHAT_ADMIN;
USE SCHEMA CHIP_CHAT.ACCOUNTS;

-- --------------------------------------------------------------------------
-- place_order -- docs/action-surface.md §7.1
--
-- Takes the composition rather than a draft id. The draft store is the ops
-- API's (`api/drafts.py`) and lives in the app tier where the confirmation flag
-- is out of the model's reach; by the time a call arrives here the draft has
-- already been confirmed, and what this procedure re-checks is the half that can
-- go stale between proposal and confirmation -- whether the items still exist,
-- whether this restaurant still has them, and what they cost right now. Issue
-- #46 describes the procedure in exactly those terms: validate every item and
-- modifier against the catalogue, price it, insert orders and order_items,
-- accrue into the ledger, return a receipt.
--
-- THE ORDER IS PRICED AT THE STORE IT IS PLACED AT, AND NOWHERE ELSE. Generated
-- history carries priced_restaurant_id because the harvest priced one restaurant
-- of thirty and eighteen months of orders had to come from somewhere; a live
-- order has no such excuse. An item the visitor's restaurant does not publish a
-- price for is ITEM_UNAVAILABLE_AT_STORE -- "we do not have that here" -- rather
-- than a price borrowed from a restaurant they are not standing in.
--
-- THE EARN RATE IS READ, NOT KNOWN. points_per_dollar comes out of
-- CATALOGUE.rewards_terms, and its absence rejects the whole order. Ten points
-- per dollar is published in three places and this file still refuses to type
-- it: data-gen deliberately took the same number out of its own configuration
-- so that the generated ledger reads it off the harvest, and a serving layer
-- with a literal here would be a second opinion about a published figure that
-- nobody would notice diverging.
-- --------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE place_order(
    RETRY_KEY VARCHAR,
    STORE_ID NUMBER,
    CHANNEL VARCHAR,
    ORDER_LINES VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Place one confirmed order for the visitor bound to this session. Takes no visitor identifier: demo_id comes from the session variable and every row written carries it. Validates every item and every modifier against the published catalogue at this restaurant, prices from item_prices at the column the channel selects, writes orders, order_items and one loyalty_ledger accrual in a single transaction, and returns a receipt. Idempotent on RETRY_KEY: a second call with a spent key replays the stored receipt and writes nothing. Rejects rather than repairs -- ITEM_NOT_ORDERABLE, ITEM_UNAVAILABLE_AT_STORE, QUANTITY_EXCEEDS_MAX, MODIFIER_NOT_OFFERED and the rest of docs/action-surface.md section 7.1.'
EXECUTE AS CALLER
AS
$$
DECLARE
    visitor       VARCHAR;
    known         NUMBER;
    earn_rate     NUMBER;
    daily_cap     NUMBER;
    earned_today  NUMBER;
    capped        BOOLEAN;
    rejection     VARCHAR;
    subject       VARCHAR;
    n_lines       NUMBER;
    priced_lines  VARIANT;
    order_total   NUMBER(10,2);
    priced_at     TIMESTAMP_NTZ;
    new_order     VARCHAR;
    new_entry     VARCHAR;
    placed_at     TIMESTAMP_NTZ;
    points        NUMBER;
    balance       NUMBER;
    claimed       NUMBER;
    in_txn        BOOLEAN DEFAULT FALSE;
    stored        VARIANT;
    spent_on      VARCHAR;
    store_row     VARIANT;
    receipt       VARIANT;
BEGIN
    -- ---- who is asking -------------------------------------------------
    visitor := GETVARIABLE('DEMO_ID');
    IF (visitor IS NULL OR visitor = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'SESSION_NOT_BOUND',
            'detail', 'this session has no DEMO_ID set, so there is no visitor to place an order for');
    END IF;
    IF (RETRY_KEY IS NULL OR RETRY_KEY = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'RETRY_KEY_REQUIRED',
            'detail', 'a write without a retry key cannot be made idempotent, and a retried network call would place a second order');
    END IF;
    SELECT COUNT(*) INTO :known FROM CHIP_CHAT.ACCOUNTS.demo_visitors WHERE demo_id = :visitor;
    IF (:known = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'VISITOR_NOT_FOUND',
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
        IF (:spent_on <> 'PLACE_ORDER') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- ---- the published earn rate, or nothing ---------------------------
    SELECT value INTO :earn_rate
      FROM CHIP_CHAT.CATALOGUE.rewards_terms WHERE rule = 'points_per_dollar';
    IF (:earn_rate IS NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'EARN_RATE_NOT_PUBLISHED',
            'detail', 'CATALOGUE.rewards_terms carries no points_per_dollar row, and an accrual at a rate nobody published is a wrong balance that cannot be undone later');
    END IF;

    -- ---- the shape of the call -----------------------------------------
    IF (CHANNEL IS NULL OR CHANNEL NOT IN ('IN_STORE', 'DELIVERY')) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'CHANNEL_NOT_RECOGNISED',
            'detail', 'channel is IN_STORE or DELIVERY -- the two published price columns, and orders.channel says which one priced the order');
    END IF;
    SELECT COUNT(*) INTO :known FROM CHIP_CHAT.CATALOGUE.stores WHERE store_id = :STORE_ID;
    IF (:known = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'STORE_NOT_FOUND',
            'detail', 'no such restaurant in the harvested locator');
    END IF;
    SELECT COUNT(*) INTO :n_lines FROM TABLE(FLATTEN(input => :ORDER_LINES));
    IF (:n_lines = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'DRAFT_EMPTY',
            'detail', 'an order with no lines');
    END IF;

    -- ---- the catalogue's answer, first failure only ---------------------
    --
    -- One query, five rules, ranked in the order docs/action-surface.md §7.1
    -- applies them, and LIMIT 1 so the visitor is told the first thing wrong
    -- rather than a list they did not ask for. Every rule is a LEFT JOIN that
    -- came back null: an item the catalogue does not carry, a price row this
    -- restaurant does not publish, a modifier that belongs to a different item.
    SELECT reason, subject INTO :rejection, :subject FROM (
        WITH raw AS (
            SELECT f.index                                     AS idx,
                   f.value:item_id::VARCHAR                    AS item_id,
                   COALESCE(f.value:qty::NUMBER, 1)            AS qty,
                   COALESCE(f.value:modifiers, ARRAY_CONSTRUCT()) AS mods
              FROM TABLE(FLATTEN(input => :ORDER_LINES)) f
        ),
        item AS (
            SELECT r.*, m.category, p.unit_price, p.is_available
              FROM raw r
              LEFT JOIN CHIP_CHAT.CATALOGUE.menu_items m ON m.item_id = r.item_id
              LEFT JOIN CHIP_CHAT.CATALOGUE.item_prices p
                     ON p.item_id = r.item_id AND p.restaurant_id = :STORE_ID
        ),
        sel AS (
            SELECT i.idx, i.item_id, s.value::VARCHAR AS modifier_item_id
              FROM item i, LATERAL FLATTEN(input => i.mods) s
        ),
        selv AS (
            SELECT sel.*, md.modifier_id, mp.unit_price AS m_price, mp.is_available AS m_available
              FROM sel
              LEFT JOIN CHIP_CHAT.CATALOGUE.modifiers md
                     ON md.item_id = sel.item_id AND md.modifier_item_id = sel.modifier_item_id
              LEFT JOIN CHIP_CHAT.CATALOGUE.item_prices mp
                     ON mp.item_id = sel.modifier_item_id AND mp.restaurant_id = :STORE_ID
        )
        SELECT 'ITEM_NOT_ORDERABLE' AS reason, item_id AS subject, 1 AS rank
          FROM item WHERE category IS NULL
        UNION ALL
        SELECT 'ITEM_UNAVAILABLE_AT_STORE', item_id, 2
          FROM item WHERE category IS NOT NULL AND (unit_price IS NULL OR is_available = FALSE)
        UNION ALL
        SELECT 'QUANTITY_EXCEEDS_MAX', item_id, 3
          FROM item WHERE qty < 1 OR qty > IFF(category = 'Entree', 1, 5)
        UNION ALL
        SELECT 'MODIFIER_NOT_OFFERED', modifier_item_id, 4
          FROM selv WHERE modifier_id IS NULL
        UNION ALL
        SELECT 'MODIFIER_UNAVAILABLE_AT_STORE', modifier_item_id, 5
          FROM selv WHERE modifier_id IS NOT NULL AND (m_price IS NULL OR m_available = FALSE)
        ORDER BY rank, subject
        LIMIT 1
    );
    IF (:rejection IS NOT NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', :rejection,
            'subject', :subject, 'store_id', :STORE_ID,
            'detail', 'the published catalogue at this restaurant does not support this line, and a draft is rejected rather than repaired');
    END IF;

    -- ---- price it --------------------------------------------------------
    --
    -- Computed once into a variable and used twice: once to write order_items,
    -- once to render the receipt. A receipt re-derived from a second query is a
    -- second chance to disagree with the rows that were written.
    SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                'line_number', line_number, 'item_id', item_id, 'name', item_name,
                'qty', qty, 'modifiers', mod_ids, 'modifier_names', mod_names,
                'unit_price', unit_price, 'line_total', line_total)
            ) WITHIN GROUP (ORDER BY line_number),
           SUM(line_total), MAX(priced_at)
      INTO :priced_lines, :order_total, :priced_at
      FROM (
        WITH raw AS (
            SELECT f.index + 1                                 AS line_number,
                   f.value:item_id::VARCHAR                    AS item_id,
                   COALESCE(f.value:qty::NUMBER, 1)            AS qty,
                   COALESCE(f.value:modifiers, ARRAY_CONSTRUCT()) AS mods
              FROM TABLE(FLATTEN(input => :ORDER_LINES)) f
        ),
        base AS (
            SELECT r.line_number, r.item_id, r.qty, r.mods, m.name AS item_name,
                   IFF(:CHANNEL = 'DELIVERY', p.unit_delivery_price, p.unit_price) AS base_price,
                   p.harvested_at
              FROM raw r
              JOIN CHIP_CHAT.CATALOGUE.menu_items m ON m.item_id = r.item_id
              JOIN CHIP_CHAT.CATALOGUE.item_prices p
                ON p.item_id = r.item_id AND p.restaurant_id = :STORE_ID
        ),
        mods AS (
            -- Comma-joined rather than JOIN ... ON, because a LATERAL FLATTEN
            -- followed by a JOIN binds the ON clause to the flatten alone and
            -- the outer row falls out of scope. Every row here has already
            -- passed the validation above, so an inner join drops nothing.
            SELECT b.line_number,
                   ARRAY_AGG(md.modifier_id) WITHIN GROUP (ORDER BY md.modifier_id) AS mod_ids,
                   ARRAY_AGG(md.name)        WITHIN GROUP (ORDER BY md.modifier_id) AS mod_names,
                   SUM(IFF(:CHANNEL = 'DELIVERY', mp.unit_delivery_price, mp.unit_price)) AS mod_price,
                   MAX(mp.harvested_at) AS mod_harvested_at
              FROM base b,
                   LATERAL FLATTEN(input => b.mods) s,
                   CHIP_CHAT.CATALOGUE.modifiers md,
                   CHIP_CHAT.CATALOGUE.item_prices mp
             WHERE md.item_id = b.item_id
               AND md.modifier_item_id = s.value::VARCHAR
               AND mp.item_id = md.modifier_item_id
               AND mp.restaurant_id = :STORE_ID
             GROUP BY b.line_number
        )
        SELECT b.line_number, b.item_id, b.item_name, b.qty,
               COALESCE(m.mod_ids, ARRAY_CONSTRUCT())   AS mod_ids,
               COALESCE(m.mod_names, ARRAY_CONSTRUCT()) AS mod_names,
               (b.base_price + COALESCE(m.mod_price, 0))::NUMBER(10,2)          AS unit_price,
               (b.qty * (b.base_price + COALESCE(m.mod_price, 0)))::NUMBER(10,2) AS line_total,
               GREATEST(b.harvested_at, COALESCE(m.mod_harvested_at, b.harvested_at)) AS priced_at
          FROM base b LEFT JOIN mods m ON m.line_number = b.line_number
      );

    -- ---- what it earns ---------------------------------------------------
    --
    -- Three qualifying purchases a day is published (rewards terms, ACCUMULATING
    -- POINTS). The real programme does not refuse the fourth purchase, it simply
    -- does not pay points for it, so neither does this: the order places, the
    -- accrual is zero, and the receipt says which of the two happened.
    SELECT value INTO :daily_cap
      FROM CHIP_CHAT.CATALOGUE.rewards_terms WHERE rule = 'daily_qualifying_purchases';
    SELECT COUNT(DISTINCT order_id) INTO :earned_today
      FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger
     WHERE demo_id = :visitor AND reason = 'ORDER' AND delta > 0
       AND created_at::DATE = SYSDATE()::DATE;
    -- Two different reasons to earn nothing, and the receipt has to be able to
    -- tell them apart: the fourth order of a day earns nothing because the
    -- programme says so, and an order that totals zero earns nothing because
    -- ten times nothing is nothing. Deriving "capped" from points being zero
    -- would report the first reason for the second.
    capped := :daily_cap IS NOT NULL AND :earned_today >= :daily_cap;
    points := IFF(:capped, 0, FLOOR(:order_total * :earn_rate));

    -- ---- write it --------------------------------------------------------
    in_txn := TRUE;
    BEGIN TRANSACTION;

    -- The claim. First statement inside the transaction, and a MERGE rather
    -- than a SELECT: it locks action_receipts for the rest of the transaction,
    -- so a simultaneous retry waits here instead of racing past.
    MERGE INTO CHIP_CHAT.ACCOUNTS.action_receipts t
      USING (SELECT :visitor AS demo_id, :RETRY_KEY AS retry_key) s
         ON t.demo_id = s.demo_id AND t.retry_key = s.retry_key
       WHEN NOT MATCHED THEN INSERT (demo_id, retry_key, action, subject_id, receipt, created_at)
            VALUES (s.demo_id, s.retry_key, 'PLACE_ORDER', NULL, TO_VARIANT('CLAIMED'), SYSDATE());
    claimed := SQLROWCOUNT;

    IF (:claimed = 0) THEN
        SELECT receipt, action INTO :stored, :spent_on
          FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;
        COMMIT;
        IF (:spent_on <> 'PLACE_ORDER') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- A sequence is read in a SELECT, never in an assignment: NEXTVAL is a
    -- column expression rather than a scalar function, and it is qualified
    -- because a procedure runs in whatever schema its caller left the session in.
    SELECT 'ord-' || TO_VARCHAR(CHIP_CHAT.ACCOUNTS.live_order_seq.NEXTVAL),
           'loy-' || TO_VARCHAR(CHIP_CHAT.ACCOUNTS.live_ledger_seq.NEXTVAL)
      INTO :new_order, :new_entry;
    placed_at := SYSDATE();

    INSERT INTO CHIP_CHAT.ACCOUNTS.orders (order_id, demo_id, store_id, placed_at, status, total, channel, priced_restaurant_id)
    SELECT :new_order, :visitor, :STORE_ID, :placed_at, 'PENDING', :order_total, :CHANNEL, :STORE_ID;

    INSERT INTO CHIP_CHAT.ACCOUNTS.order_items (order_id, line_number, demo_id, item_id, qty, modifiers, unit_price, line_total)
    SELECT :new_order, f.value:line_number::NUMBER, :visitor, f.value:item_id::VARCHAR,
           f.value:qty::NUMBER, f.value:modifiers, f.value:unit_price::NUMBER(10,2),
           f.value:line_total::NUMBER(10,2)
      FROM TABLE(FLATTEN(input => :priced_lines)) f;

    IF (:points > 0) THEN
        INSERT INTO CHIP_CHAT.ACCOUNTS.loyalty_ledger (entry_id, demo_id, delta, reason, order_id, reward_name, created_at)
        SELECT :new_entry, :visitor, :points, 'ORDER', :new_order, NULL, :placed_at;
    END IF;

    SELECT COALESCE(SUM(delta), 0) INTO :balance FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger WHERE demo_id = :visitor;
    SELECT OBJECT_CONSTRUCT('store_id', store_id, 'name', name, 'city', city, 'region', region)
      INTO :store_row FROM CHIP_CHAT.CATALOGUE.stores WHERE store_id = :STORE_ID;

    receipt := OBJECT_CONSTRUCT_KEEP_NULL(
        'ok', TRUE,
        'action', 'PLACE_ORDER',
        'order_id', :new_order,
        'status', 'PENDING',
        'store', :store_row,
        'channel', :CHANNEL,
        'priced_restaurant_id', :STORE_ID,
        'lines', :priced_lines,
        'total', :order_total,
        'points_earned', :points,
        'points_balance', :balance,
        'earn_rate_points_per_dollar', :earn_rate,
        'daily_earning_cap_reached', :capped,
        'prices_harvested_at', :priced_at,
        'placed_at', :placed_at,
        'simulation', 'Simulated order. Nothing was cooked, charged or sent to a restaurant.');

    UPDATE CHIP_CHAT.ACCOUNTS.action_receipts
       SET subject_id = :new_order, receipt = :receipt
     WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;

    COMMIT;
    RETURN :receipt;

EXCEPTION
    WHEN OTHER THEN
        -- Nothing predicted lands here: every rejection above returns before a
        -- transaction is open. What reaches this is a failure nobody wrote a
        -- rule for -- a revoked privilege, a table that moved -- and the answer
        -- is to undo the half-written order and say so loudly rather than to
        -- return something that reads like a receipt.
        IF (in_txn) THEN
            ROLLBACK;
        END IF;
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'PLACE_ORDER', 'rejection', 'WRITE_FAILED',
            'sqlcode', SQLCODE, 'detail', SQLERRM,
            'rolled_back', in_txn);
END;
$$;

-- --------------------------------------------------------------------------
-- redeem_points -- docs/action-surface.md §7.3
--
-- The write with no undo, and the reason its confirmation card does more work
-- than place_order's. The rewards terms say redeemed points are gone: no
-- refunds, no returns, no exchanges, even if the reward itself is returned.
-- There is no un-redeem tool because there is no un-redeem.
--
-- THREE PUBLISHED RULES SHAPE THE VALIDATION, and all three are about the
-- moment of redeeming rather than the moment of looking:
--
--   * Availability is checked at redemption, not at display. A reward that has
--     left the published catalogue since the visitor last looked is
--     REWARD_UNAVAILABLE.
--   * The cost is re-read here, never taken from the card. Chipotle reserves
--     the right to change what a reward costs at any time, so QUOTED_POINT_COST
--     is what the visitor was SHOWN and a mismatch is REWARD_COST_CHANGED --
--     re-proposed rather than silently charged the new price.
--   * The balance is the sum of the ledger, never a stored number. That is what
--     makes the ledger the system of record rather than a log beside one.
--
-- THE MINT IS THE LEDGER ROW. §7.3 describes two writes -- a negative ledger
-- entry and a Reward minted onto the account with a sixty-day life. There is one
-- write here, deliberately: the negative entry, carrying the published reward
-- name, IS the mint, and the expiry is that row's created_at plus the published
-- window. A second table would be a second system of record for one fact, and
-- the two would eventually disagree. Nothing in this demo consumes a minted
-- reward -- applying one to food is a checkout act the action surface puts out
-- of scope (§2.2) -- so there is no state for a second table to hold.
--
-- AND THE EXPIRY IS READ RATHER THAN ASSUMED. reward_expiry_days comes out of
-- CATALOGUE.rewards_terms. If it is not loaded the receipt carries a null expiry
-- and says NOT_PUBLISHED, in the same three-valued spirit as
-- menu_items.allergen_disclosure: a date computed from a constant this file
-- chose would be a promise to the visitor that nobody published.
-- --------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE redeem_points(
    RETRY_KEY VARCHAR,
    REWARD_ID VARCHAR,
    QUOTED_POINT_COST NUMBER
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Redeem the bound visitor''s points for one published reward. Takes no visitor identifier: the balance is the sum of that visitor''s loyalty_ledger, which is what a row access policy scopes. Validates the reward is still in the published catalogue, that its cost has not moved since the visitor was shown it (QUOTED_POINT_COST, or null to skip the check), and that the balance covers it; then appends one negative ledger entry naming the reward and returns the receipt, the new balance and the reward''s expiry. Idempotent on RETRY_KEY. Rejections: REWARD_UNAVAILABLE, REWARD_COST_CHANGED, INSUFFICIENT_POINTS. There is no un-redeem: the terms say redeemed points are gone.'
EXECUTE AS CALLER
AS
$$
DECLARE
    visitor      VARCHAR;
    known        NUMBER;
    reward_name  VARCHAR;
    point_cost   NUMBER;
    expiry_days  NUMBER;
    balance      NUMBER;
    new_entry    VARCHAR;
    redeemed_at  TIMESTAMP_NTZ;
    claimed      NUMBER;
    in_txn       BOOLEAN DEFAULT FALSE;
    stored       VARIANT;
    spent_on     VARCHAR;
    receipt      VARIANT;
BEGIN
    visitor := GETVARIABLE('DEMO_ID');
    IF (visitor IS NULL OR visitor = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'SESSION_NOT_BOUND',
            'detail', 'this session has no DEMO_ID set, so there is no balance to spend');
    END IF;
    IF (RETRY_KEY IS NULL OR RETRY_KEY = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'RETRY_KEY_REQUIRED',
            'detail', 'a redemption cannot be undone, so a retried call that redeemed twice could not be repaired');
    END IF;
    SELECT COUNT(*) INTO :known FROM CHIP_CHAT.ACCOUNTS.demo_visitors WHERE demo_id = :visitor;
    IF (:known = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'VISITOR_NOT_FOUND',
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
        IF (:spent_on <> 'REDEEM_POINTS') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- The catalogue, now rather than when the card was rendered.
    SELECT name, point_cost INTO :reward_name, :point_cost
      FROM CHIP_CHAT.CATALOGUE.rewards WHERE reward_id = :REWARD_ID;
    IF (:reward_name IS NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'REWARD_UNAVAILABLE',
            'subject', :REWARD_ID,
            'detail', 'no such reward in the published catalogue. The terms make availability a question asked at redemption, not at display');
    END IF;
    IF (QUOTED_POINT_COST IS NOT NULL AND QUOTED_POINT_COST <> :point_cost) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'REWARD_COST_CHANGED',
            'subject', :REWARD_ID, 'quoted', QUOTED_POINT_COST, 'point_cost', :point_cost,
            'detail', 'the published cost moved since the visitor was shown it. Re-propose rather than charge a price nobody agreed to');
    END IF;

    SELECT COALESCE(SUM(delta), 0) INTO :balance FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger WHERE demo_id = :visitor;
    IF (:balance < :point_cost) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'INSUFFICIENT_POINTS',
            'subject', :REWARD_ID, 'point_cost', :point_cost, 'points_balance', :balance,
            'points_short', :point_cost - :balance,
            'detail', 'the balance does not cover the reward, and how far short it is is worth saying out loud');
    END IF;

    SELECT value INTO :expiry_days
      FROM CHIP_CHAT.CATALOGUE.rewards_terms WHERE rule = 'reward_expiry_days';

    in_txn := TRUE;
    BEGIN TRANSACTION;

    MERGE INTO CHIP_CHAT.ACCOUNTS.action_receipts t
      USING (SELECT :visitor AS demo_id, :RETRY_KEY AS retry_key) s
         ON t.demo_id = s.demo_id AND t.retry_key = s.retry_key
       WHEN NOT MATCHED THEN INSERT (demo_id, retry_key, action, subject_id, receipt, created_at)
            VALUES (s.demo_id, s.retry_key, 'REDEEM_POINTS', NULL, TO_VARIANT('CLAIMED'), SYSDATE());
    claimed := SQLROWCOUNT;

    IF (:claimed = 0) THEN
        SELECT receipt, action INTO :stored, :spent_on
          FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;
        COMMIT;
        IF (:spent_on <> 'REDEEM_POINTS') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    SELECT 'loy-' || TO_VARCHAR(CHIP_CHAT.ACCOUNTS.live_ledger_seq.NEXTVAL) INTO :new_entry;
    redeemed_at := SYSDATE();

    INSERT INTO CHIP_CHAT.ACCOUNTS.loyalty_ledger (entry_id, demo_id, delta, reason, order_id, reward_name, created_at)
    SELECT :new_entry, :visitor, -:point_cost, 'REWARD_REDEEMED', NULL, :reward_name, :redeemed_at;

    SELECT COALESCE(SUM(delta), 0) INTO :balance FROM CHIP_CHAT.ACCOUNTS.loyalty_ledger WHERE demo_id = :visitor;

    receipt := OBJECT_CONSTRUCT_KEEP_NULL(
        'ok', TRUE,
        'action', 'REDEEM_POINTS',
        'entry_id', :new_entry,
        'reward_id', :REWARD_ID,
        'reward_name', :reward_name,
        'points_deducted', :point_cost,
        'points_balance', :balance,
        'redeemed_at', :redeemed_at,
        'reward_expires_at', IFF(:expiry_days IS NULL, NULL, DATEADD('day', :expiry_days, :redeemed_at)),
        'reward_expiry_disclosure', IFF(:expiry_days IS NULL, 'NOT_PUBLISHED', 'PUBLISHED'),
        'finality', 'Redeeming cannot be undone. The reward goes onto the account, and only one reward can be used per order.',
        'simulation', 'Simulated redemption. Nothing was cooked, charged or sent to a restaurant.');

    UPDATE CHIP_CHAT.ACCOUNTS.action_receipts
       SET subject_id = :REWARD_ID, receipt = :receipt
     WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;

    COMMIT;
    RETURN :receipt;

EXCEPTION
    WHEN OTHER THEN
        IF (in_txn) THEN
            ROLLBACK;
        END IF;
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'REDEEM_POINTS', 'rejection', 'WRITE_FAILED',
            'sqlcode', SQLCODE, 'detail', SQLERRM, 'rolled_back', in_txn);
END;
$$;

-- --------------------------------------------------------------------------
-- update_preferences -- docs/action-surface.md §7.4
--
-- Three fields, and they are the three docs/decisions/persona-editing.md fixes:
-- display_name, home_store_override and stated_preferences, all on
-- demo_visitors and on no other table. That placement is the mechanism behind
-- PRD Q2 rather than a rule anybody has to remember -- every gold mart is
-- computed from orders, order_items and loyalty_ledger, so there is no edit a
-- visitor can make that a mart was computed against, and therefore no edit that
-- can make one stale.
--
-- ABSENT MEANS UNCHANGED; AN EXPLICIT NULL CLEARS. Those are different calls and
-- a VARIANT is what can tell them apart: PREFS:display_name reads as null in
-- both cases, so the presence test is OBJECT_KEYS. A partial update that could
-- not distinguish them would silently clear whatever the caller left out.
--
-- A CLOSED VOCABULARY, NOT FREE TEXT, and that is the product's own argument
-- rather than ours. Asked whether special instructions can be added to an app
-- order, Chipotle answers "Unfortunately, there isn't" -- comment boxes caused
-- confusion, and the customer is pointed at modifying ingredients instead. So a
-- stated preference must NAME A MODIFIER the grammar can act on: a visitor may
-- say no dairy, because dairy names modifiers; they may not ask the crew to be
-- generous with the rice.
--
-- WHAT THIS TIER CANNOT CHECK. §7.4 rule 4 also requires a portion stance to be
-- one the published portion_options permit for that modifier on at least one
-- item -- light guacamole is rejected because no item offers it. The serving
-- projection carries no portion_options table, so what is checked here is that
-- the stance is one of the five published words; the per-pair permission is the
-- ops API's, against chip_chat.catalog. Same gap as place_order's rules 6, 8
-- and 9, same bead.
--
-- A STATED PREFERENCE IS NOT AN ALLERGY, and the acknowledgement says so in
-- words rather than leaving it to be inferred. PRD K3 requires unconditional
-- honesty on allergens; a no-dairy preference filters a candidate set, does not
-- consult item_allergens, and is not a safety guarantee.
-- --------------------------------------------------------------------------

CREATE OR REPLACE PROCEDURE update_preferences(
    RETRY_KEY VARCHAR,
    PREFS VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT = 'Update the bound visitor''s three editable fields -- display_name, home_store, stated_preferences -- and nothing else. Takes no visitor identifier. A key absent from PREFS is left unchanged and an explicit null clears it, which is why the argument is a VARIANT rather than three columns. stated_preferences is a closed vocabulary over published modifier ids with one of five stances, never free text: the real product refuses free-text instructions and points customers at modifying ingredients instead. Idempotent on RETRY_KEY. Rejections: NAME_TOO_LONG, STORE_NOT_FOUND, MODIFIER_NOT_RECOGNISED, STANCE_NOT_AVAILABLE_FOR_MODIFIER, TOO_MANY_PREFERENCES. The acknowledgement states that a preference is not an allergen answer.'
EXECUTE AS CALLER
AS
$$
DECLARE
    visitor      VARCHAR;
    known        NUMBER;
    set_name     BOOLEAN;
    set_store    BOOLEAN;
    set_prefs    BOOLEAN;
    clear_name   BOOLEAN;
    clear_prefs  BOOLEAN;
    new_name     VARCHAR;
    new_store    NUMBER;
    prefs_array  VARIANT;
    n_prefs      NUMBER;
    rejection    VARCHAR;
    subject      VARCHAR;
    claimed      NUMBER;
    in_txn       BOOLEAN DEFAULT FALSE;
    stored       VARIANT;
    spent_on     VARCHAR;
    updated_at   TIMESTAMP_NTZ;
    receipt      VARIANT;
BEGIN
    visitor := GETVARIABLE('DEMO_ID');
    IF (visitor IS NULL OR visitor = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'SESSION_NOT_BOUND',
            'detail', 'this session has no DEMO_ID set, so there is no visitor to update');
    END IF;
    IF (RETRY_KEY IS NULL OR RETRY_KEY = '') THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'RETRY_KEY_REQUIRED',
            'detail', 'every write action takes one, so that the four are retried the same way');
    END IF;
    SELECT COUNT(*) INTO :known FROM CHIP_CHAT.ACCOUNTS.demo_visitors WHERE demo_id = :visitor;
    IF (:known = 0) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'VISITOR_NOT_FOUND',
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
        IF (:spent_on <> 'UPDATE_PREFERENCES') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- Present, versus present and null. OBJECT_KEYS is the only thing that
    -- separates "leave it alone" from "clear it".
    SELECT ARRAY_CONTAINS('display_name'::VARIANT, OBJECT_KEYS(:PREFS)),
           ARRAY_CONTAINS('home_store'::VARIANT, OBJECT_KEYS(:PREFS)),
           ARRAY_CONTAINS('stated_preferences'::VARIANT, OBJECT_KEYS(:PREFS))
      INTO :set_name, :set_store, :set_prefs;
    IF (NOT :set_name AND NOT :set_store AND NOT :set_prefs) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'NOTHING_TO_UPDATE',
            'detail', 'prefs named none of display_name, home_store or stated_preferences, which are the only three fields a visitor may change');
    END IF;

    new_name := TRIM(PREFS:display_name::VARCHAR);
    IF (:set_name AND PREFS:display_name IS NOT NULL
        AND (LENGTH(:new_name) = 0 OR LENGTH(:new_name) > 40)) THEN
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'NAME_TOO_LONG',
            'length', LENGTH(:new_name),
            'detail', 'a display name is 1 to 40 characters after trimming. INVENTED: the ceiling is an ordinary product limit nobody publishes, docs/action-surface.md section 10 row 5');
    END IF;

    new_store := PREFS:home_store::NUMBER;
    IF (:set_store AND :new_store IS NOT NULL) THEN
        SELECT COUNT(*) INTO :known FROM CHIP_CHAT.CATALOGUE.stores WHERE store_id = :new_store;
        IF (:known = 0) THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'STORE_NOT_FOUND',
                'subject', :new_store,
                'detail', 'no such restaurant in the harvested locator. This changes where the NEXT order is priced and nothing about past orders');
        END IF;
    END IF;

    prefs_array := PREFS:stated_preferences;
    IF (:set_prefs AND :prefs_array IS NOT NULL) THEN
        SELECT COUNT(*) INTO :n_prefs FROM TABLE(FLATTEN(input => :prefs_array));
        IF (:n_prefs > 20) THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'TOO_MANY_PREFERENCES',
                'count', :n_prefs,
                'detail', 'at most 20 entries. INVENTED: the ceiling, docs/action-surface.md section 10 row 5');
        END IF;
        SELECT reason, subject INTO :rejection, :subject FROM (
            WITH stated AS (
                SELECT f.value:modifier_item_id::VARCHAR AS modifier_item_id,
                       f.value:stance::VARCHAR           AS stance
                  FROM TABLE(FLATTEN(input => :prefs_array)) f
            )
            SELECT 'MODIFIER_NOT_RECOGNISED' AS reason,
                   COALESCE(s.modifier_item_id, 'null') AS subject, 1 AS rank
              FROM stated s
             WHERE s.modifier_item_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM CHIP_CHAT.CATALOGUE.modifiers m
                                WHERE m.modifier_item_id = s.modifier_item_id)
            UNION ALL
            SELECT 'STANCE_NOT_AVAILABLE_FOR_MODIFIER', COALESCE(s.stance, 'null'), 2
              FROM stated s
             WHERE s.stance IS NULL
                OR s.stance NOT IN ('always', 'never', 'light', 'extra', 'side')
            ORDER BY rank, subject
            LIMIT 1
        );
        IF (:rejection IS NOT NULL) THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', :rejection,
                'subject', :subject,
                'detail', 'a preference names a published modifier and one of the five published stances, or it is not stored. A preference that cannot name a modifier is one nothing can act on');
        END IF;
    END IF;

    in_txn := TRUE;
    BEGIN TRANSACTION;

    MERGE INTO CHIP_CHAT.ACCOUNTS.action_receipts t
      USING (SELECT :visitor AS demo_id, :RETRY_KEY AS retry_key) s
         ON t.demo_id = s.demo_id AND t.retry_key = s.retry_key
       WHEN NOT MATCHED THEN INSERT (demo_id, retry_key, action, subject_id, receipt, created_at)
            VALUES (s.demo_id, s.retry_key, 'UPDATE_PREFERENCES', NULL, TO_VARIANT('CLAIMED'), SYSDATE());
    claimed := SQLROWCOUNT;

    IF (:claimed = 0) THEN
        SELECT receipt, action INTO :stored, :spent_on
          FROM CHIP_CHAT.ACCOUNTS.action_receipts WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;
        COMMIT;
        IF (:spent_on <> 'UPDATE_PREFERENCES') THEN
            RETURN OBJECT_CONSTRUCT(
                'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'RETRY_KEY_SPENT_ON_ANOTHER_ACTION',
                'detail', 'this retry key was already spent by ' || :spent_on);
        END IF;
        RETURN OBJECT_INSERT(:stored, 'replayed', TRUE, TRUE);
    END IF;

    -- Whether each field is being cleared is decided in scripting expressions,
    -- where PREFS:key reads, and passed into the UPDATE as booleans: inside a
    -- SQL statement an argument has to be bound as :PREFS, and :PREFS:key is
    -- not something the parser can make sense of.
    clear_name := :set_name AND PREFS:display_name IS NULL;
    clear_prefs := :set_prefs AND PREFS:stated_preferences IS NULL;
    updated_at := SYSDATE();
    UPDATE CHIP_CHAT.ACCOUNTS.demo_visitors
       SET display_name        = IFF(:set_name,  IFF(:clear_name, NULL, :new_name), display_name),
           home_store_override = IFF(:set_store, :new_store, home_store_override),
           stated_preferences  = IFF(:set_prefs, IFF(:clear_prefs, NULL, TO_JSON(:prefs_array)), stated_preferences),
           last_seen           = :updated_at
     WHERE demo_id = :visitor;

    SELECT OBJECT_CONSTRUCT_KEEP_NULL(
               'ok', TRUE,
               'action', 'UPDATE_PREFERENCES',
               'display_name', display_name,
               'home_store', home_store_override,
               'stated_preferences', TRY_PARSE_JSON(stated_preferences),
               'updated_at', :updated_at,
               'fields_changed', ARRAY_COMPACT(ARRAY_CONSTRUCT(
                   IFF(:set_name, 'display_name', NULL),
                   IFF(:set_store, 'home_store', NULL),
                   IFF(:set_prefs, 'stated_preferences', NULL))),
               'not_an_allergen_answer',
                   'A stated preference filters what gets proposed. It is not an allergen check, and an allergen question is answered from the published chart with a citation.',
               'applies_to', 'The next order this visitor proposes. Past orders and the marts computed from them are unchanged.',
               'simulation', 'Simulated account. This is a demo persona rather than a Chipotle account, and nothing here reaches one.')
      INTO :receipt
      FROM CHIP_CHAT.ACCOUNTS.demo_visitors WHERE demo_id = :visitor;

    UPDATE CHIP_CHAT.ACCOUNTS.action_receipts
       SET subject_id = NULL, receipt = :receipt
     WHERE demo_id = :visitor AND retry_key = :RETRY_KEY;

    COMMIT;
    RETURN :receipt;

EXCEPTION
    WHEN OTHER THEN
        IF (in_txn) THEN
            ROLLBACK;
        END IF;
        RETURN OBJECT_CONSTRUCT(
            'ok', FALSE, 'action', 'UPDATE_PREFERENCES', 'rejection', 'WRITE_FAILED',
            'sqlcode', SQLCODE, 'detail', SQLERRM, 'rolled_back', in_txn);
END;
$$;
