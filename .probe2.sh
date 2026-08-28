#!/bin/bash
set -u
U=https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io
JAR=$(mktemp)
curl -sS --max-time 120 -c "$JAR" -b "$JAR" -X POST "$U/api/entry" \
  -H 'Content-Type: application/json' -d '{"name":"Casey"}' -o /dev/null

echo "--- one turn: propose then place immediately ---"
curl -sS --max-time 240 -c "$JAR" -b "$JAR" -X POST "$U/api/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"First call propose_order with one line: item_id CMG-101, quantity 1, selections CMG-5001 and CMG-5051. Then immediately call place_order with the draft id it returns. Report the exact rejection code verbatim if it is refused.","confirm_draft_id":null}' \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['reply'][:900]);print('card:',bool(d.get('card')),'receipt:',d.get('receipt'))"
rm -f "$JAR"
