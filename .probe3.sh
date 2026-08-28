#!/bin/bash
set -u
U=https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io
JAR=$(mktemp)
for i in 1 2 3; do
  curl -sS --max-time 60 -c "$JAR" -b "$JAR" -X POST "$U/api/entry" \
    -H 'Content-Type: application/json' -d '{"name":"Casey"}' \
    | python3 -c "import json,sys;d=json.load(sys.stdin);v=d.get('visitor') or {};print('entry',$i,v.get('persona_id'),v.get('home_store'))"
done
echo "--- lanes seen by the same session ---"
curl -sS --max-time 60 -c "$JAR" -b "$JAR" "$U/healthz/lanes" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print({l['lane']:l['state'] for l in d['lanes']})"
rm -f "$JAR"
