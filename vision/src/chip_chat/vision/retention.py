"""What we tell strangers about their photographs, and why the number is 48.

The design says uploads are deleted after 24 hours. They are not, and cannot be.
Azure blob lifecycle management has **day granularity**, and the engine takes up
to 24 hours to begin executing after a policy change and then runs periodically
(``docs/service-inventory.md``, checked 2026-08-25). A blob written one minute
after a run is therefore collected by the *next* one. The honest envelope is:

.. code-block:: text

    upload ──▶ 24h  (the shortest a lifecycle rule can express)
               + up to 24h  (the engine's run interval)
               = deleted 24-48 hours after upload

So the copy says 48. This is a promise made to strangers about their own
photographs and it should be one we actually keep, which means quoting the
ceiling rather than the best case. :data:`RETENTION_CEILING_HOURS` is the single
place that number lives; the notice below is derived from it, and
``vision/tests/test_retention.py`` holds it against what the Terraform in
``infra/terraform/storage.tf`` actually configures.

**The trap is soft delete.** A correct-looking lifecycle rule on an account with
blob soft delete enabled does not delete anything: it *soft*-deletes, and the
images are then retained for the full soft-delete window. That is precisely the
quiet accumulation the design says it does not do, and it is invisible from the
outside -- the blob disappears from a listing on schedule and stays on the
account for a week. Soft delete is off on the uploads account, asserted by a
Terraform postcondition and again by a test here, because the way this setting
gets turned on is somebody enabling it for a different container.
"""

from typing import Final

__all__ = [
    "RETENTION_CEILING_HOURS",
    "RETENTION_NOTICE",
    "RETENTION_NOTICE_LONG",
]

LIFECYCLE_RETENTION_DAYS: Final = 1
"""``uploads_retention_days`` in ``infra/terraform/variables.tf``. The floor is 1."""

LIFECYCLE_EXECUTION_LAG_HOURS: Final = 24
"""How long the lifecycle engine may take to get to a blob that is due."""

RETENTION_CEILING_HOURS: Final = (
    LIFECYCLE_RETENTION_DAYS * 24 + LIFECYCLE_EXECUTION_LAG_HOURS
)
"""The longest an uploaded photo can survive: 48 hours. The number in the copy."""

RETENTION_NOTICE: Final = f"Deleted within {RETENTION_CEILING_HOURS} hours."
"""One line, for the upload control and the confirmation card."""

RETENTION_NOTICE_LONG: Final = (
    f"Your photo is used to read the meal in it and nothing else. "
    f"It is deleted within {RETENTION_CEILING_HOURS} hours, "
    f"and the location data cameras attach to photos is removed before it is stored."
)
"""The fuller version, for wherever there is room to say it properly."""
