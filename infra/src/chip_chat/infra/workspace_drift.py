"""Ask the Databricks workspace whether it is still running what this repository says.

The eight library modules under ``/Shared/chip-chat/lib`` and the sixteen
notebooks beside them are Terraform-managed — ``databricks_workspace_file`` and
``databricks_notebook`` resources, each with a ``source`` pointing at a file in
this tree. That makes ``terraform plan`` the authoritative drift detector, and
it is the reason nobody has ever had to think about this: the resources are
declared, so of course they are deployed.

They were not. On 2026-08-28 the nightly publish failed with
``CHIP_CHAT.ACCOUNTS.orders holds 0 rows after the swap and staging held 18898``
— the message ``publish.py`` emits when it suspects a row access policy, and the
message that had been added precisely because a row access policy caused this
failure once before. It was not the policy. ``/Shared/chip-chat/lib/publish.py``
in the workspace was 850 lines against the repository's 887 and carried no
``row_count()`` at all, so the job was still counting with ``SELECT COUNT(*)``,
which returns zero for an unbound publisher under ``VISITOR_ISOLATION``. The fix
had been committed weeks earlier and never applied. The deployed
``snowflake_publish`` notebook matched the stale module, every other path
matched the repository, and nothing anywhere told anyone.

That is the gap this module closes, and it is worth being precise about what the
gap actually was, because the obvious diagnosis is wrong. The gap was not that
Terraform cannot see this — it can, trivially, and an apply is what repaired it.
The gap was that **no gate anybody runs asks Terraform the question.**
``make ci`` cannot: a plan needs an initialised backend and a workspace
credential, and CLAUDE.md's rule that nothing costing money or needing a
credential goes in ``make ci`` is a rule rather than an oversight. So the check
has to be something an operator runs deliberately, which means it has to be
short, read-only, quiet when there is nothing to say, and specific enough about
what drifted that reading its output is faster than reading a plan.

Hence: not ``terraform plan``. A plan over this stack touches every Azure
resource in it, takes minutes, needs the backend, and answers a much larger
question than "is the deployed publish.py the committed publish.py". This asks
exactly that question, of exactly the twenty-four paths, with
``databricks workspace export`` on the read side and ``difflib`` on the compare
side. It needs a Databricks credential and nothing else — no Terraform state, no
Azure login, no initialised working directory. That last property is deliberate:
``docs/runbook.md`` §1 exists because every other operations target in this
repository resolves its arguments through ``terraform output`` and therefore
fails in a fresh clone, and there is no reason for this one to inherit that.

## Where the list of paths comes from

From the Terraform source, parsed, rather than from a list written down here.

A hardcoded list of twenty-four paths would be correct on the day it was written
and would then rot in exactly the way that produced this bug in the first place:
somebody adds a ninth module, the check keeps passing, and the ninth module is
the one that is stale. So ``managed_files()`` reads
``infra/terraform/databricks_*.tf``, finds every ``databricks_workspace_file``
and ``databricks_notebook`` resource block, and resolves the ``path`` and
``source`` arguments — including the ``${local.base}`` and ``${local.*_lib_path}``
interpolations, which it resolves out of the ``locals`` blocks in the same
files. A resource added tomorrow lands in the check tomorrow.

The parser is a regular expression over formatted HCL rather than a real HCL
parser, and that is a deliberate trade with a tripwire attached. There is no HCL
parser in this workspace's lockfile, adding one to check twenty-four file hashes
is disproportionate, and ``make infra-fmt`` (``terraform fmt``) guarantees the
one property the regex depends on: a block's closing brace is at column zero and
every nested brace is indented. What the regex cannot do is evaluate a ternary,
a ``for_each``, or a function call — so it refuses them loudly rather than
skipping them quietly, and ``EXPECTED_WORKSPACE_FILES`` / ``EXPECTED_NOTEBOOKS``
below are a second tripwire on top of that. If the derived counts stop matching,
the run fails and says so. Either Terraform gained a resource, in which case the
expectation and ``docs/workspace-drift.md`` want updating in the same commit, or
the parser stopped seeing something, in which case a silently short list would
have been the far worse outcome.

The one value that is *not* derived is ``local.base``, the stack name prefix. It
is computed by a ternary on ``var.environment`` — ``"chip-chat"`` for the live
``demo`` stack, ``"chip-chat-<env>"`` for a disposable one — and evaluating it
would mean either evaluating HCL or shelling out to ``terraform output``, which
is the dependency this module exists partly to avoid. So the default is the live
stack's value, ``--base`` overrides it for any other stack, and
``check_base_assumption()`` re-reads ``locals.tf`` to confirm the source still
computes what the default assumes. That check is the honest version of a
hardcoded string: it does not pretend to derive the value, it verifies that the
value it did not derive is still true.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The stack whose name Phase 0 created by hand and which `var.environment =
# "demo"` still produces. `check_base_assumption` re-derives that this is what
# `locals.tf` says, so the string here is an assumption that gets verified
# rather than an assumption that gets trusted.
DEFAULT_BASE = "chip-chat"

# The tripwire described in the module docstring. These are what Terraform
# manages today; a disagreement is a hard failure rather than a warning, because
# every way of reaching one is a way of checking fewer files than you think.
EXPECTED_WORKSPACE_FILES = 8
EXPECTED_NOTEBOOKS = 16

# Exit codes. Anything a caller might want to branch on gets its own.
EXIT_CLEAN = 0
EXIT_DRIFTED = 1
EXIT_ERROR = 2

REPO_ROOT = Path(__file__).resolve().parents[4]

_RESOURCE_KINDS = ("databricks_workspace_file", "databricks_notebook")

# A top-level HCL block, closed by a brace at column zero. `terraform fmt`
# indents every nested brace, so this does not need to count them.
_BLOCK = re.compile(
    r'^resource\s+"(?P<kind>databricks_workspace_file|databricks_notebook)"'
    r'\s+"(?P<name>[A-Za-z0-9_-]+)"\s*\{\n(?P<body>.*?)^\}',
    re.DOTALL | re.MULTILINE,
)
_LOCALS_BLOCK = re.compile(r"^locals\s*\{\n(?P<body>.*?)^\}", re.DOTALL | re.MULTILINE)
_ARGUMENT = re.compile(
    r'^\s{2}(?P<key>[a-z_]+)\s*=\s*"(?P<value>[^"]*)"\s*$', re.MULTILINE
)
_LOCAL_STRING = re.compile(
    r'^\s+(?P<key>[a-z0-9_]+)\s*=\s*"(?P<value>[^"]*)"\s*$', re.MULTILINE
)
_LOCAL_ALIAS = re.compile(
    r"^\s+(?P<key>[a-z0-9_]+)\s*=\s*local\.(?P<target>[a-z0-9_]+)\s*$", re.MULTILINE
)
_INTERPOLATION = re.compile(r"\$\{(?P<expr>[^}]*)\}")

# `${path.module}` is resolved by the caller, which knows where the .tf file it
# is reading lives. A NUL cannot occur in HCL, so this cannot collide.
PATH_MODULE_SENTINEL = "\0path.module\0"

# HCL meta-arguments the regex cannot evaluate. A resource carrying one is not
# skipped; the run stops, because "this file is not managed" and "this file is
# managed and I could not tell you its path" must not look the same.
_UNEVALUABLE = ("count", "for_each", "dynamic")


class DriftCheckError(Exception):
    """The check could not be performed, which is different from finding drift."""


@dataclass(frozen=True)
class ManagedPath:
    """One Terraform-managed file: where it lives deployed, and where it lives here."""

    kind: str
    """``databricks_workspace_file`` or ``databricks_notebook``."""

    address: str
    """The Terraform address, so a failure names the resource to fix."""

    declared_in: str
    """The ``.tf`` file the resource is declared in."""

    workspace_path: str
    """The absolute workspace path, e.g. ``/Shared/chip-chat/lib/publish.py``."""

    repo_path: Path
    """The absolute path of the ``source`` file in this checkout."""


@dataclass(frozen=True)
class Comparison:
    """What the workspace answered for one managed path."""

    managed: ManagedPath
    drifted: bool
    summary: str
    """One line, printed whether or not there is a diff to go with it."""

    diff: str = ""
    """A unified diff, deployed on the left and committed on the right. May be empty."""


# --- Reading the Terraform ---------------------------------------------------


def _terraform_sources(terraform_dir: Path) -> list[Path]:
    """The ``databricks_*.tf`` files, sorted, so output order is stable."""
    sources = sorted(terraform_dir.glob("databricks_*.tf"))
    if not sources:
        raise DriftCheckError(
            f"no databricks_*.tf files under {terraform_dir}. "
            "Pass --terraform-dir if the estate has moved."
        )
    return sources


def terraform_locals(terraform_dir: Path, base: str) -> dict[str, str]:
    """Resolve the ``locals`` this module needs: ``base`` and the lib paths.

    Only two RHS shapes are understood — a quoted string, which may interpolate
    another local, and a bare ``local.other`` alias. Everything else in a
    ``locals`` block (the ternaries, the tag maps, the ``substr`` calls) is
    ignored rather than mis-parsed, because nothing this module resolves is
    written that way. ``base`` is seeded from the caller instead of being
    derived; see ``check_base_assumption``.
    """
    raw: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for path in sorted(terraform_dir.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for block in _LOCALS_BLOCK.finditer(text):
            body = block.group("body")
            for match in _LOCAL_STRING.finditer(body):
                raw[match.group("key")] = match.group("value")
            for match in _LOCAL_ALIAS.finditer(body):
                aliases[match.group("key")] = match.group("target")

    resolved: dict[str, str] = {"base": base}

    def resolve(key: str, seen: frozenset[str]) -> str:
        if key in resolved:
            return resolved[key]
        if key in seen:
            raise DriftCheckError(f"local.{key} resolves in a cycle")
        if key in aliases:
            value = resolve(aliases[key], seen | {key})
        elif key in raw:
            value = _interpolate(raw[key], lambda name: resolve(name, seen | {key}))
        else:
            raise DriftCheckError(
                f"local.{key} is referenced by a managed resource but is not a "
                "plain string local this parser can resolve. Read the module "
                "docstring in workspace_drift.py before widening the parser."
            )
        resolved[key] = value
        return value

    for key in list(raw) + list(aliases):
        if key.endswith("_lib_path"):
            resolve(key, frozenset())
    return resolved


def _interpolate(value: str, lookup: Callable[[str], str]) -> str:
    """Substitute ``${local.x}`` and ``${path.module}`` and refuse anything else.

    ``${path.module}`` becomes a sentinel rather than a directory, because the
    caller knows which directory the module is and this function does not. Only
    ``source`` arguments contain it.
    """

    def replace(match: re.Match[str]) -> str:
        expr = match.group("expr").strip()
        if expr == "path.module":
            return PATH_MODULE_SENTINEL
        if expr.startswith("local."):
            return lookup(expr.removeprefix("local."))
        raise DriftCheckError(
            f'cannot evaluate "${{{expr}}}". Only ${{local.*}} and ${{path.module}} '
            "are understood; a managed resource now interpolates something else."
        )

    return _INTERPOLATION.sub(replace, value)


def managed_files(terraform_dir: Path, base: str = DEFAULT_BASE) -> list[ManagedPath]:
    """Every ``databricks_workspace_file`` and ``databricks_notebook`` in the source.

    Sorted with the library modules first and each kind by workspace path, so
    two runs a month apart produce output that can be diffed against each other.
    """
    terraform_dir = terraform_dir.resolve()
    scope = terraform_locals(terraform_dir, base)

    def lookup(key: str) -> str:
        if key not in scope:
            raise DriftCheckError(
                f"local.{key} is used by a managed resource and was not resolved. "
                "terraform_locals only resolves `base` and `*_lib_path`; read the "
                "module docstring before widening it."
            )
        return scope[key]

    found: list[ManagedPath] = []

    for source in _terraform_sources(terraform_dir):
        text = source.read_text(encoding="utf-8")
        for block in _BLOCK.finditer(text):
            kind = block.group("kind")
            name = block.group("name")
            body = block.group("body")
            address = f"{kind}.{name}"

            for meta in _UNEVALUABLE:
                if re.search(rf"^\s+{meta}\b", body, re.MULTILINE):
                    raise DriftCheckError(
                        f"{address} in {source.name} uses `{meta}`, which this "
                        "parser cannot evaluate. It manages files that would "
                        "silently go unchecked; teach the parser or move the "
                        "resource before relying on this target again."
                    )

            arguments = {
                m.group("key"): m.group("value") for m in _ARGUMENT.finditer(body)
            }
            for required in ("path", "source"):
                if required not in arguments:
                    raise DriftCheckError(
                        f"{address} in {source.name} has no literal `{required}` argument"
                    )

            workspace_path = _interpolate(arguments["path"], lookup)
            raw_source = _interpolate(arguments["source"], lookup)
            repo_path = Path(
                raw_source.replace(PATH_MODULE_SENTINEL, str(terraform_dir))
            ).resolve()

            found.append(
                ManagedPath(
                    kind=kind,
                    address=address,
                    declared_in=source.name,
                    workspace_path=workspace_path,
                    repo_path=repo_path,
                )
            )

    found.sort(key=lambda m: (_RESOURCE_KINDS.index(m.kind), m.workspace_path))
    return found


def check_counts(
    found: Sequence[ManagedPath],
    expect_workspace_files: int = EXPECTED_WORKSPACE_FILES,
    expect_notebooks: int = EXPECTED_NOTEBOOKS,
) -> None:
    """Fail loudly when the derived list is not the size Terraform is known to manage.

    The failure this guards is the quiet one: a parser that matches nothing
    reports no drift, and no drift is exactly what a healthy workspace reports.
    """
    counted = {
        "databricks_workspace_file": sum(
            1 for m in found if m.kind == "databricks_workspace_file"
        ),
        "databricks_notebook": sum(1 for m in found if m.kind == "databricks_notebook"),
    }
    expected = {
        "databricks_workspace_file": expect_workspace_files,
        "databricks_notebook": expect_notebooks,
    }
    if counted != expected:
        raise DriftCheckError(
            "the Terraform source does not declare the number of managed paths "
            "this check expects:\n"
            f"  databricks_workspace_file  found {counted['databricks_workspace_file']}, "
            f"expected {expect_workspace_files}\n"
            f"  databricks_notebook        found {counted['databricks_notebook']}, "
            f"expected {expect_notebooks}\n"
            "Either the estate gained or lost a resource -- in which case update "
            "EXPECTED_WORKSPACE_FILES / EXPECTED_NOTEBOOKS in workspace_drift.py "
            "and the count in docs/workspace-drift.md in the same commit -- or the "
            "HCL parser stopped recognising a block, which would have checked "
            "fewer files than you thought and reported a clean run."
        )


def check_base_assumption(terraform_dir: Path, base: str) -> str | None:
    """Confirm ``locals.tf`` still computes the default ``base`` it is assumed to.

    Returns a warning string when it does not, and ``None`` when the assumption
    holds. Not fatal: a stack other than the live one is a legitimate thing to
    check, and ``--base`` is how you say so. The warning exists so that a
    silently wrong prefix -- which would report every path missing -- reads as a
    configuration problem rather than as a catastrophic drift.
    """
    if base != DEFAULT_BASE:
        return None
    locals_tf = terraform_dir / "locals.tf"
    if not locals_tf.is_file():
        return f"{locals_tf} is gone; --base {base} is now an unverified assumption."
    text = locals_tf.read_text(encoding="utf-8")
    if 'var.environment == "demo"' in text and f'"{DEFAULT_BASE}"' in text:
        return None
    return (
        f'locals.tf no longer computes `base` as "{DEFAULT_BASE}" for the demo '
        "stack. Every path below is derived from that prefix; pass --base if the "
        "live stack has been renamed."
    )


# --- Reading the workspace ---------------------------------------------------


def export(
    workspace_path: str,
    databricks: str = "databricks",
    profile: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """The deployed content of one path, or ``None`` if the workspace has no such path.

    ``databricks workspace export`` writes the decoded source to stdout for both
    resource kinds. Notebooks come back in ``SOURCE`` format, which is what
    ``databricks_notebook`` uploads and therefore byte-for-byte what is in this
    repository -- including the ``# Databricks notebook source`` first line and
    the ``# COMMAND ----------`` separators, both of which are in the checked-in
    files. Verified against the live workspace on 2026-08-28: every one of the
    twenty-four exports compared byte-identical to its source file, with no
    normalisation applied.
    """
    command = [databricks, "workspace", "export", workspace_path]
    if profile:
        command += ["--profile", profile]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DriftCheckError(
            f"`{databricks}` is not on PATH. Install the Databricks CLI, or pass "
            "--databricks. https://docs.databricks.com/dev-tools/cli/"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DriftCheckError(
            f"`databricks workspace export {workspace_path}` timed out"
        ) from exc

    if completed.returncode == 0:
        return completed.stdout

    stderr = completed.stderr.strip()
    # A path Terraform declares and the workspace does not have is drift of the
    # most consequential kind -- the apply never ran -- so it is reported as a
    # finding rather than raised as a failure to perform the check.
    if (
        "RESOURCE_DOES_NOT_EXIST" in stderr
        or "doesn't exist" in stderr
        or "not found" in stderr
    ):
        return None
    raise DriftCheckError(
        f"`databricks workspace export {workspace_path}` failed with exit "
        f"{completed.returncode}:\n{stderr}\n"
        "If this is an authentication error, run `databricks auth login --host "
        "<workspace url>` or set DATABRICKS_CONFIG_PROFILE."
    )


def compare(
    managed: ManagedPath,
    deployed: str | None,
    repo_root: Path | None = None,
) -> Comparison:
    """Diff one deployed file against its committed source.

    Line endings are normalised on both sides before the comparison. The live
    export has never yet returned a ``\\r\\n`` -- the 2026-08-28 run was
    byte-identical -- but a workspace file that has been round-tripped through a
    Windows editor would otherwise report every line as drifted, which is a
    twenty-four-page diff hiding a one-line finding.
    """
    root = (repo_root or REPO_ROOT).resolve()
    try:
        here = managed.repo_path.relative_to(root).as_posix()
    except ValueError:
        here = str(managed.repo_path)

    if not managed.repo_path.is_file():
        return Comparison(
            managed=managed,
            drifted=True,
            summary=(
                f"{managed.workspace_path}: {managed.address} points at {here}, "
                "which is not in this checkout"
            ),
        )

    committed = managed.repo_path.read_text(encoding="utf-8")

    if deployed is None:
        return Comparison(
            managed=managed,
            drifted=True,
            summary=(
                f"{managed.workspace_path}: NOT DEPLOYED. "
                f"{managed.address} declares it; the workspace does not have it"
            ),
        )

    left = deployed.replace("\r\n", "\n").splitlines(keepends=True)
    right = committed.replace("\r\n", "\n").splitlines(keepends=True)
    if left == right:
        return Comparison(
            managed=managed, drifted=False, summary=f"{managed.workspace_path}: ok"
        )

    diff = "".join(
        difflib.unified_diff(
            left,
            right,
            fromfile=f"deployed {managed.workspace_path}",
            tofile=f"committed {here}",
            n=3,
        )
    )
    return Comparison(
        managed=managed,
        drifted=True,
        summary=(
            f"{managed.workspace_path}: DRIFTED from {here} "
            f"(deployed {len(left)} lines, committed {len(right)} lines)"
        ),
        diff=diff,
    )


# --- The command -------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.infra.workspace_drift",
        description=(
            "Diff the Terraform-managed Databricks lib modules and notebooks in "
            "the deployed workspace against this checkout. Read-only. Needs a "
            "Databricks credential and nothing else."
        ),
        epilog="Exit 0 clean, 1 drifted, 2 the check could not be run.",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("CHIP_CHAT_DATABRICKS_BASE", DEFAULT_BASE),
        help=(
            "The stack prefix `local.base` computes. Default %(default)r, which "
            "is the live demo stack; a disposable stack is chip-chat-<environment>."
        ),
    )
    parser.add_argument(
        "--terraform-dir",
        type=Path,
        default=REPO_ROOT / "infra" / "terraform",
        help="Where the databricks_*.tf files are (default: %(default)s).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help=(
            "The checkout the deployed files are compared against (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--databricks",
        default=os.environ.get("DATABRICKS_CLI", "databricks"),
        help="The Databricks CLI binary (default: %(default)s).",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("DATABRICKS_CONFIG_PROFILE"),
        help=(
            "Databricks CLI profile. Defaults to $DATABRICKS_CONFIG_PROFILE, "
            "then the CLI's own default."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "Print the managed paths derived from the Terraform and exit, "
            "touching no workspace."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print nothing at all on a clean run.",
    )
    parser.add_argument(
        "--expect-workspace-files",
        type=int,
        default=EXPECTED_WORKSPACE_FILES,
        help="Tripwire on the derived count (default: %(default)s).",
    )
    parser.add_argument(
        "--expect-notebooks",
        type=int,
        default=EXPECTED_NOTEBOOKS,
        help="Tripwire on the derived count (default: %(default)s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check. Quiet and zero when the workspace matches the repository."""
    args = _parser().parse_args(argv)
    out = sys.stdout

    try:
        found = managed_files(args.terraform_dir, args.base)
        check_counts(found, args.expect_workspace_files, args.expect_notebooks)
    except DriftCheckError as exc:
        print(f"cannot check for drift: {exc}", file=sys.stderr)
        return EXIT_ERROR

    warning = check_base_assumption(args.terraform_dir, args.base)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    if args.list:
        for managed in found:
            try:
                here = managed.repo_path.relative_to(
                    Path(args.repo_root).resolve()
                ).as_posix()
            except ValueError:
                here = str(managed.repo_path)
            print(f"{managed.workspace_path}\t{here}\t{managed.address}", file=out)
        return EXIT_CLEAN

    comparisons: list[Comparison] = []
    for managed in found:
        try:
            deployed = export(managed.workspace_path, args.databricks, args.profile)
        except DriftCheckError as exc:
            print(f"cannot check for drift: {exc}", file=sys.stderr)
            return EXIT_ERROR
        comparisons.append(compare(managed, deployed, args.repo_root))

    drifted = [c for c in comparisons if c.drifted]
    if not drifted:
        if not args.quiet:
            print(
                f"{len(comparisons)} Terraform-managed paths in the Databricks "
                f"workspace match this checkout.",
                file=out,
            )
        return EXIT_CLEAN

    for comparison in drifted:
        print(comparison.summary, file=out)
        if comparison.diff:
            print(file=out)
            print(
                comparison.diff,
                end="" if comparison.diff.endswith("\n") else "\n",
                file=out,
            )
            print(file=out)

    print(
        f"\n{len(drifted)} of {len(comparisons)} managed paths differ from this "
        "checkout. The workspace is running code this repository does not "
        "describe.\n"
        "Repair with an apply, which is what put them there in the first place:\n"
        "  make infra-apply\n"
        "or, for one path and without Terraform state:\n"
        "  databricks workspace import --overwrite --format SOURCE "
        "--language PYTHON \\\n"
        f"    --file <repo file> {drifted[0].managed.workspace_path}",
        file=out,
    )
    return EXIT_DRIFTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
