"""The 48-hour promise, held against the infrastructure that has to keep it.

This test lives in ``vision/`` rather than ``infra/`` because the promise is
made here -- :data:`~chip_chat.vision.retention.RETENTION_NOTICE` is the string
a visitor reads -- and a promise is only worth testing next to the thing that
has to honour it. The Terraform is one edit away from making the copy a lie, in
a repository where nothing else would notice.

Two things are asserted, and the second is the one that matters:

1. The lifecycle rule exists, is enabled, and covers the uploads container.
2. **Blob soft delete is off on that account.** This is the trap. With soft
   delete on, the lifecycle rule still runs and the blob still vanishes from a
   listing on schedule -- and the image is retained for the full soft-delete
   window regardless. From outside, a correct-looking rule and silent long-term
   retention of exactly the data we promised to drop are indistinguishable. It
   is disabled by *omitting* two blocks, because the provider's minimum for
   ``days`` is 1 and there is no way to write "zero days" and mean it, so the
   omission is load-bearing and an ordinary-looking edit undoes it.

Reading HCL with a brace scanner is not elegant. The alternative is asserting
nothing until someone runs ``terraform plan`` against a live subscription, and
a promise made to strangers about their photographs deserves better than a
comment saying we remembered.
"""

import re
from pathlib import Path

import pytest

from chip_chat.vision.retention import (
    LIFECYCLE_EXECUTION_LAG_HOURS,
    LIFECYCLE_RETENTION_DAYS,
    RETENTION_CEILING_HOURS,
    RETENTION_NOTICE,
    RETENTION_NOTICE_LONG,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = REPO_ROOT / "infra" / "terraform"
STORAGE_TF = TERRAFORM / "storage.tf"
VARIABLES_TF = TERRAFORM / "variables.tf"


def _uncommented(source: str) -> str:
    """Strip ``#`` comments, so that prose about a setting is not read as the setting."""
    return re.sub(r"(?m)#.*$", "", source)


def _block(source: str, header: str) -> str:
    """Return the body of the first block whose opening line contains ``header``.

    Args:
        source: The HCL to scan.
        header: A substring of the block's opening line.

    Returns:
        The text between that line's ``{`` and its matching ``}``.

    Raises:
        AssertionError: If there is no such block, or it does not terminate.
    """
    assert header in source, f"no such block: {header}"
    start = source.index(header)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unterminated block: {header}")


@pytest.fixture(scope="module")
def uploads_account() -> str:
    """The ``azurerm_storage_account.data`` block -- the account uploads land in."""
    return _block(
        _uncommented(STORAGE_TF.read_text()),
        'resource "azurerm_storage_account" "data"',
    )


@pytest.fixture(scope="module")
def blob_properties(uploads_account: str) -> str:
    """The account's ``blob_properties``, where soft delete would be turned on.

    An account with no such block has soft delete off by default, so an empty
    string is the honest answer rather than a failure.
    """
    if "blob_properties" not in uploads_account:
        return ""
    return _block(uploads_account, "blob_properties")


@pytest.fixture(scope="module")
def management_policy() -> str:
    """The lifecycle policy attached to that account."""
    return _block(
        _uncommented(STORAGE_TF.read_text()),
        'resource "azurerm_storage_management_policy" "data"',
    )


# --- soft delete must be off -----------------------------------------------


def test_blob_soft_delete_is_off_on_the_uploads_account(blob_properties: str) -> None:
    assert "delete_retention_policy" not in blob_properties, (
        "Blob soft delete is enabled on the uploads account. The lifecycle rule "
        "then only soft-deletes, and strangers' photographs are retained for the "
        "full soft-delete window -- which is the accumulation the design says it "
        "does not do, and is invisible from a container listing."
    )


def test_container_soft_delete_is_off_for_the_same_reason(
    blob_properties: str,
) -> None:
    assert "container_delete_retention_policy" not in blob_properties


def test_versioning_is_off_so_an_overwrite_cannot_retain_the_old_image(
    blob_properties: str,
) -> None:
    # Blob versioning keeps prior versions past the lifecycle rule in exactly
    # the way soft delete does.
    assert re.search(r"versioning_enabled\s*=\s*false", blob_properties)


def test_terraform_asserts_the_same_thing_at_apply_time(uploads_account: str) -> None:
    # The test catches the edit in review; the postcondition catches it in a
    # subscription somebody changed by hand. Both, because the settings can
    # also be turned on from the portal.
    assert "postcondition" in uploads_account
    assert "delete_retention_policy) == 0" in uploads_account


# --- the lifecycle rule ----------------------------------------------------


def test_the_lifecycle_rule_covers_the_uploads_container(
    management_policy: str,
) -> None:
    assert "enabled = true" in management_policy
    assert 'prefix_match = ["${azurerm_storage_container.uploads.name}/"]' in (
        management_policy
    )
    assert 'blob_types   = ["blockBlob"]' in management_policy


def test_the_rule_deletes_rather_than_tiering(management_policy: str) -> None:
    assert "delete_after_days_since_creation_greater_than" in management_policy
    assert "var.uploads_retention_days" in management_policy


def test_terraform_and_the_copy_agree_on_how_long_a_photo_lives() -> None:
    match = re.search(
        r'variable "uploads_retention_days".*?default\s+=\s+(\d+)',
        VARIABLES_TF.read_text(),
        re.DOTALL,
    )
    assert match is not None, "uploads_retention_days has no default"
    assert int(match.group(1)) == LIFECYCLE_RETENTION_DAYS


# --- the copy --------------------------------------------------------------


def test_the_ceiling_is_the_rule_plus_the_engines_own_lag() -> None:
    assert RETENTION_CEILING_HOURS == (
        LIFECYCLE_RETENTION_DAYS * 24 + LIFECYCLE_EXECUTION_LAG_HOURS
    )
    assert RETENTION_CEILING_HOURS == 48


@pytest.mark.parametrize("copy", [RETENTION_NOTICE, RETENTION_NOTICE_LONG])
def test_the_copy_quotes_the_ceiling_and_not_the_best_case(copy: str) -> None:
    # Lifecycle rules have day granularity and the engine takes up to 24 hours
    # to begin executing, so "24 hours" is a promise the storage account cannot
    # keep. Saying 48 is the version that is true.
    assert "48 hours" in copy
    assert "24 hours" not in copy


def test_the_long_form_also_says_that_location_data_is_removed() -> None:
    # The other half of what a stranger is owed here, and the half stage 2 goes
    # out of its way to make true.
    assert "location data" in RETENTION_NOTICE_LONG
