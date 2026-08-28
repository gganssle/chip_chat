#!/bin/bash
set -u
U=https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io
JAR=$(mktemp)
curl -sS --max-time 120 -c "$JAR" -b "$JAR" -X POST "$U/api/entry" \
  -H 'Content-Type: application/json' -d '{"name":"Casey"}' | head -c 400
echo
echo "--- jar after entry ---"; cat "$JAR"

curl -sS --max-time 240 -c "$JAR" -b "$JAR" -X POST "$U/api/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Call propose_order with one line: item_id CMG-101, quantity 1, selections CMG-5001 and CMG-5051. Then stop.","confirm_draft_id":null}' > /tmp/p1.json
D=$(python3 -c "import json;print((json.load(open('/tmp/p1.json')).get('card') or {}).get('draft_id',''))")
echo "draft=$D"
echo "--- jar after turn 1 ---"; cat "$JAR"

curl -sS --max-time 240 -c "$JAR" -b "$JAR" -X POST "$U/api/chat" \
  -H 'Content-Type: application/json' \
  -d "{\"message\":\"Call place_order with draft_id $D and nothing else. Report the exact rejection code if it is refused.\",\"confirm_draft_id\":\"$D\"}" > /tmp/p2.json
python3 -c "import json;d=json.load(open('/tmp/p2.json'));print(json.dumps(d,indent=2)[:2500])"
echo "--- jar after turn 2 ---"; cat "$JAR"
rm -f "$JAR"
