#!/usr/bin/env bash
#
# Create the remote state backend for infra/terraform.
#
# This is the one piece of the estate that cannot be Terraform, because it is
# where Terraform's state lives: a stack cannot hold the storage account its own
# state file is in without a chicken-and-egg problem at both create and destroy.
# Making it a shell script rather than a second Terraform stack avoids an orphan
# local state file that nobody remembers to keep.
#
# It lives in its OWN resource group, deliberately. Issue #5's acceptance
# criterion is that `terraform destroy` leaves rg-chip-chat empty; if the state
# account were in that group, either destroy would fail or the state would go
# with it.
#
# Idempotent. Safe to re-run.

set -euo pipefail

SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-c8b63a71-218d-4d4c-991c-b963ed2fd1f0}"
LOCATION="${LOCATION:-eastus2}"
RESOURCE_GROUP="${TFSTATE_RESOURCE_GROUP:-rg-chip-chat-tfstate}"
# Deterministic, so that the backend block in versions.tf can be a literal.
# Storage account names are globally unique; the subscription prefix is what
# makes this one so.
STORAGE_ACCOUNT="${TFSTATE_STORAGE_ACCOUNT:-sttfstate${SUBSCRIPTION_ID:0:6}}"
CONTAINER="${TFSTATE_CONTAINER:-tfstate}"

echo "subscription   $SUBSCRIPTION_ID"
echo "resource group $RESOURCE_GROUP ($LOCATION)"
echo "storage        $STORAGE_ACCOUNT/$CONTAINER"
echo

az account set --subscription "$SUBSCRIPTION_ID"

az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --tags project=chip_chat phase=0 issue=gh-5 purpose=terraform-state \
  --output none
echo "resource group ready"

if ! az storage account show -n "$STORAGE_ACCOUNT" -g "$RESOURCE_GROUP" --output none 2>/dev/null; then
  az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    --allow-shared-key-access false \
    --https-only true \
    --tags project=chip_chat phase=0 issue=gh-5 purpose=terraform-state \
    --output none
  echo "storage account created"
else
  echo "storage account already exists"
fi

# State is small, it changes on every apply, and losing it means reconciling the
# estate by hand. Versioning is the cheapest possible insurance against a
# corrupt or truncated write.
az storage account blob-service-properties update \
  --account-name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --enable-versioning true \
  --enable-delete-retention true \
  --delete-retention-days 30 \
  --output none
echo "versioning and 30-day soft delete enabled"

# Shared keys are disabled on the account, so the container has to be created
# over Entra auth, and so does every later read of the state file. That is why
# the backend block sets use_azuread_auth.
CALLER_OBJECT_ID="$(az ad signed-in-user show --query id -o tsv)"
SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT"

if ! az role assignment list --assignee "$CALLER_OBJECT_ID" --scope "$SCOPE" \
      --role "Storage Blob Data Owner" --query "[0].id" -o tsv | grep -q .; then
  az role assignment create \
    --assignee-object-id "$CALLER_OBJECT_ID" \
    --assignee-principal-type User \
    --role "Storage Blob Data Owner" \
    --scope "$SCOPE" \
    --output none
  echo "granted Storage Blob Data Owner to the signed-in user"
  # RBAC on the data plane is eventually consistent; the container create below
  # will fail with AuthorizationPermissionMismatch if it runs too soon.
  echo "waiting 30s for the role assignment to propagate"
  sleep 30
else
  echo "role assignment already in place"
fi

az storage container create \
  --name "$CONTAINER" \
  --account-name "$STORAGE_ACCOUNT" \
  --auth-mode login \
  --output none
echo "container ready"

echo
echo "Backend is ready. Next:"
echo "  make infra-init"
