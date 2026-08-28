#!/bin/bash
set -u
AI=$(terraform -chdir=infra/terraform output -raw application_insights_name)
Q="dependencies | where timestamp > ago(40m) | where name startswith 'chat.turn' or name startswith 'tool.' or name startswith 'ops.' | project timestamp, name, sess=tostring(customDimensions['chip_chat.session.id']), demo=tostring(customDimensions['chip_chat.demo.id']) | order by timestamp asc | take 80"
az monitor app-insights query --app "$AI" -g rg-chip-chat --analytics-query "$Q" -o tsv
