# Is the Databricks workspace running what this repository says?

`make infra-check-databricks` answers that question in about eleven seconds, for
the eight library modules under `/Shared/chip-chat/lib` and the sixteen notebooks
beside them, and it exists because on 2026-08-28 the answer was no and nothing
noticed.

Bead `chip-rxs`. The code is
`infra/src/chip_chat/infra/workspace_drift.py`; the tests are
`infra/tests/test_workspace_drift.py`; the target and its argument for not being
in `make ci` are in the `Infrastructure` family of the `Makefile`; and
`docs/runbook.md` §8 is the operator-facing form, written twice the way every
procedure there is.

---

## 1. The failure this is the answer to

On 2026-08-28 the nightly publish failed with:

```
CHIP_CHAT.ACCOUNTS.orders holds 0 rows after the swap and staging held 18898.
If the count is HIGHER, the truncate half of INSERT OVERWRITE did not remove
every row -- check whether a row access policy on the table filters
CHIP_CHAT_PUBLISH, which it must not. If it is LOWER, the swap did not land.
```

That message is a good message. It names a cause, and the cause it names is the
one that produced this exact failure the first time, on 2026-08-27 —
`docs/nightly-publish.md` §7 is the whole story. It was also, the second time,
wrong, and it was wrong in the most expensive way a diagnostic can be: by
pointing a confident finger at the row access policy, which is the single most
load-bearing guarantee in this system and the thing you least want somebody
editing at three in the morning on the strength of an error message.

The policy was fine. `/Shared/chip-chat/lib/publish.py` in the workspace was 850
lines against the repository's 887 and had no `row_count()` in it at all. It was
still counting with `SELECT COUNT(*)`, which is exactly what returns zero for an
unbound publisher under `VISITOR_ISOLATION`, and which is exactly what the
committed fix had replaced with a read of
`INFORMATION_SCHEMA.TABLES.ROW_COUNT`. The deployed `snowflake_publish` notebook
matched the stale module rather than the repository. Every other lib file and
every other notebook matched. The fix had been committed, reviewed, tested by
`make ci`, and never applied.

It was repaired with `databricks workspace import --overwrite` on those two
paths — which is what Terraform would have written — and the next run passed.

## 2. Why nothing caught it, which is not the obvious answer

The obvious answer is "Terraform cannot see this". Terraform can see this
perfectly. `databricks_workspace_file.publish_module` and
`databricks_notebook.snowflake_publish` both carry a `source` pointing at a file
in this tree, the provider hashes the file, and `terraform plan` would have
printed both of them as changed. An apply is what repaired the estate.

The gap is that **nothing anybody runs asks Terraform the question.** A plan is
not on any schedule, `make ci` does not run one, and there is no reason an
engineer merging a change to `publish.py` would think to. The commit that fixed
`row_count` was a change to a Python module in a package whose tests are green;
that it was *also* a change to the contents of a deployed file is a fact the diff
does not put in front of you.

And `make ci` cannot close it. A plan needs an initialised Terraform backend and
a workspace credential, and CLAUDE.md's rule — "nothing that costs money or needs
a credential is in `make ci`, and that is a rule rather than an oversight" — is
the right rule and this is not the exception to it. A gate that needs a logged-in
human is not a gate; it is a step people learn to skip.

So the thing to build was never "put a plan in CI". It was: make the question
cheap enough, specific enough and quiet enough that an operator runs it after an
apply and before believing a nightly failure is about the thing its error message
names.

## 3. Why not `terraform plan`

A plan would work. It is also the wrong tool for the question, for four reasons
that compound:

**It answers a much larger question.** A plan over this stack refreshes every
Azure resource in it — the container app, the search service, the Foundry
project, the storage accounts, the budget. Twenty-four file hashes are somewhere
in that output. Reading a plan to find them is slower than reading twenty-four
lines.

**It needs the backend.** `docs/runbook.md` §1 is an entire section about the
fact that every operations target in this repository resolves its arguments
through `terraform output`, and therefore fails in a fresh clone, in Cloud Shell,
or in any worktree nobody ran `make infra-init` in. That failure mode is real and
was reproduced on 2026-08-27. There is no reason to inherit it here: this check
needs a Databricks credential and nothing else — no state file, no Azure login,
no initialised working directory.

**It is not read-only in the way that matters.** A plan is read-only against the
estate, but it acquires the state lock and refreshes state, and running one
casually against production while somebody else is applying is a bad habit to
teach. `databricks workspace export` cannot do anything.

**It cannot tell you what changed.** A plan says `~ source_code_hash`. This says
which function is missing, with a unified diff. In the incident above that
distinction is the whole value: the finding is "the deployed module has no
`row_count`", and a hash tells you nothing about why the job behaved the way it
did.

## 4. How the list of twenty-four paths is derived

**From the Terraform source, parsed.** Not from a list in the script.

A hardcoded list would be correct on the day it was written and would then rot in
exactly the way that produced this bug: somebody adds a ninth module, the check
keeps passing, and the ninth module is the stale one. So `managed_files()` globs
`infra/terraform/databricks_*.tf`, finds every `databricks_workspace_file` and
`databricks_notebook` resource block, and resolves the `path` and `source`
arguments. `make infra-list-databricks` prints what it derived, needs no
credential, and is free.

The interpolations it resolves are `${path.module}`, which is the directory of
the `.tf` file being read, and `${local.*}`. The lib path is worth following
because it is the one piece of indirection: `databricks_bronze.tf` declares
`bronze_lib_path = "/Shared/${local.base}/lib"` and four other files alias it
(`publish_lib_path = local.bronze_lib_path`, and so on), so all eight modules
land in one directory and no file has to know that. `terraform_locals()` follows
the aliases; `test_the_lib_path_locals_resolve_through_their_aliases` holds it to
resolving all five to the same string, because an alias that stopped resolving
would leave a literal `${local.…}` in the path and report all eight modules as
"not deployed" — a spectacular-looking false positive.

### The parser is a regular expression, and that is a trade

There is no HCL parser in this workspace's lockfile, and adding a dependency to
every developer's virtualenv in order to check twenty-four file hashes is
disproportionate. The regex depends on one property of the source:
a top-level block's closing brace is at column zero and every nested brace is
indented. `terraform fmt` guarantees that, `make infra-fmt` runs it, and the tree
is formatted.

What a regex cannot do is evaluate HCL. So rather than skipping what it cannot
evaluate — which would silently shorten the list, which is the exact failure this
whole document is about — it **stops**:

| It meets | It does |
| --- | --- |
| `count`, `for_each` or `dynamic` on a managed resource | fails, naming the resource: those manage files it cannot name, and unchecked-but-believed-checked is worse than not running |
| `${var.something}` or a function call in `path` or `source` | fails, quoting the expression |
| `${local.x}` where `x` is not a string local it resolved | fails, naming the local |
| a `databricks_*.tf` glob that matches nothing | fails |

### And a second tripwire on top of it

`EXPECTED_WORKSPACE_FILES = 8` and `EXPECTED_NOTEBOOKS = 16` in
`workspace_drift.py` are checked against the derived counts before any export
runs, and a disagreement is exit 2 with a message naming both explanations.

This looks like the hardcoding the derivation was supposed to avoid, and it is
the opposite. The list is still derived; these are an assertion about its
*size*. The failure they guard is the quiet one: a parser that has stopped
matching finds nothing, reports no drift, and **no drift is precisely what a
healthy workspace reports.** A check whose broken state and whose passing state
are indistinguishable is worse than no check, because it is a check people
believe. When Terraform gains a resource this fails on the next run, which is a
one-line edit here and in §7 of this document, in the same commit — and that is
the intended cost, not an oversight.

### The one value that is not derived

`local.base`, the stack prefix — `chip-chat` for the live `demo` stack,
`chip-chat-<environment>` for a disposable one. It is computed by a ternary on
`var.environment` in `locals.tf`, and evaluating a ternary means either
evaluating HCL or shelling out to `terraform output`, which is the dependency §3
argues for not having.

So it is not derived; it is defaulted to the live stack's value, overridable with
`--base` (or `make infra-check-databricks DATABRICKS_BASE=…`), and
`check_base_assumption()` re-reads `locals.tf` on every run to confirm the source
still computes what the default assumes. That is the honest version of a
hardcoded string: it does not pretend to have derived the value, it verifies that
the value it did not derive is still true, and it warns rather than fails if it
is not — because checking a differently-named stack is a legitimate thing to do
and `--base` is how you say so.

## 5. What the check does, and what happened when it was run

Twenty-four sequential `databricks workspace export` calls, each compared against
the file the `source` argument points at.

- **Clean:** one line, exit 0. `-q` prints nothing at all.
- **Drifted:** a summary line and a unified diff per path — deployed on the left,
  committed on the right — then a count and both repair commands, `make
  infra-apply` and the single-path `databricks workspace import --overwrite`, so
  that the laptop form and the phone form are both in front of you.
- **A path the workspace does not have:** reported as `NOT DEPLOYED` drift, not
  as an error. An apply that never ran is the most consequential finding
  available and must not be confused with the CLI failing.
- **Cannot run at all** (no credential, no CLI, a parser refusal, a count
  disagreement): exit **2**, not 1. A caller that collapses "the check is broken"
  into "the workspace is wrong" will eventually act on the second when the first
  is true.

Those three codes are the *module's*. `make` collapses any failed recipe into its
own exit 2, so anything that wants to tell drift apart from a broken check should
call `uv run python -m chip_chat.infra.workspace_drift` rather than the target.
The target is for a human reading the output, which is what it is for.

### It was run, and here is what it found

**Run against the live workspace on 2026-08-28**, profile `DEFAULT`, host
`adb-7405614862446074.14.azuredatabricks.net`:

| | |
| --- | --- |
| Paths checked | 24 (8 modules, 16 notebooks) |
| Wall clock | **10.9 s**, sequential, measured with `time` |
| Result against `main` (`87a78fb`) | **clean** — all 24 byte-identical |
| Result against the working tree | 1 drifted: `lib/silver.py` |

Both numbers are findings.

The clean result against `main` is the evidence that the `chip-rxs` repair
holds: `publish.py` and `snowflake_publish` in the workspace are the committed
ones, and so is everything else.

The drifted result against the working tree is not a false positive, and it is
worth being clear about why, because it is the check's most important behaviour.
`databricks/src/chip_chat/databricks/silver.py` had uncommitted changes in the
checkout at the time — a `MAXIMUM_DOCUMENT_SHARE` rework, in progress. The
deployed file matched `HEAD` exactly and did not match the working tree. **The
check compares against your checkout, not against `HEAD`**, which is the right
default because your checkout is what an apply would upload; and what it printed
was, precisely, *"you have a change to a deployed file that you have not
applied"*. That is the same sentence as the `chip-rxs` finding. The difference
between the incident and a dirty working tree is a `git commit` and an apply, and
the check does not care which side of that you are on.

If you want the question asked of `main` rather than of your desk, point it at a
clean tree:

```bash
mkdir -p /tmp/head && git archive HEAD | tar -x -C /tmp/head
uv run python -m chip_chat.infra.workspace_drift \
  --repo-root /tmp/head --terraform-dir /tmp/head/infra/terraform
```

That is how the `main` row above was measured.

### The export format needed no normalisation, which was not obvious in advance

`databricks workspace export` returns notebooks in `SOURCE` format, and
`databricks_notebook` uploads `SOURCE`, so the round trip is lossless — including
the `# Databricks notebook source` first line and the `# COMMAND ----------`
separators, both of which are in the checked-in files. All twenty-four exports
compared **byte-identical**, with no whitespace or trailing-newline handling
applied. The one normalisation the code does apply is `\r\n` → `\n` on both
sides, and it has never yet been load-bearing; it is there so that a file
round-tripped through a Windows editor produces a one-line finding instead of a
twenty-four-page diff.

## 6. What this does not check

Written down rather than left to be discovered, in the spirit of the rest of
`docs/`:

**Anything in the workspace that Terraform does not manage.** The check walks the
Terraform's list and asks the workspace about each entry. It never lists the
workspace, so a ninth module hand-imported into `/Shared/chip-chat/lib` by
somebody debugging at three in the morning is invisible to it — and the
`chip-rxs` repair was itself a hand `databricks workspace import`, so this is not
a hypothetical habit. It is a real gap and a deliberate one: the finding this was
built for is "deployed is behind committed", and listing the workspace answers a
different question that would be better asked by a plan.

**Jobs, pipelines, clusters, policies, secret scopes, catalogs, grants.** Every
other `databricks_*` resource in `infra/terraform/`. A job whose schedule was
changed in the console, a cluster policy edited by hand, a grant added
imperatively — none of them are visible here. `terraform plan` is the tool for
those and this is not a substitute for it.

**Whether the deployed file *works*.** It is a byte comparison. `publish_verify`
and the `*_verify` jobs are what assert behaviour.

**Anything on a schedule.** Nothing runs this. It is a target an operator invokes
after an apply and during triage. Putting it on a schedule would need somewhere
to put a Databricks credential and somewhere for the failure to go, which is a
larger piece of work than the bead asked for.

### Not measured

- **The drift path has never been exercised against a real stale workspace.** The
  2026-08-28 run found real drift against the working tree, which exercised the
  diff, the summary, the exit code and the repair message end to end — but
  against an uncommitted local edit, not against a workspace that had genuinely
  fallen behind. The `chip-rxs` situation itself was repaired before this check
  existed. The synthetic version of that case is covered offline in
  `test_one_stale_module_exits_one_and_prints_its_diff`.
- **`NOT DEPLOYED` has never been seen live.** Every one of the twenty-four paths
  existed. The branch is covered by a stub in the tests and by nothing else, and
  in particular the `RESOURCE_DOES_NOT_EXIST` string matching in `export()` is
  written from the Databricks CLI's documented error and has not been observed.
- **Only one profile, one workspace, one CLI version.** Databricks CLI v1.13.0
  against `adb-7405614862446074.14.azuredatabricks.net`. `--profile` is
  passed straight through and has not been exercised.
- **No timing for a drifted run.** 10.9 s is the clean-run figure; the drifted
  run against the working tree was 11.6 s, which is one sample and within noise
  of the same thing.

## 7. If you add a ninth module or a seventeenth notebook

The check will fail on its next run with a count disagreement, on purpose. Then:

1. Update `EXPECTED_WORKSPACE_FILES` or `EXPECTED_NOTEBOOKS` in
   `infra/src/chip_chat/infra/workspace_drift.py`.
2. Update the counts in §1 and §5 of this document.
3. `uv run pytest infra/tests` — `test_every_managed_resource_in_the_terraform_is_found`
   reads the same constants and will already be red.

All three in the commit that adds the resource. That is the whole cost of the
tripwire, and it buys the guarantee that a short list can never be mistaken for a
clean workspace.

---

## References

`docs/nightly-publish.md` §7 — the row access policy, the `row_count` fix that
was not deployed, and the three checks added to `make snowflake-verify` after the
first version of this same failure. `docs/runbook.md` §1 (why `make` does not
work from a phone), §8 (the procedure), §10 (triage), §11 (what has not been
run). `infra/README.md` — the estate. Bead `chip-rxs`.
