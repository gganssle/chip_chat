"""The sentence that tells a visitor who they have been set up as.

It lives in ``web/`` rather than in ``agent/`` because it is copy, and it is
derived from :data:`chip_chat.agent.hardcoded.ACCOUNT` rather than written out
again because the one thing worse than a demo persona is a demo persona whose
opening message and whose points balance disagree.

That import is the only thing ``web/`` takes from ``agent/``. When the loaded
personas become real (issue #29) this file changes shape and the page does not.
"""

from chip_chat.agent.hardcoded import ACCOUNT

__all__ = ["ACCOUNT_SUMMARY"]

ACCOUNT_SUMMARY = (
    f"You are signed in as {ACCOUNT.display_name}: a member since "
    f"{ACCOUNT.member_since} at the {ACCOUNT.home_store.name} store, with "
    f"{ACCOUNT.points_balance:,} points and a standing order of "
    f"{ACCOUNT.usual_order}."
)
"""Who the visitor is, in one sentence, on entry."""
