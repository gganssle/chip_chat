#!/usr/bin/env bash
#
# Verify, against the live account, that uploaded photos actually expire.
#
# Issue #51 asks for the expiry to be "confirmed by observing an object
# disappear", and that observation takes 24-48 hours of wall-clock: lifecycle
# rules have day granularity and the engine takes up to a day to begin
# executing. This script is the part that can be checked in seconds, run twice
# a day apart:
#
#   1. Assert soft delete is OFF. This is the setting that makes a correct
#      lifecycle rule a lie -- with it on, the rule soft-deletes and the images
#      are retained for the full soft-delete window, while a container listing
#      shows them gone on schedule. It can be turned on from the portal without
#      touching Terraform, so it is worth re-reading from the account rather
#      than from the state file.
#   2. Assert the lifecycle rule exists, is enabled, and deletes.
#   3. Print every blob in the uploads container with its age, so the "watch an
#      object disappear" criterion becomes: run this, note a name, run it again
#      tomorrow, and see that the name is gone.
#
# Read-only. Safe to run against production, and meant to be.

set -euo pipefail

SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-c8b63a71-218d-4d4c-991c-b963ed2fd1f0}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-chip-chat}"
CONTAINER="${AZURE_UPLOADS_CONTAINER:-uploads}"

az account set --subscription "$SUBSCRIPTION_ID"

STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-}"
if [[ -z "$STORAGE_ACCOUNT" ]]; then
  # The data account is the one with the hierarchical namespace; the functions
  # account next to it does not take uploads.
  STORAGE_ACCOUNT=$(az storage account list -g "$RESOURCE_GROUP" \
    --query "[?isHnsEnabled].name | [0]" -o tsv)
fi
if [[ -z "$STORAGE_ACCOUNT" || "$STORAGE_ACCOUNT" == "null" ]]; then
  echo "no uploads storage account found in $RESOURCE_GROUP" >&2
  echo "set AZURE_STORAGE_ACCOUNT, or run 'make infra-apply' first" >&2
  exit 1
fi

echo "account   $STORAGE_ACCOUNT"
echo "container $CONTAINER"
echo

failed=0
check() {  # check <description> <actual> <expected>
  if [[ "$2" == "$3" ]]; then
    printf '  ok    %-46s %s\n' "$1" "$2"
  else
    printf '  FAIL  %-46s %s (expected %s)\n' "$1" "$2" "$3"
    failed=1
  fi
}

# --- 1. soft delete, versioning and change feed ------------------------------

properties=$(az storage account blob-service-properties show \
  --account-name "$STORAGE_ACCOUNT" -g "$RESOURCE_GROUP" -o json)

check "blob soft delete disabled" \
  "$(jq -r '.deleteRetentionPolicy.enabled // false' <<<"$properties")" "false"
check "container soft delete disabled" \
  "$(jq -r '.containerDeleteRetentionPolicy.enabled // false' <<<"$properties")" "false"
check "versioning disabled" \
  "$(jq -r '.isVersioningEnabled // false' <<<"$properties")" "false"
check "change feed disabled" \
  "$(jq -r '.changeFeed.enabled // false' <<<"$properties")" "false"

# --- 2. the lifecycle rule ---------------------------------------------------

policy=$(az storage account management-policy show \
  --account-name "$STORAGE_ACCOUNT" -g "$RESOURCE_GROUP" -o json 2>/dev/null || echo '{}')
rule=$(jq -r --arg c "$CONTAINER" \
  '.policy.rules[]? | select(.definition.filters.prefixMatch[]? == ($c + "/"))' \
  <<<"$policy")

if [[ -z "$rule" ]]; then
  printf '  FAIL  %-46s %s\n' "lifecycle rule covering $CONTAINER/" "missing"
  failed=1
else
  check "lifecycle rule enabled" "$(jq -r '.enabled' <<<"$rule")" "true"
  days=$(jq -r '.definition.actions.baseBlob.delete.daysAfterCreationGreaterThan // "none"' \
    <<<"$rule")
  check "deletes after (days since creation)" "$days" "1"
fi

# --- 3. what is in there now -------------------------------------------------

echo
echo "blobs in $CONTAINER (note a name, re-run tomorrow, watch it go):"
az storage blob list \
  --account-name "$STORAGE_ACCOUNT" --container-name "$CONTAINER" \
  --auth-mode login --num-results 50 \
  --query "[].{name:name, created:properties.creationTime, bytes:properties.contentLength}" \
  -o table 2>/dev/null || echo "  (could not list -- needs Storage Blob Data Reader on the account)"

echo
if (( failed )); then
  echo "FAILED: uploaded photos are not expiring the way the copy promises." >&2
  exit 1
fi
echo "Configuration is correct. Deletion happens 24-48 hours after upload."
