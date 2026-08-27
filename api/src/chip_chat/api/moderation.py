"""Content Safety on inbound visitor text, and the prompt shield beside it.

Issue #79. The image lane has moderated its input since #52; text has not, and
the asymmetry was never deliberate -- an unauthenticated public LLM endpoint
attached to somebody's own Azure subscription is the more exposed of the two
surfaces, not the less.

**Ordering is the whole design.** Moderation runs before the model, never after,
and the way that is enforced here is the way :mod:`chip_chat.api.turns` enforces
the spend cap: the moderator is private to
:class:`~chip_chat.api.turns.SpendGate`, and the only way to obtain a
:class:`~chip_chat.api.turns.FundedTurn` -- the only object in the process that
can call a model -- is through a code path that has already moderated. A route
added next year cannot reorder the check, because there is nothing to reorder;
there is no second door.

**Two failures, deliberately different.** A message Content Safety *flags* gets
a neutral reply and the conversation continues, because the visitor may simply
have phrased something badly and a demo that ends the session over one sentence
is worse than one that declines it. A moderation service that is *unreachable*
closes the door: :class:`ModerationUnavailableError` refuses the turn and no
model is called. That asymmetry is #79's, and it copies
:mod:`chip_chat.vision.moderation`, which made the same choice for images.

**Why the default analyzer is local.** ``make ci`` runs free and offline, and a
moderation check that only exists when a credential does is a check that is
absent exactly where it is easiest to forget -- a developer's laptop, and then a
deployment whose configuration silently lost its endpoint.
:class:`LocalTextAnalyzer` is not a substitute for Content Safety and does not
pretend to be: it recognises the published jailbreak shapes and nothing else. It
exists so that the *plumbing* -- the span, the attributes, the fail-closed path
-- is exercised on every run, and so that a deployment without an endpoint
degrades to a weak check rather than to no check.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from chip_chat.otel import content_safety

__all__ = [
    "BLOCKED_MESSAGE",
    "AzureTextAnalyzer",
    "LocalTextAnalyzer",
    "ModerationUnavailableError",
    "TextAnalyzer",
    "TextModerator",
    "TextVerdict",
]

_ENDPOINT_VARIABLE: Final = "AZURE_CONTENT_SAFETY_ENDPOINT"
"""Same variable the image lane reads. One resource, two callers."""

_SHIELD_API_VERSION: Final = "2024-09-01"
_SHIELD_TIMEOUT_SECONDS: Final = 5.0

BLOCKED_MESSAGE: Final = (
    "I can't help with that one — ask me something about the menu, "
    "your account or an order and we'll carry on."
)
"""What a flagged message gets back.

Neutral on purpose, and #79 says so: *a blocked message gets a neutral response
and the conversation continues*. It names no category and no rule, because a
refusal that explains precisely which word tripped it is a refusal that teaches
an attacker what to write instead.
"""


class ModerationUnavailableError(Exception):
    """Content Safety could not be reached, or answered something unusable.

    Deliberately not a subclass of anything the request handler's broad
    ``except Exception`` catches by accident -- see
    :meth:`TextModerator.screen`, which is where the distinction is kept.
    """


@dataclass(frozen=True, slots=True)
class TextVerdict:
    """What the screen found.

    Attributes:
        categories: Content Safety categories above threshold, as strings.
        shield_detections: What the prompt shield flagged. ``user_prompt`` for
            the visitor's own message, ``document`` for a retrieved passage --
            the cross-prompt half of the API, which is the half that matters
            for #81's planted-corpus attack.
    """

    categories: tuple[str, ...] = ()
    shield_detections: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether this turn should be declined.

        A shield detection alone does not block. The shield fires on text that
        *attempts* an injection, and an attempt that the structural gates
        already defeat is worth recording rather than refusing -- refusing it
        would turn every visitor who typed the word "ignore" into a stopped
        conversation. What blocks is a Content Safety category.
        """
        return bool(self.categories)


class TextAnalyzer(Protocol):
    """Screens one string. Implemented locally and against Azure."""

    def analyze(self, text: str, *, subject: str) -> TextVerdict:
        """Return what was found in ``text``.

        Args:
            text: The string to screen.
            subject: ``user_prompt`` or ``document``.

        Raises:
            ModerationUnavailableError: If the service could not be reached, or
                answered in a shape this code cannot read. Both are outages;
                neither is a clean bill of health.
        """
        ...


_JAILBREAK_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"ignore (all|any|your|the) (previous|prior|above|earlier)", "instruction_override"),
    (
        r"disregard (all|any|your|the) (previous|prior|above|earlier)",
        "instruction_override",
    ),
    (r"\byou are now\b", "persona_override"),
    (r"\bdeveloper mode\b", "persona_override"),
    (r"\bmaintenance mode\b", "persona_override"),
    (r"\bDAN\b", "persona_override"),
    (r"print your (system )?prompt", "prompt_exfiltration"),
    (r"reveal your (system )?(prompt|instructions)", "prompt_exfiltration"),
    (r"repeat (the|your) (system )?(prompt|instructions)", "prompt_exfiltration"),
    (r"\bno restrictions\b", "guardrail_removal"),
    (r"\bwithout asking\b", "confirmation_bypass"),
    (r"\bpre-authorised\b|\bpre-authorized\b", "confirmation_bypass"),
)
"""Shapes a published jailbreak takes, and what to call each.

Not a security boundary and not presented as one. Content Safety's own shield is
the real check; this is what a deployment without an endpoint still gets, and
what CI exercises for free. Every entry here is a phrasing that appears in this
repository's own adversarial suite, which is the only claim being made for it.
"""


@dataclass(frozen=True, slots=True)
class LocalTextAnalyzer:
    """The offline analyzer. Recognises jailbreak shapes; flags no categories.

    It deliberately returns no Content Safety categories, ever. Inventing a
    ``hate`` or ``violence`` verdict from a regular expression would produce
    exactly the false confidence #79 is written against -- and, worse, would
    block real visitors on a keyword. What it does produce is shield detections,
    which are recorded rather than acted on.
    """

    patterns: Sequence[tuple[str, str]] = field(default=_JAILBREAK_PATTERNS)

    def analyze(self, text: str, *, subject: str) -> TextVerdict:
        """Return the shield detections in ``text``. Never raises."""
        found: list[str] = []
        for pattern, label in self.patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                tag = f"{subject}:{label}"
                if tag not in found:
                    found.append(tag)
        return TextVerdict(categories=(), shield_detections=tuple(found))


@dataclass(frozen=True, slots=True)
class TextModerator:
    """The gate. Opens ``guard.content_safety`` and decides the turn's fate.

    Held privately by :class:`~chip_chat.api.turns.SpendGate`. There is no
    public accessor, for the reason there is no public accessor for the model:
    a second route to the moderator is a second route around it.
    """

    analyzer: TextAnalyzer = field(default_factory=LocalTextAnalyzer)

    def screen(self, text: str, *, subject: str = "user_prompt") -> TextVerdict:
        """Screen one inbound string, recording what was found on the span.

        Args:
            text: The visitor's message, or a retrieved passage.
            subject: ``user_prompt`` or ``document``.

        Returns:
            The verdict. :attr:`TextVerdict.blocked` says whether to decline.

        Raises:
            ModerationUnavailableError: If the analyzer could not answer. The
                caller must fail closed; this method will not decide that for
                it, because "the check did not run" and "the check passed" have
                to be different objects or they become the same behaviour.
        """
        with content_safety(subject="text") as guard:
            verdict = self.analyzer.analyze(text, subject=subject)
            guard.record_shield(verdict.shield_detections)
            if verdict.blocked:
                guard.block("content_blocked", categories=verdict.categories)
            else:
                guard.allow()
            return verdict


class AzureTextAnalyzer:
    """Content Safety over the wire: ``text:analyze`` and ``text:shieldPrompt``.

    Two calls, because they are two endpoints answering two questions.
    ``text:analyze`` returns a severity per harm category and is what blocks a
    turn. ``text:shieldPrompt`` returns whether a jailbreak was detected in the
    ``userPrompt`` and, separately, in each supplied ``document`` -- the
    cross-prompt half, which is the half #81 plants its payloads for.

    Authentication is the app's managed identity, exactly as
    :class:`chip_chat.vision.moderation.AzureImageAnalyzer` does it. There is no
    key to configure here and nothing that could authenticate with one.
    """

    __slots__ = ("_client", "_credential", "_endpoint", "_threshold")

    def __init__(
        self,
        client: object,
        credential: object,
        endpoint: str,
        *,
        threshold: int = 2,
    ) -> None:
        """Wrap a Content Safety client and the credential the shield call needs.

        Args:
            client: A ``ContentSafetyClient`` pointed at ``endpoint``.
            credential: The credential to mint a bearer token from, for the
                shield endpoint the SDK does not cover.
            endpoint: The Content Safety resource endpoint.
            threshold: Severity at or above which a category counts. Content
                Safety reports 0/2/4/6; 2 is the first non-zero rung.
        """
        self._client = client
        self._credential = credential
        self._endpoint = endpoint.rstrip("/")
        self._threshold = threshold

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureTextAnalyzer":
        """Build from ``AZURE_CONTENT_SAFETY_ENDPOINT``.

        Raises:
            RuntimeError: If the variable is missing. Failing at startup rather
                than on the first turn, for the reason the image lane gives: an
                unconfigured moderator fails closed on every request, which is
                indistinguishable from an outage and takes a day to tell apart.
        """
        from azure.ai.contentsafety import ContentSafetyClient
        from azure.identity import DefaultAzureCredential

        source = os.environ if env is None else env
        endpoint = source.get(_ENDPOINT_VARIABLE, "").strip()
        if not endpoint:
            raise RuntimeError(f"content safety is not configured: {_ENDPOINT_VARIABLE}")
        credential = DefaultAzureCredential()
        return cls(
            ContentSafetyClient(endpoint=endpoint, credential=credential),
            credential,
            endpoint,
        )

    def analyze(self, text: str, *, subject: str) -> TextVerdict:
        """Screen ``text`` against both endpoints.

        Raises:
            ModerationUnavailableError: On any transport failure, and on a
                response missing the fields this reads. A partial answer is an
                outage rather than a pass -- the alternative is reading a
                truncated response as a clean bill of health.
        """
        return TextVerdict(
            categories=self._categories(text),
            shield_detections=self._shield(text, subject=subject),
        )

    def _categories(self, text: str) -> tuple[str, ...]:
        """Categories at or above the threshold, from ``text:analyze``."""
        from azure.ai.contentsafety.models import AnalyzeTextOptions
        from azure.core.exceptions import AzureError

        client: Any = self._client
        try:
            result = client.analyze_text(AnalyzeTextOptions(text=text))
        except AzureError as error:
            raise ModerationUnavailableError(str(error)) from error

        flagged: list[str] = []
        for analysis in result.categories_analysis or ():
            severity = analysis.severity
            if severity is None:
                # A category reported with no severity is not a zero. It is an
                # answer with a hole in it, and this does not fill holes in.
                raise ModerationUnavailableError(
                    f"content safety returned no severity for {analysis.category}"
                )
            if severity >= self._threshold:
                flagged.append(str(analysis.category))
        return tuple(flagged)

    def _shield(self, text: str, *, subject: str) -> tuple[str, ...]:
        """Detections from ``text:shieldPrompt``.

        The visitor's own message goes in ``userPrompt``; a retrieved passage
        goes in ``documents``, which is the field that answers the question #81
        actually asks -- did an instruction arrive inside content we fetched.
        """
        import httpx

        body: dict[str, object] = (
            {"userPrompt": text, "documents": []}
            if subject == "user_prompt"
            else {"userPrompt": "", "documents": [text]}
        )
        try:
            credential: Any = self._credential
            token = credential.get_token("https://cognitiveservices.azure.com/.default")
            response = httpx.post(
                f"{self._endpoint}/contentsafety/text:shieldPrompt",
                params={"api-version": _SHIELD_API_VERSION},
                headers={"Authorization": f"Bearer {token.token}"},
                json=body,
                timeout=_SHIELD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        # Broad on purpose: a transport error, a non-2xx, and a body that is
        # not JSON are the same fact here -- the check did not run.
        except Exception as error:
            raise ModerationUnavailableError(str(error)) from error

        found: list[str] = []
        user = payload.get("userPromptAnalysis") or {}
        if user.get("attackDetected"):
            found.append("user_prompt:attack_detected")
        for index, document in enumerate(payload.get("documentsAnalysis") or ()):
            if document.get("attackDetected"):
                found.append(f"document:{index}:attack_detected")
        return tuple(found)
