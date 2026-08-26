"""The system prompt, and the version that follows it into every trace.

Issue #60 asks for a prompt "versioned in the repo and referenced by version in
traces, so an Arize experiment can attribute a score change to a specific
prompt." The trap in that sentence is the word *specific*. A hand-maintained
version constant answers it only while everyone remembers to bump it, and the
run that matters -- the one where a score moved -- is exactly the run where
someone edited the text and did not.

So the version is two halves and only one of them is maintained by a person:

``v1``
    The revision. Bump it when the change is deliberate and you want the
    experiment to say so.

``+3f2a1b9c8d7e``
    The first twelve hex digits of the SHA-256 of the prompt bytes. Nobody
    maintains this and nobody can forget it. Two runs whose spans carry the same
    version ran the same bytes, and two that differ did not, whatever the
    revision says.

:attr:`SystemPrompt.version` is what
:func:`chip_chat.otel.chat_turn` records as ``chip_chat.prompt.version``.

**What the prompt is not.** Neither launch gate depends on a word of it.
Visitor isolation is enforced by row access policies below the model and by the
absence of any visitor identifier from all eleven tool signatures
(:mod:`chip_chat.agent.tools`); confirmation before writes is enforced by the
ops API. Delete this file's contents and Cilantro becomes useless, but nothing
leaks and nothing is written without a confirmation. ``test_sabotage.py``
asserts exactly that, by running the attacks against a prompt written to help
them. When you find yourself adding a sentence here that would be dangerous to
remove, the mechanism belongs somewhere else.
"""

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = ["PROMPT_DIR", "REVISION", "SystemPrompt", "load"]

PROMPT_DIR = Path(__file__).parent / "prompts"
"""Where prompt revisions live. One file per revision, kept, never edited in place
once it has been used for an experiment anyone intends to cite."""

REVISION = "v1"
"""The current revision. ``PROMPT_DIR / f"system-{REVISION}.md"`` is the text."""

_DIGEST_CHARS = 12
"""Twelve hex digits. Collision odds across the few dozen prompts this project
will ever have are not worth the width in a trace viewer."""


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    """One revision of the system prompt, and the version that identifies it."""

    revision: str
    """The maintained half, e.g. ``v1``."""

    text: str
    """The prompt as the model receives it."""

    digest: str
    """The unmaintained half: SHA-256 of :attr:`text`, first twelve hex digits."""

    @property
    def version(self) -> str:
        """The value a span carries, e.g. ``v1+3f2a1b9c8d7e``.

        Changes when the text changes, whether or not the revision was bumped,
        which is the whole point of it having two halves.
        """
        return f"{self.revision}+{self.digest}"


@cache
def load(revision: str = REVISION, *, directory: Path = PROMPT_DIR) -> SystemPrompt:
    """Load a prompt revision.

    Args:
        revision: Which revision to read. Defaults to :data:`REVISION`, the one
            the agent ships with; an eval experiment comparing two prompts names
            the other one here.
        directory: Where to read it from. Defaults to :data:`PROMPT_DIR`. The
            sabotage test points this at a fixture directory, so that the prompt
            written to help an attacker is exercised through this exact loader
            without shipping inside the package.

    Returns:
        The revision, with the version that will appear on every ``chat.turn``
        span the agent emits under it.

    Raises:
        FileNotFoundError: If no such revision exists in ``directory``.
    """
    text = (directory / f"system-{revision}.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return SystemPrompt(revision=revision, text=text, digest=digest)
