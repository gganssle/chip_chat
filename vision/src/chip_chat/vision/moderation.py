"""Stage 3. Content Safety, and the ordering that is the whole point of it.

RFC-001 section 07 puts moderation at position three of six, and is explicit
that the position is the design rather than the checklist item: **moderation
happens before inference so that nothing unmoderated reaches a model.** An
implementation that can call the vision model before Content Safety has
answered is wrong however well its happy path behaves, because the happy path is
not what a stranger uploading to a public endpoint is exercising.

So the ordering is not written down here as a convention. It is arranged so that
getting it wrong is difficult:

.. code-block:: text

    validate  ──▶  normalize  ──▶  screen  ──▶  put  ──▶  blob_ref
                                      |           |
    screen() takes a NormalizedImage -+           |
    and nothing else, so stage 3 can              |
    never precede stage 2                         |
                                                  |
    the write is the statement after screen(), so a refused photograph is
    never stored -- and stage 4 cannot be handed a reference to one

:meth:`ImageModerator.screen` takes a
:class:`~chip_chat.vision.normalize.NormalizedImage` and nothing else, so it
cannot run before stage 2. :class:`~chip_chat.vision.intake.PhotoIntake`
requires a moderator to be constructed at all and calls
:meth:`~ImageModerator.screen` before the write, so a refused image is never
stored -- and stage 4 (issue #53) is handed a ``blob_ref``, which by then can
only refer to a photograph Content Safety has already passed.

Refusal
-------

Both refusals a visitor can reach here -- flagged, and *the service could not be
reached* -- produce the same neutral line, and the line is in
:mod:`chip_chat.vision.validate` with the rest of the visitor copy. Neutral
means it does not name a category, does not moralise, and gives an uploader
nothing to iterate against. It is deliberately the *same* sentence for both
outcomes: an outage that announced itself would tell the previous uploader that
their photograph, specifically, was the one that got flagged.

The two are still distinguishable where distinguishing them is useful. They
carry different :class:`~chip_chat.vision.validate.RejectionReason` values, and
``guard.content_safety`` records which one happened, along with the categories
and severities the service returned. The operator can tell them apart in a
trace; the uploader cannot tell them apart at all.

Fail closed
-----------

An unreachable Content Safety means **no vision lane**, not an unmoderated one.
Every path out of :meth:`~ImageModerator.screen` that is not "the service
answered, and its answer was below every threshold" raises: a transport error, a
timeout, a response missing a category, a severity the service declined to fill
in. RFC-001 section 10 allows a lane to fail and forbids the conversation
failing with it -- the visitor is asked to describe their meal in words, which
is the same thing that happens when the vision model itself is down.

Thresholds
----------

Image analysis returns one severity per category on a four-level scale --
``0``, ``2``, ``4``, ``6`` -- and a threshold here is the lowest severity that
refuses. The numbers are chosen for *this* endpoint, where the expected upload
is a photograph of a burrito bowl:

=============== ========= =====================================================
Category        Block at  Why this number
=============== ========= =====================================================
``Sexual``      2         No photograph of a meal is legitimately sexual, so
                          there is no false positive to trade against. A public,
                          unauthenticated endpoint attached to a restaurant
                          brand is the wrong place to be relaxed about this one.
``SelfHarm``    2         Same reasoning, and the cost of being wrong the other
                          way is higher than a refused photograph.
``Hate``        2         Symbols and text in a frame. Nothing about an
                          orderable meal requires tolerating any of it.
``Violence``    4         **The deliberate exception.** Knives, cleavers, raw
                          meat and cutting boards are ordinary food photography
                          and are exactly what this classifier's low band
                          reports. Blocking at 2 would refuse real meals; 4 is
                          depiction rather than implement.
unknown         2         A category this module has not heard of is a category
                          nobody chose a threshold for, and an unchosen
                          threshold should not default to permissive.
=============== ========= =====================================================

The asymmetry is the point: a false positive costs one visitor one photograph
and a sentence asking them to type what they wanted, while a false negative puts
a stranger's image in front of a model, in a trace, and in a demo. Every
threshold is overridable per category from the environment -- see
:meth:`ModerationThresholds.from_env` -- and none of them may be set to a value
that disables a category, because "disabled" is not a severity.

.. code-block:: python

    moderator = ImageModerator(analyzer=AzureImageAnalyzer.from_env())
    intake = PhotoIntake(store=AzureBlobStore.from_env(), moderator=moderator)

    with chat_turn(session_id=sid, turn_index=n, message=text):
        try:
            photo = intake.accept(payload, declared_media_type=content_type)
        except UploadRejectedError as refusal:
            return upload_error(refusal.message)

``guard.content_safety`` is a child of ``chat.turn``, so
:meth:`~chip_chat.vision.intake.PhotoIntake.accept` is called inside one. That
is not incidental: the guard belongs to the turn it is protecting, next to the
spend cap, in front of ``agent.step``.
"""

import enum
import os
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from PIL import Image

from chip_chat.otel import content_safety
from chip_chat.vision.normalize import NormalizedImage
from chip_chat.vision.validate import RejectionReason, rejection

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from azure.ai.contentsafety import ContentSafetyClient

__all__ = [
    "ENDPOINT_VARIABLE",
    "SERVICE_MAX_BYTES",
    "SERVICE_MAX_EDGE",
    "SERVICE_MIN_EDGE",
    "SEVERITY_LEVELS",
    "AzureImageAnalyzer",
    "ImageAnalyzer",
    "ImageModerator",
    "ModerationThresholds",
    "ModerationUnavailableError",
    "ModerationVerdict",
    "SafetyCategory",
]

ENDPOINT_VARIABLE = "AZURE_CONTENT_SAFETY_ENDPOINT"
"""The Content Safety endpoint. Set on the Container App by ``infra/terraform``."""

SEVERITY_LEVELS: Final = (0, 2, 4, 6)
"""The four severities image analysis reports. There is no eight-level output for images.

Text analysis offers a finer scale; image analysis does not, so a threshold
outside this set is a misconfiguration rather than a stricter setting.
"""

_BLOCKABLE_SEVERITIES: Final = frozenset(SEVERITY_LEVELS[1:])
"""Severities a threshold may be set to. ``0`` is "safe" and would block everything."""


class SafetyCategory(enum.StrEnum):
    """The four categories image analysis reports.

    The values are the service's own spelling rather than this repository's
    snake_case, because they arrive on the wire that way and a translation layer
    between here and Azure would be one more place for a category to go missing.
    """

    HATE = "Hate"
    SELF_HARM = "SelfHarm"
    SEXUAL = "Sexual"
    VIOLENCE = "Violence"


_THRESHOLD_FIELDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        SafetyCategory.HATE.value: "hate",
        SafetyCategory.SELF_HARM.value: "self_harm",
        SafetyCategory.SEXUAL.value: "sexual",
        SafetyCategory.VIOLENCE.value: "violence",
    }
)

_ENV_PREFIX: Final = "CHIP_CHAT_MODERATION_"


class ModerationUnavailableError(Exception):
    """Content Safety did not answer, or answered something unusable.

    Internal to stage 3: it never reaches a visitor, because
    :meth:`ImageModerator.screen` turns it into the same neutral refusal a
    flagged image gets. It exists so that "the service is down" and "the service
    said no" stay distinguishable in a trace and in
    :class:`~chip_chat.vision.validate.RejectionReason`.
    """


@dataclass(frozen=True, slots=True)
class ModerationThresholds:
    """The lowest severity that refuses, per category. See the module docstring.

    Attributes:
        hate: Ceiling for ``Hate``.
        self_harm: Ceiling for ``SelfHarm``.
        sexual: Ceiling for ``Sexual``.
        violence: Ceiling for ``Violence`` -- the one deliberately looser number.
        unknown: Applied to any category the service reports that this module
            does not know about.
    """

    hate: int = 2
    self_harm: int = 2
    sexual: int = 2
    violence: int = 4
    unknown: int = 2

    def __post_init__(self) -> None:
        """Refuse a threshold that is not a severity this service can report.

        Raises:
            ValueError: If any threshold is outside ``{2, 4, 6}``. ``0`` would
                refuse every photograph, and there is deliberately no value
                meaning "never refuse": a category nobody wants enforced is a
                decision to make in the open, not by setting an environment
                variable to 8.
        """
        for name in ("hate", "self_harm", "sexual", "violence", "unknown"):
            value = getattr(self, name)
            if value not in _BLOCKABLE_SEVERITIES:
                allowed = ", ".join(str(level) for level in sorted(_BLOCKABLE_SEVERITIES))
                raise ValueError(f"{name} must be one of {allowed}, got {value}")

    def block_at(self, category: str) -> int:
        """Return the lowest severity that refuses ``category``.

        Args:
            category: A category name as Content Safety spells it.

        Returns:
            The threshold, or :attr:`unknown` for a category added to the
            service since this module was written.
        """
        field = _THRESHOLD_FIELDS.get(category)
        if field is None:
            return self.unknown
        threshold: int = getattr(self, field)
        return threshold

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModerationThresholds":
        """Build thresholds from the environment.

        Reads ``CHIP_CHAT_MODERATION_HATE_BLOCK_AT`` and its ``SELF_HARM``,
        ``SEXUAL``, ``VIOLENCE`` and ``UNKNOWN`` siblings. Every one is optional.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configured thresholds.

        Raises:
            ValueError: If a value is unparseable or is not a severity this
                service reports.
        """
        source = os.environ if env is None else env
        defaults = cls()
        return cls(
            hate=_severity(source, "HATE", defaults.hate),
            self_harm=_severity(source, "SELF_HARM", defaults.self_harm),
            sexual=_severity(source, "SEXUAL", defaults.sexual),
            violence=_severity(source, "VIOLENCE", defaults.violence),
            unknown=_severity(source, "UNKNOWN", defaults.unknown),
        )


def _severity(env: Mapping[str, str], category: str, default: int) -> int:
    """Read one ``CHIP_CHAT_MODERATION_<CATEGORY>_BLOCK_AT`` value."""
    raw = env.get(f"{_ENV_PREFIX}{category}_BLOCK_AT", "").strip()
    if not raw:
        return default
    return int(raw)


@dataclass(frozen=True, slots=True)
class ModerationVerdict:
    """What Content Safety said about one image, and what we did with it.

    Attributes:
        severities: Every category the service reported, and its severity.
            Recorded on the span so a threshold can be retuned against real
            traffic rather than against an intuition.
        flagged: The categories at or above their threshold, sorted. Empty on
            an allowed image, which is the only kind
            :meth:`ImageModerator.screen` returns.
    """

    severities: Mapping[str, int]
    flagged: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        """True when nothing reached its threshold."""
        return not self.flagged


class ImageAnalyzer(Protocol):
    """The one call stage 3 makes to Content Safety."""

    def analyze(self, image: bytes) -> Mapping[str, int]:
        """Return a severity for every category, keyed by the service's names.

        Args:
            image: The normalized JPEG bytes -- the same bytes that would be
                stored, so what is screened is what is kept.

        Returns:
            Category name to severity, one entry per category the service
            reports.

        Raises:
            ModerationUnavailableError: If the service could not be reached or
                did not return something usable. Stage 3 fails closed, so an
                implementation must raise rather than return an optimistic
                verdict.
        """
        ...


class ImageModerator:
    """Stage 3. One instance per process is enough."""

    __slots__ = ("_analyzer", "_thresholds")

    def __init__(
        self,
        analyzer: ImageAnalyzer,
        *,
        thresholds: ModerationThresholds | None = None,
    ) -> None:
        """Assemble the moderator.

        Args:
            analyzer: The Content Safety client. There is no default: a
                moderator that would silently do nothing is the failure this
                module exists to prevent.
            thresholds: The severities that refuse. Defaults to
                :class:`ModerationThresholds`.
        """
        self._analyzer = analyzer
        self._thresholds = (
            thresholds if thresholds is not None else ModerationThresholds()
        )

    @property
    def thresholds(self) -> ModerationThresholds:
        """The thresholds this moderator enforces, for an ops surface reporting them."""
        return self._thresholds

    def screen(self, photo: NormalizedImage) -> ModerationVerdict:
        """Screen one normalized photo, and refuse if anything reached a threshold.

        Emits ``guard.content_safety``, so this runs inside a ``chat.turn``.

        Args:
            photo: The output of :func:`~chip_chat.vision.normalize.normalize`.
                Only a normalized image may be screened, which is what keeps
                stage 3 behind stage 2 -- and it is the stored bytes that are
                sent, so the screened image and the kept image are the same one.

        Returns:
            The :class:`ModerationVerdict`, always an allowed one.

        Raises:
            UploadRejectedError: If a category reached its threshold, or if
                Content Safety could not be reached. Both carry the same neutral
                message; only the
                :class:`~chip_chat.vision.validate.RejectionReason` and the span
                distinguish them.
        """
        outcome: ModerationVerdict | ModerationUnavailableError
        with content_safety(subject="image") as guard:
            try:
                outcome = self._verdict(self._analyzer.analyze(photo.data))
            except ModerationUnavailableError as error:
                # Fail closed. No moderation service means no vision lane, and
                # the visitor is asked to type what they wanted instead. This is
                # the one outcome that is a *failure* rather than a decision, so
                # it is the one that marks the span.
                guard.record_failure(error)
                guard.block(RejectionReason.MODERATION_UNAVAILABLE.value)
                outcome = error
            else:
                guard.set_metadata(severities=dict(outcome.severities))
                if outcome.flagged:
                    guard.block(
                        RejectionReason.UNSAFE_IMAGE.value, categories=outcome.flagged
                    )
                else:
                    guard.allow()

        # Raised after the span closes, deliberately. A refusal is what this
        # guard is *for*, not a failure of it -- and an exception unwinding
        # through the context manager would set an error status on
        # ``guard.content_safety`` for every photograph it correctly refused.
        # That would put a working block and a Content Safety outage in the same
        # bucket, in the one place the two are supposed to be told apart.
        if isinstance(outcome, ModerationUnavailableError):
            raise rejection(RejectionReason.MODERATION_UNAVAILABLE) from outcome
        if outcome.flagged:
            raise rejection(RejectionReason.UNSAFE_IMAGE)
        return outcome

    def _verdict(self, severities: Mapping[str, int]) -> ModerationVerdict:
        """Compare what the service said against the thresholds.

        Args:
            severities: The analyzer's answer.

        Returns:
            The verdict.

        Raises:
            ModerationUnavailableError: If a category we screen for is absent.
                A partial answer is not a pass -- it is an answer we cannot read,
                and stage 3 treats those the same as no answer at all.
        """
        missing = sorted(
            category.value
            for category in SafetyCategory
            if category.value not in severities
        )
        if missing:
            raise ModerationUnavailableError(
                f"content safety reported no severity for: {', '.join(missing)}"
            )
        flagged = tuple(
            sorted(
                category
                for category, severity in severities.items()
                if severity >= self._thresholds.block_at(category)
            )
        )
        return ModerationVerdict(
            severities=MappingProxyType(dict(severities)), flagged=flagged
        )


# --- the real client --------------------------------------------------------

SERVICE_MIN_EDGE: Final = 50
SERVICE_MAX_EDGE: Final = 2048
SERVICE_MAX_BYTES: Final = 4 * 1024 * 1024
"""What Content Safety accepts: 50x50 to 2048x2048, and no more than 4 MB.

The defaults in :class:`~chip_chat.vision.limits.UploadLimits` land a normalized
photo comfortably inside all three -- 1024 pixels on the longest edge at quality
85 is a couple of hundred kilobytes -- which is another reason stage 3 sits
behind stage 2 rather than in front of it. The ceilings are configurable though,
and a photograph the service will not look at must not become a photograph
nothing looked at, so :func:`_fit_for_service` makes a copy that fits.
"""


class AzureImageAnalyzer:
    """Calls Content Safety image analysis with the app's managed identity.

    Constructed from the environment the Container App is given. The account
    grants ``Cognitive Services User`` to the app identity, so there is no key
    to configure and nothing here that could authenticate with one.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "ContentSafetyClient") -> None:
        """Wrap a client.

        Args:
            client: A client already pointed at the Content Safety endpoint.
        """
        self._client = client

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureImageAnalyzer":
        """Build an analyzer from ``AZURE_CONTENT_SAFETY_ENDPOINT``.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            An analyzer pointed at the endpoint.

        Raises:
            RuntimeError: If the variable is missing. Failing at startup is the
                point: an unconfigured moderator would otherwise fail closed on
                every upload, which looks exactly like a service outage and
                takes a day to tell apart from one.
        """
        # Imported here rather than at module scope so the thresholds and the
        # verdict logic stay importable -- and unit-testable -- without the
        # Azure SDK's import cost or an identity chain to resolve.
        from azure.ai.contentsafety import ContentSafetyClient
        from azure.identity import DefaultAzureCredential

        source = os.environ if env is None else env
        endpoint = source.get(ENDPOINT_VARIABLE, "").strip()
        if not endpoint:
            raise RuntimeError(f"content safety is not configured: {ENDPOINT_VARIABLE}")

        return cls(
            ContentSafetyClient(endpoint=endpoint, credential=DefaultAzureCredential())
        )

    def analyze(self, image: bytes) -> Mapping[str, int]:
        from azure.ai.contentsafety.models import AnalyzeImageOptions, ImageData
        from azure.core.exceptions import AzureError

        try:
            result = self._client.analyze_image(
                AnalyzeImageOptions(image=ImageData(content=_fit_for_service(image)))
            )
        except AzureError as error:
            raise ModerationUnavailableError(str(error)) from error

        severities: dict[str, int] = {}
        for analysis in result.categories_analysis or ():
            severity = analysis.severity
            if severity is None:
                # A category reported with no severity is not a zero. It is an
                # answer with a hole in it, and stage 3 does not fill holes in.
                raise ModerationUnavailableError(
                    f"content safety returned no severity for {analysis.category}"
                )
            severities[str(analysis.category)] = severity
        return severities


def _fit_for_service(data: bytes) -> bytes:
    """Return a copy of ``data`` inside Content Safety's own size limits.

    Downscales first, because the maximum is the one an ordinary configuration
    could cross, then pads the short side up to the minimum rather than
    enlarging: padding adds white margin around the photograph, where upscaling
    would invent pixels and could smear the very detail the classifier is
    looking for.

    Args:
        data: The normalized JPEG bytes.

    Returns:
        ``data`` unchanged when it already fits, or a re-encoded copy that does.

    Raises:
        ModerationUnavailableError: If the image cannot be made to fit -- which
            means we are about to send something the service will refuse, and
            stage 3 would rather say "unavailable" than send it and read the
            refusal as a verdict.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            width, height = opened.size
            fits = (
                min(width, height) >= SERVICE_MIN_EDGE
                and max(width, height) <= SERVICE_MAX_EDGE
            )
            if fits and len(data) <= SERVICE_MAX_BYTES:
                return data
            working = opened.convert("RGB")
            if max(width, height) > SERVICE_MAX_EDGE:
                scale = SERVICE_MAX_EDGE / max(width, height)
                working = working.resize(
                    (
                        max(1, round(width * scale)),
                        max(1, round(height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            if min(working.size) < SERVICE_MIN_EDGE:
                canvas = Image.new(
                    "RGB",
                    (
                        max(working.width, SERVICE_MIN_EDGE),
                        max(working.height, SERVICE_MIN_EDGE),
                    ),
                    (255, 255, 255),
                )
                canvas.paste(working, (0, 0))
                working = canvas
            fitted = _encode_under(working, SERVICE_MAX_BYTES)
    except (OSError, ValueError) as error:
        raise ModerationUnavailableError(f"could not prepare image: {error}") from error

    if fitted is None:
        raise ModerationUnavailableError("image will not fit content safety's ceiling")
    return fitted


def _encode_under(image: Image.Image, ceiling: int) -> bytes | None:
    """Encode ``image`` as JPEG small enough for ``ceiling``, or return ``None``.

    Args:
        image: The RGB image to encode.
        ceiling: The byte ceiling to come in under.

    Returns:
        The encoded bytes, or ``None`` if even the lowest quality tried is too
        big -- which a 2048-pixel photograph does not manage, and a caller must
        still handle rather than assume.
    """
    for quality in (85, 70, 55):
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= ceiling:
            return encoded
    return None
