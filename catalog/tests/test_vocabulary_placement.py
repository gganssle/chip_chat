"""That the generated vocabulary lands somewhere the interpreter will find it.

`test_catalog_vocabulary.py` next door proves the *content* of
`build/vision_vocabulary.py`: that every term traces back to a published row and
that nothing in the pipeline holds a second, hand-maintained copy. This file
proves the far duller thing that broke the deployment anyway — that the module,
once generated, is installed where `import chip_chat.vision_vocabulary` can
reach it, and that a build which gets that wrong fails instead of shipping.

The history is worth keeping because the failure mode is the interesting part.
The Dockerfile used to name the destination directly, interpolating the
`PYTHON_VERSION` build argument into
`/app/.venv/lib/python${PYTHON_VERSION}/site-packages/`. That is right for
`3.13` and wrong for `3.13.15`, because `PYTHON_VERSION` is a base-image tag and
a virtualenv's library directory is always `pythonX.Y` — never the patch level.
COPY does not object to a destination that does not exist; it creates it. So the
image built clean, pushed clean, deployed clean, and then withdrew the photo
lane at start-up because `Vocabulary.from_env()` could not import a module
sitting in a directory that was on no `sys.path`. The visitor's symptom was the
agent politely reporting a tool it had not been offered.

Nothing here builds a container — that needs a daemon and belongs in CI. What it
checks is that the Dockerfile still asks the interpreter where its own
site-packages is rather than spelling the answer, and that it still asserts the
import at build time. Both are one-line edits away from being lost by somebody
tidying a RUN back into a COPY, and the whole cost of losing them is paid weeks
later by a tester with a photograph.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
MAKEFILE = REPO_ROOT / "Makefile"
VARIABLES = REPO_ROOT / "infra" / "terraform" / "variables.tf"

# The dotted name the deployment sets CHIP_CHAT_VISION_VOCABULARY to, and
# therefore the name the build has to be able to import.
MODULE = "chip_chat.vision_vocabulary"


def _runtime_stage() -> str:
    """The runtime stage's instructions, with its comments removed.

    Two narrowings, each for its own reason. The stage, because the build stage
    installs the workspace and has its own interpreter: an assertion about the
    image that ships must not be satisfiable by a line in the half of the file
    that is thrown away. The comments, because the Dockerfile explains the bug
    these tests exist to prevent, and it explains it by quoting the wrong path
    verbatim -- so a test that grepped the raw text would be tripped by the
    prose describing the very thing it is checking has gone. Deleting that
    explanation to make a grep simpler would be the wrong trade in a repository
    whose comments are the argument.
    """
    text = DOCKERFILE.read_text()
    match = re.search(r"^FROM [^\n]* AS runtime$", text, re.MULTILINE)
    assert match is not None, "the Dockerfile has no stage named `runtime`"
    lines = text[match.start() :].splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_the_destination_is_discovered_from_the_interpreter() -> None:
    # `sysconfig.get_paths()["purelib"]` is the interpreter's own answer to the
    # question, evaluated by the same `python` that will later do the import, so
    # it cannot disagree with it. Any other way of arriving at the directory is
    # a second source of truth for a string only one program actually consumes.
    assert "sysconfig.get_paths()" in _runtime_stage()
    assert "purelib" in _runtime_stage()


def test_no_site_packages_path_is_spelled_out_of_the_base_image_tag() -> None:
    # This is the #110 regression, stated as narrowly as it can be: the tag and
    # the library directory are different strings that happen to match for
    # `X.Y`, and interpolating one into the other is a bug that only shows up
    # when somebody pins the patch level for the entirely good reason that they
    # want a reproducible base.
    stage = _runtime_stage()
    assert "python${PYTHON_VERSION}" not in stage
    assert "python$PYTHON_VERSION" not in stage
    for line in stage.splitlines():
        if "site-packages" in line and "PYTHON_VERSION" in line:
            raise AssertionError(f"site-packages path derived from the tag: {line}")


def test_the_build_fails_if_the_vocabulary_does_not_import() -> None:
    # The reason the placement bug survived a deploy is that nothing between
    # generating the file and a visitor uploading a photograph ever asserted the
    # module was importable. A RUN that imports it is that assertion, and it has
    # to be in the runtime stage: importing it in the build stage would prove
    # something about an interpreter that does not ship.
    stage = _runtime_stage()
    assert re.search(rf"RUN[^\n]*import {re.escape(MODULE)}", stage) or re.search(
        rf"import {re.escape(MODULE)}", stage
    ), "the runtime stage does not import the vocabulary at build time"


def test_the_asserted_module_is_the_one_the_deployment_asks_for() -> None:
    # Two places type this dotted name: the Dockerfile's build-time import and
    # the Terraform variable that becomes CHIP_CHAT_VISION_VOCABULARY on the
    # container. A build that proves the wrong name importable proves nothing.
    assert MODULE in _runtime_stage()
    assert re.search(rf'default\s+=\s+"{re.escape(MODULE)}"', VARIABLES.read_text())


def test_the_image_target_generates_the_vocabulary_first() -> None:
    # `make image` depends on `vocabulary` so the generation is not a step
    # anybody has to remember. With the build-time import in place, forgetting
    # it is now a failed build rather than a withdrawn lane -- but the
    # dependency is what makes the ordinary path work without thinking about it.
    assert re.search(r"^image: vocabulary\b", MAKEFILE.read_text(), re.MULTILINE)
