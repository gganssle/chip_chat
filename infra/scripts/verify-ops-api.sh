#!/usr/bin/env bash
#
# Put issue #63's acceptance criteria to the DEPLOYED ops API.
#
# Three of that ticket's five criteria are claims about a running system rather
# than about this repository's code, and `api/tests/test_ops_routes.py` cannot
# reach them: it drives the four route functions in-process with a recording
# backend where Snowflake would be. That suite establishes that the code refuses;
# this script establishes that the thing on the internet refuses -- with the real
# Key Vault references resolved, the real write role authenticated, the real
# catalogue loaded, and the request arriving over HTTPS from outside.
#
#   "An unconfirmed draft_id is rejected -- tested directly against the API,
#    bypassing the UI."
#
# That sentence is the reason this file exists. There is no UI in it anywhere.
#
# WHAT IS PROVED, AND WHAT IS NOT. Read this before quoting a green run.
#
# The Functions host is a *different process* from the chat app, and the draft
# store is in memory (#62). So nothing outside the ops API's own worker can mint
# a draft or set its confirmed flag -- which is the whole design: the flag lives
# where no tool argument and no model output can reach it. The consequence for
# this script is precise and worth stating rather than glossing:
#
#   * Every write this script attempts is refused, and refused BEFORE a
#     Snowflake session is acquired. That is the gate, and it is observable from
#     outside: the answer carries a rejection code, and `action_receipts` --
#     the table every procedure MERGEs into, and the whole of the idempotency
#     mechanism -- does not grow.
#   * It cannot observe a SUCCESSFUL write, so it cannot observe a successful
#     write happening exactly once. The retry-key criterion is therefore
#     reported UNSCORED here rather than passed, and `api/tests/test_ops.py`'s
#     `commit_then_fail()` remains the only place that criterion is met. A
#     script that printed "one write" having caused zero writes would be the
#     most expensive kind of green.
#
# That distinction is the same one `chip_chat.eval.adversarial` draws between
# `held` and `unscored`, and for the same reason.
#
# COST. One warehouse resume at most, and usually not even that: every probe
# here is refused before a connection is opened. The Snowflake queries are two
# counts.
#
# Safe to run against production, and meant to be. It writes nothing.

set -euo pipefail

APP=""
GROUP="rg-chip-chat"
URL=""
VAULT=""
OPS_KEY_SECRET="${OPS_KEY_SECRET:-ops-api-key}"
SNOW_CONNECTION="${SNOW_CONNECTION:-chipchat}"
SPAN_WAIT_SECONDS="${SPAN_WAIT_SECONDS:-300}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --vault) VAULT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$APP" || -z "$URL" || -z "$VAULT" ]]; then
  echo "usage: $0 --app <function app> --group <rg> --url <https://...> --vault <kv name>" >&2
  echo "  'make ops-verify' fills all four in from terraform output." >&2
  exit 2
fi

# --- The three things a caller has to present -------------------------------
#
# Which is itself the first thing this script establishes: it is only able to
# reach the write path because it holds all three, and every probe below that
# omits one gets a refusal naming which.
#
# The platform's function key is on top of the ops key rather than instead of
# it. `func.AuthLevel.FUNCTION` is Azure's door; `CHIP_CHAT_OPS_KEY` is the
# application's, and the second is the one that would still be there if somebody
# turned the first off.

HOST_KEY=$(az functionapp keys list -g "$GROUP" -n "$APP" \
  --query functionKeys.default -o tsv)
OPS_KEY=$(az keyvault secret show --vault-name "$VAULT" \
  --name "$OPS_KEY_SECRET" --query value -o tsv)

if [[ -z "$HOST_KEY" || -z "$OPS_KEY" ]]; then
  echo "could not read the function key or the ops key; nothing was attempted" >&2
  exit 1
fi

# Two visitors. They are the cross-session probe: the second presents the first
# one's draft id, and the identifiers are of the shape data-gen mints
# (`dm-000123`) because `function_app.py::_DEMO_ID` allow-lists the spelling
# before a connection is opened.
VISITOR_A="dm-999001"
VISITOR_B="dm-999002"

# W3C trace context, composed here rather than borrowed, because the ops API
# refuses a write it could not be found in a trace for -- and this run wants to
# be findable afterwards. The trace id is fresh per run so that the span query
# at the end reads only this run's spans.
TRACE_ID=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
SPAN_ID=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
TRACEPARENT="00-${TRACE_ID}-${SPAN_ID}-01"
# `session.id` and `chip_chat.turn.index` are what `continue_turn` reads back
# out of baggage; without the first it raises rather than opening an
# unattributable half-trace.
BAGGAGE="session.id=verify-${TRACE_ID:0:8},chip_chat.turn.index=0"

echo "# Issue #63's live acceptance criteria, against the deployment"
echo
echo "Target:  $URL"
echo "App:     $APP"
echo "Trace:   $TRACE_ID"
echo

# --- What Snowflake held before anything was attempted ----------------------
#
# `action_receipts` is the table every one of #46's procedures MERGEs into,
# keyed on the retry key, and it is therefore the exact measure of "a write
# happened". `orders` is the one `place_order` inserts into. Counting both
# before and after is how "the gate refused" is distinguished from "the gate
# refused and something else wrote anyway".

counts() {
  snow sql -c "$SNOW_CONNECTION" --format json -q \
    "SELECT (SELECT COUNT(*) FROM CHIP_CHAT.ACCOUNTS.action_receipts) AS receipts,
            (SELECT COUNT(*) FROM CHIP_CHAT.ACCOUNTS.orders) AS orders" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)[0]; print(r["RECEIPTS"], r["ORDERS"])'
}

read -r BEFORE_RECEIPTS BEFORE_ORDERS < <(counts)
echo "Before:  action_receipts=$BEFORE_RECEIPTS orders=$BEFORE_ORDERS"
echo

# --- The probes -------------------------------------------------------------

FAILURES=0
UNSCORED=0

# One call. Every argument is explicit so that a probe which omits a header is
# obviously omitting it rather than relying on a default.
call() {
  local route="$1" body="$2" visitor="$3" trace="$4" ops_key="$5"
  local args=(-s -o /tmp/ops-verify-body.json -w '%{http_code}'
    -X POST "$URL/api/$route"
    -H "x-functions-key: $HOST_KEY"
    -H 'Content-Type: application/json'
    --data "$body")
  [[ -n "$ops_key" ]] && args+=(-H "x-cilantro-ops-key: $ops_key")
  [[ -n "$visitor" ]] && args+=(-H "x-cilantro-session: $visitor")
  [[ -n "$trace" ]] && args+=(-H "traceparent: $trace" -H "baggage: $BAGGAGE")
  curl "${args[@]}"
}

field() {
  python3 -c 'import json,sys
try:
    print(json.load(open("/tmp/ops-verify-body.json")).get(sys.argv[1], ""))
except Exception:
    print("")' "$1"
}

# `probe <id> <what it establishes> <expected status> <expected error/rejection>`
# after a `call`. Prints one line either way, because a run where everything
# holds should still show what each probe actually got back -- "held" is a
# verdict and the answer is the thing somebody will want to argue with.
probe() {
  local id="$1" claim="$2" want_status="$3" want_code="$4" got_status="$5"
  local got_code
  got_code=$(field error)
  [[ -z "$got_code" ]] && got_code=$(field rejection)
  if [[ "$got_status" == "$want_status" && "$got_code" == "$want_code" ]]; then
    printf 'PASS  %-34s %s -> %s %s\n' "$id" "$claim" "$got_status" "$got_code"
  else
    printf 'FAIL  %-34s %s -> %s %s (wanted %s %s)\n' \
      "$id" "$claim" "$got_status" "$got_code" "$want_status" "$want_code"
    FAILURES=$((FAILURES + 1))
  fi
}

# 1. The edge's own three preconditions, in the order the code checks them. Not
#    among #63's criteria, but the reason the criteria below mean anything: a
#    caller who cannot get past these never reaches the confirmation gate, and a
#    deployment that had quietly lost `CHIP_CHAT_OPS_KEY` would let everything
#    through while every probe below still printed a refusal.
STATUS=$(call place_order '{"draft_id":"draft-anything"}' "$VISITOR_A" "$TRACEPARENT" "")
probe "no-ops-key" "an unauthenticated caller may not write" 401 OPS_KEY_INVALID "$STATUS"

STATUS=$(call place_order '{"draft_id":"draft-anything"}' "" "$TRACEPARENT" "$OPS_KEY")
probe "no-visitor" "a write with nobody bound is refused" 401 SESSION_REQUIRED "$STATUS"

STATUS=$(call place_order '{"draft_id":"draft-anything"}' "$VISITOR_A" "" "$OPS_KEY")
probe "no-trace-context" "a write nobody could find in a trace is refused" \
  400 TRACE_CONTEXT_REQUIRED "$STATUS"

# 2. CRITERION: an unconfirmed draft_id is rejected, called directly, bypassing
#    the UI. This is the launch gate, and this is the shape #83 could not put at
#    the door until there was a door. `DRAFT_NOT_FOUND` rather than
#    `DRAFT_NOT_CONFIRMED` is the correct and slightly disappointing answer from
#    outside the process: this caller cannot mint a draft in the host's memory,
#    so the id it presents is one the store never held. Both codes are the same
#    refusal from `DraftStore.claim` and both end the call before a Snowflake
#    session is acquired, which is the property under test.
STATUS=$(call place_order '{"draft_id":"draft-0000000000000000"}' \
  "$VISITOR_A" "$TRACEPARENT" "$OPS_KEY")
probe "unconfirmed-draft" "an unconfirmed draft is refused at the door" \
  200 DRAFT_NOT_FOUND "$STATUS"
FIRST_ANSWER=$(cat /tmp/ops-verify-body.json)

# 3. CRITERION: a confirmed draft from another session is rejected. The same
#    draft id, a different visitor on `x-cilantro-session`. The assertion worth
#    making is not only that it is refused but that it is refused IDENTICALLY --
#    an app that distinguished "somebody else has this" from "nobody has this"
#    would be an oracle for other visitors' draft ids, which is the control
#    `chip_chat.eval.adversarial.writegate.FORGED_DRAFT_ID` exists to be.
STATUS=$(call place_order '{"draft_id":"draft-0000000000000000"}' \
  "$VISITOR_B" "$TRACEPARENT" "$OPS_KEY")
probe "another-session" "another visitor's draft id is refused" \
  200 DRAFT_NOT_FOUND "$STATUS"
if [[ "$(cat /tmp/ops-verify-body.json)" == "$FIRST_ANSWER" ]]; then
  printf 'PASS  %-34s %s\n' "no-oracle" \
    "the stranger and the owner get byte-identical answers"
else
  printf 'FAIL  %-34s %s\n' "no-oracle" \
    "the answers differ, so the API tells a stranger which draft ids exist"
  FAILURES=$((FAILURES + 1))
fi

# 4. The other three routes, because "the ops API rejects an unconfirmed write"
#    is a claim about all four and three of them claim from a different ledger
#    (`ConfirmationLedger`, not `DraftStore`). A deployment where `place_order`
#    was gated and `redeem_points` was not would pass every probe above.
#    `CONFIRMATION_NOT_FOUND` rather than `CONFIRMATION_NOT_CONFIRMED` for the
#    same reason `place_order` answers `DRAFT_NOT_FOUND`: no card was ever
#    offered to this visitor from outside the host's process, and a reference
#    that was never offered and one belonging to somebody else get the same
#    answer on purpose.
STATUS=$(call cancel_order '{"order_id":"ord-000000"}' "$VISITOR_A" "$TRACEPARENT" "$OPS_KEY")
probe "cancel-unconfirmed" "an unconfirmed cancellation is refused" \
  200 CONFIRMATION_NOT_FOUND "$STATUS"
STATUS=$(call redeem_points '{"reward_id":"rw-000000"}' "$VISITOR_A" "$TRACEPARENT" "$OPS_KEY")
probe "redeem-unconfirmed" "an unconfirmed redemption is refused" \
  200 CONFIRMATION_NOT_FOUND "$STATUS"
STATUS=$(call update_preferences '{"prefs":{"spice":"mild"}}' \
  "$VISITOR_A" "$TRACEPARENT" "$OPS_KEY")
probe "prefs-unconfirmed" "an unconfirmed preference edit is refused" \
  200 CONFIRMATION_NOT_FOUND "$STATUS"

# 5. CRITERION, UNSCORED: retrying with the same idempotency key produces one
#    write. See the header. Nothing outside the host's own process can confirm a
#    draft, so this script cannot cause the one write it would then have to
#    count. What it CAN do is put the retry twice and establish that the number
#    of writes is the same both times, which here is zero -- a weaker fact,
#    stated as the weaker fact it is.
call place_order '{"draft_id":"draft-0000000000000000"}' \
  "$VISITOR_A" "$TRACEPARENT" "$OPS_KEY" >/dev/null
printf 'UNSCORED  %-30s %s\n' "retry-key-writes-once" \
  "no confirmed record can be created from outside the host's process (#62)"
UNSCORED=$((UNSCORED + 1))

echo

# --- Nothing was written ----------------------------------------------------

read -r AFTER_RECEIPTS AFTER_ORDERS < <(counts)
echo "After:   action_receipts=$AFTER_RECEIPTS orders=$AFTER_ORDERS"
if [[ "$AFTER_RECEIPTS" == "$BEFORE_RECEIPTS" && "$AFTER_ORDERS" == "$BEFORE_ORDERS" ]]; then
  printf 'PASS  %-34s %s\n' "nothing-was-written" \
    "every refusal ended before a procedure was called"
else
  printf 'FAIL  %-34s %s\n' "nothing-was-written" \
    "a refused call still wrote to CHIP_CHAT.ACCOUNTS"
  FAILURES=$((FAILURES + 1))
fi
echo

# --- CRITERION: every write emits ops.<action> with its confirmation state ---
#
# Read off Application Insights rather than asserted in the host, because the
# criterion is about what an auditor can find later. `ops_write` opens the span
# BEFORE the record is claimed, so a rejected write emits one too -- which is
# the case that matters: a gate is auditable when its refusals are in the trace,
# not only its successes.
#
# Ingestion is not instant. The wait is bounded and a timeout is reported as not
# measured rather than as a failure, because "the span had not arrived after
# five minutes" and "the span was never emitted" are different findings and only
# one of them is this deployment's fault.

APPI=$(az monitor app-insights component show -g "$GROUP" \
  --query "[?contains(name, 'chip-chat')].name | [0]" -o tsv 2>/dev/null || true)
if [[ -z "$APPI" ]]; then
  printf 'UNSCORED  %-30s %s\n' "ops-span-emitted" \
    "no Application Insights component found in $GROUP"
  UNSCORED=$((UNSCORED + 1))
else
  QUERY="dependencies
    | where timestamp > ago(1h)
    | where operation_Id == '$TRACE_ID'
    | where name startswith 'ops.'
    | project name, tostring(customDimensions)"
  FOUND=""
  DEADLINE=$((SECONDS + SPAN_WAIT_SECONDS))
  while [[ $SECONDS -lt $DEADLINE ]]; do
    FOUND=$(az monitor app-insights query --app "$APPI" -g "$GROUP" \
      --analytics-query "$QUERY" --offset 1h -o json 2>/dev/null \
      | python3 -c 'import json,sys
try:
    rows = json.load(sys.stdin)["tables"][0]["rows"]
except Exception:
    rows = []
print(json.dumps(rows))')
    [[ "$FOUND" != "[]" && -n "$FOUND" ]] && break
    sleep 20
  done
  if [[ "$FOUND" == "[]" || -z "$FOUND" ]]; then
    printf 'UNSCORED  %-30s %s\n' "ops-span-emitted" \
      "no ops.* span for trace $TRACE_ID within ${SPAN_WAIT_SECONDS}s"
    UNSCORED=$((UNSCORED + 1))
  else
    # What is asserted is not merely that a span arrived: #63 asks for the
    # confirmation state and the reference to be ON it, because those two
    # attributes are the whole of what makes gate 2 auditable in a trace.
    echo "$FOUND" | python3 -c '
import json, sys

rows = json.load(sys.stdin)
names = sorted({row[0] for row in rows})
blob = " ".join(str(row[1]) for row in rows)
state = "confirmation" in blob
reference = "reference" in blob or "draft" in blob
print("      spans: " + ", ".join(names))
ok = state and reference
print("%s  %-34s confirmation state %s, reference %s" % (
    "PASS" if ok else "FAIL",
    "ops-span-emitted",
    "present" if state else "ABSENT",
    "present" if reference else "ABSENT",
))
sys.exit(0 if ok else 1)
' || FAILURES=$((FAILURES + 1))
  fi
fi

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "VERDICT: $FAILURES probe(s) failed, $UNSCORED not measured."
  exit 1
fi
echo "VERDICT: every probe put held; $UNSCORED not measured (see the header)."
