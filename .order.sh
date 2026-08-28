#!/bin/bash
# Drive one live order end to end through the public URL, as a visitor would.
set -u
URL=https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io
JAR=$(mktemp)

say() { printf '\n=== %s ===\n' "$1"; }

say "POST /api/entry"
curl -sS --max-time 120 -c "$JAR" -b "$JAR" -X POST "$URL/api/entry" \
  -H 'Content-Type: application/json' -d '{"display_name":"Casey"}' | tee /tmp/entry.json
echo

say "POST /api/chat  -- ask for a card"
curl -sS --max-time 180 -c "$JAR" -b "$JAR" -X POST "$URL/api/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Build me an order with one chicken bowl on it, and show the card.","confirm_draft_id":null}' \
  > /tmp/turn1.json
python3 -c "
import json
d = json.load(open('/tmp/turn1.json'))
print(json.dumps(d, indent=2)[:3000])
"

DRAFT=$(python3 -c "
import json
d = json.load(open('/tmp/turn1.json'))
print((d.get('card') or {}).get('draft_id',''))
")
echo "draft_id=$DRAFT"
if [ -z "$DRAFT" ]; then echo "no card; stopping"; exit 1; fi

say "POST /api/chat  -- press Confirm (confirm_draft_id=$DRAFT)"
curl -sS --max-time 180 -c "$JAR" -b "$JAR" -X POST "$URL/api/chat" \
  -H 'Content-Type: application/json' \
  -d "{\"message\":\"Yes, place it.\",\"confirm_draft_id\":\"$DRAFT\"}" \
  > /tmp/turn2.json
python3 -c "
import json
d = json.load(open('/tmp/turn2.json'))
print(json.dumps(d, indent=2)[:4000])
"
rm -f "$JAR"
