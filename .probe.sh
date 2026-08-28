#!/bin/bash
set -u
U=https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io
JAR=$(mktemp)
curl -sS --max-time 240 -c "$JAR" -b "$JAR" -X POST "$U/api/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Call propose_order with exactly one line: item_id CMG-101, quantity 1, no selections. Show me the card.","confirm_draft_id":null}' \
  > /tmp/probe.json
python3 -c "
import json
print(json.dumps(json.load(open('/tmp/probe.json')), indent=2)[:3000])
"
rm -f "$JAR"
