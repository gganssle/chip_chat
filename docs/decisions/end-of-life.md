# Decision: this proof of concept ends with the Snowflake trial, and buys nothing

**Decided by Graham, 28 August 2026.** Two questions were open and both are now
closed in the same direction, so they are recorded together.

## The decisions

1. **The Snowflake trial will never be converted to a paid account.** The work
   will be finished before it expires on **2026-09-24**.
2. **No permanent domain will be bought.** The demo stays on the Container Apps
   default FQDN with its automatic managed certificate.

## What this settles, and what it costs

### The rebuild path stays untested, and that is now fine

`docs/runbook.md` §9 and §11 record that `make snowflake-rebuild` has never been
run, because running it would destroy the synthetic population irrecoverably —
the landing zone it was generated from is not in the repository. That was
flagged as a risk **specifically because** the day-30 plan was *rebuild on
demand*, which is a plan that depends on a path nobody has walked.

That risk is now retired, not by testing the path but by removing the need for
it. The demo is not intended to outlive the trial, so a rebuild is not on the
critical path of anything. **Do not spend effort making the rebuild testable.**

The related recommendation in `docs/launch-readiness.md` §4.5 is superseded by
this record.

One consequence worth keeping in view: **2026-09-24 is a hard stop, not a
soft one.** When the trial expires the account lane, the personalization lane
and the action lane all stop working simultaneously, because all three read or
write Snowflake. The knowledge lane and the entry flow would survive. Nothing in
the system degrades gracefully into that date — it is a cliff, and the
`docs/runbook.md` teardown procedure is what should be run before it rather than
after.

### The hostname is not stable, and nobody should depend on it

Issue #4 was closed without buying a domain, and this record makes that
permanent rather than provisional. The live URL is

<https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io>

and `docs/deployment.md` §1 already says the important part: **the hostname
changes if the app is ever recreated**, so it should be read from
`terraform output web_url` and never copied from a document. Anything that
embeds this hostname — a recorded demo, a link in a writeup, a bookmark — is
correct only until the next `terraform destroy`.

The managed certificate is automatic and free on the default domain, so the
choice costs nothing in trust or in TLS; what it costs is permanence, and
permanence is not something a five-week proof of concept needs.

## Why this is the right call rather than a concession

The point of this project was never to run a service. It was to exercise four
platforms' authentication, deployment and evaluation stories end to end and to
be able to argue about the decisions afterwards. `docs/writeup.md` is the
deliverable that outlives the demo; the demo itself is scaffolding.

A paid Snowflake account and a purchased domain would both be spending money to
extend the life of the scaffolding rather than to improve the deliverable. The
budget guardrails in `docs/cost.md` exist to keep this at hobby cost, and the
largest line in that document is already **standing infrastructure that bills
for existing rather than for being used** — the NAT gateway charges 44× more to
exist than to carry every byte ever sent through it. Adding two more standing
costs to a thing with a known end date would be the same mistake, twice.

## What to do instead, when the time comes

Run the teardown in `docs/runbook.md` deliberately and before the expiry, rather
than letting the trial lapse and discovering which pieces broke first. Teardown
being one command is half the reason everything went into Terraform, and it is
worth actually exercising once — it is the one operational path this project can
still prove, and the rebuild path is now the one it never will.

## References

`docs/runbook.md` §9, §11 · `docs/launch-readiness.md` §4.5 (superseded) ·
`docs/snowflake-account.md` §3.4, §10 · `docs/deployment.md` §1 ·
`docs/cost.md` · Issue #4, Issue #40.
