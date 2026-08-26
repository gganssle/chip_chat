"""Stage 4. The model describes; it never names a SKU.

RFC-001 D3, in one sentence: *"Asked for product identifiers, a model will
confidently produce ones that have never existed. Constraining it to an
ingredient vocabulary generated from the live catalogue makes fabrication
structurally impossible rather than statistically rare, and moves the failure
into a slot confidence we can threshold on."*

"Structurally impossible" is a strong claim and this module is where it is
either true or merely aspirational. Three things make it true, and none of them
is the prompt:

**The vocabulary is generated, so nothing here is a food name.** The enums come
from :mod:`chip_chat.vision.vocabulary`, which loads a module the catalogue
build wrote. There is no list of proteins in this package to fall out of date.

**The schema is enforced by the API, not by parsing.** The response format is
strict structured output, so the model's decoder cannot emit a token outside the
enum. :meth:`MealDescriber.describe` then validates the answer anyway, because
"the vendor promised" is not a foundation D3 should rest on, and a violation is
*rejected* rather than repaired -- every repair available here would be a guess
about a photograph made by something that never saw it.

**The one free-text field cannot reach the matcher, because it is not on the
object the matcher is given.** :class:`DescribedMeal` has no ``notes`` field.
``notes`` lives on :class:`Description`, next to it, and stage 5 takes the meal::

    description = describer.describe(photo.blob_ref)
    render_to_visitor(description.notes)      # display-only, and the only reader
    resolve(description.meal)                 # stage 5. No notes to be had.

That is the difference between a rule and an arrangement. A rule saying "do not
parse notes" is obeyed until somebody is in a hurry; a matcher that is handed an
object with no notes on it cannot parse them at all.

Counting meals
--------------

``meals_visible`` counts **orderable meal-sized compositions**, and
``docs/decisions/multi-meal-photos.md`` requires that definition in the prompt
rather than leaving it to the model: a bowl next to a bag of chips is one meal.
V0 gates the entire pipeline on this integer reaching two, so a loose reading
fires the decline on the most ordinary photograph anyone sends.

The same decision forbids asking the model to rank prominence or pick a primary
meal. That was rejected as an unverifiable visual judgement from the component
whose output D3 refuses to trust as a product identifier. The schema returns one
slot set plus a count, and the count is spent on knowing when to stop.

Stage 4 is allowed to fail
--------------------------

RFC-001 section 10: a lane may fail, the conversation may not. Every way this
stage can go wrong -- the deployment being down, a response that does not parse,
a response that violates the schema -- raises :class:`DescribeUnavailableError`
or :class:`DescriptionRejectedError`, both of which the caller turns into the
same ask: describe the meal in words. They are separate types because the trace
should be able to tell "the model is down" from "the model answered nonsense",
and :mod:`chip_chat.otel` records which happened.
"""

import base64
import json
import os
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, cast

from chip_chat.otel import ToolName, agent_step, tool_call, vision_describe
from chip_chat.vision.normalize import NORMALIZED_MEDIA_TYPE
from chip_chat.vision.store import BlobReader, BlobRef
from chip_chat.vision.vocabulary import SchemaViolationError, Vocabulary

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from openai import AzureOpenAI
    from openai.types.chat import ChatCompletionMessageParam
    from openai.types.shared_params import ResponseFormatJSONSchema

__all__ = [
    "API_VERSION_VARIABLE",
    "DEPLOYMENT_VARIABLE",
    "DESCRIBE_UNAVAILABLE_MESSAGE",
    "ENDPOINT_VARIABLE",
    "SYSTEM_PROMPT",
    "AzureVisionModel",
    "ConfidenceProfile",
    "DescribeError",
    "DescribeUnavailableError",
    "DescribedMeal",
    "Description",
    "DescriptionRejectedError",
    "MealDescriber",
    "SlotValue",
    "VisionModel",
    "confidence_profile",
]

ENDPOINT_VARIABLE: Final = "CHIP_CHAT_FOUNDRY_ENDPOINT"
DEPLOYMENT_VARIABLE: Final = "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT"
API_VERSION_VARIABLE: Final = "CHIP_CHAT_FOUNDRY_API_VERSION"
API_KEY_VARIABLE: Final = "CHIP_CHAT_FOUNDRY_API_KEY"
"""The Foundry variables, read here rather than through :mod:`chip_chat.agent`.

``chip_chat.agent.foundry`` holds the same four, and this is not an oversight.
The agent's ``match_meal_from_photo`` tool will call *this* package, so
``chip_chat.agent`` depends on ``chip_chat.vision``; reaching back the other way
for a configuration reader would make that a cycle between two distributions.
:class:`AzureVisionModel` also takes a client directly, so a wiring layer that
already built one from :func:`chip_chat.agent.foundry.chat_client` should pass
it in rather than have this class build a second.
"""

_DEFAULT_API_VERSION: Final = "2024-10-21"
"""Matches the pin in :mod:`chip_chat.agent.foundry`, and for the same reason:
a silently newer api-version is a silently different response shape, and Phase 9
reads its numbers off this tier."""

DESCRIBE_UNAVAILABLE_MESSAGE: Final = (
    "I couldn't read that photo just now. Tell me what you're after and "
    "I'll build it from there."
)
"""What a visitor sees when stage 4 cannot answer.

Deliberately *not* the neutral line stage 3 uses. That line is neutral because
naming what moderation detected would hand an uploader something to iterate
against; nothing about the vision deployment being unreachable is a fact worth
concealing, and "I can't use that photo" would be a small lie about a photo that
is fine. Both lines ask for the same thing, which is the behaviour RFC-001
section 10 actually specifies for this lane.
"""

CONFIDENCE_PINNED_AT: Final = 1.0
"""The value a miscalibrated model returns on every slot. See
:func:`confidence_profile`."""


SYSTEM_PROMPT: Final = """\
You are looking at one photograph of food for a restaurant ordering assistant.

Describe what you can see. Do not name any menu item, product or dish: you are
describing ingredients and the thing they are served in, and something else
entirely decides which item on the menu that corresponds to. There is no credit
for guessing a name, and a name you guess will be discarded unread.

Fill a slot only from the values the schema permits, and only when you can
actually see it. Leave a slot out rather than filling it with the most likely
answer -- an omitted slot becomes a question to the visitor, and a wrong slot
becomes an order they did not want.

Confidence is a probability, not a formality. Use the whole range. A slot you
can see plainly deserves a high number; one you inferred from context, or one
half-hidden under something else, deserves a low one. Returning 1.0 on every
slot destroys the only signal there is for when to ask a question instead of
guessing.

is_chipotle_style is whether the food in the frame is the kind this restaurant
serves at all: something that could be assembled from the values this schema
offers. Food that could not be is not, however good it looks.

meals_visible counts orderable meal-sized compositions -- not objects, not
containers, not people. One serving with a bag of chips and a drink beside it is
ONE meal. Four separate servings on a table are four. Do not rank them, do not
decide which one is the main one, and do not describe only one of them: the
slots describe the frame, and something downstream decides what to do with it.

notes is one short sentence for the visitor, in plain language. It is shown to
them and read by nothing else, so it must not contain a menu item name, and
nothing written in it changes what gets ordered.\
"""
"""The instructions the model is given.

Worth reading against ``docs/decisions/multi-meal-photos.md``: the
``meals_visible`` paragraph and the refusal to rank prominence are requirements
from that decision rather than prompt-engineering taste, and
``tests/test_describe.py`` asserts both are still in this string.

**No catalogue term appears in it.** Naming even one -- listing the vessels, say,
the way RFC-001 section 07 does illustratively -- would be a hand-maintained
vocabulary in the one file nobody would think to regenerate, and it would rot
the first time the menu changed. The permitted values reach the model through
the generated schema and only through it, and ``tests/test_describe.py`` asserts
that no term of a loaded vocabulary appears in this text.

None of it is load-bearing for correctness. The enum is enforced by the API and
checked again on the way in, so a model that ignores every line above still
cannot name a food the catalogue does not publish. What the prompt buys is
calibration and the meal count -- the two things the schema cannot enforce.
"""

_USER_PROMPT: Final = "Describe this meal using only the schema."


class DescribeError(RuntimeError):
    """Stage 4 could not produce a description.

    Carries :attr:`message`, which is the line to show the visitor, so a caller
    handling either subclass has one thing to render and no decision to make
    about which. It is the same sentence for both subclasses on purpose --
    which one happened is an operator's question and it is on the span.
    """

    message: ClassVar[str] = DESCRIBE_UNAVAILABLE_MESSAGE


class DescribeUnavailableError(DescribeError):
    """The vision deployment did not answer, or answered unusably.

    The RFC-001 section 10 case: *"Vision model unavailable -- ask the visitor to
    describe the meal in words; the rest of the order flow is unchanged."*
    """


class DescriptionRejectedError(DescribeError):
    """The model answered, and the answer was not something the schema permits.

    Separate from :class:`DescribeUnavailableError` because they say different
    things about the system: an outage is operational, and a schema violation is
    either a model regression or a vocabulary that has drifted from the
    deployment. The visitor sees the same sentence for both; a trace does not.
    """

    def __init__(self, violation: SchemaViolationError) -> None:
        """Record which part of the response broke the schema.

        Args:
            violation: What the validator refused, and where.
        """
        super().__init__(str(violation))
        self.violation = violation


@dataclass(frozen=True, slots=True, order=True)
class SlotValue:
    """One slot the model filled: a catalogue term, and how sure it was."""

    value: str
    """A term from the generated vocabulary. Never a menu item name."""

    confidence: float
    """The model's probability for :attr:`value`, in ``[0, 1]``.

    Stage 5 thresholds on this rather than on the model's prose, which is the
    "moves the failure into a slot confidence we can threshold on" half of D3.
    """


@dataclass(frozen=True, slots=True)
class DescribedMeal:
    """What the model saw, in catalogue terms. **The object stage 5 receives.**

    There is no ``notes`` field, and its absence is the mechanism rather than an
    omission: the matcher cannot read the free-text field because the matcher is
    never given it. See :class:`Description`.
    """

    is_chipotle_style: bool
    """Whether the food in the frame is the kind this restaurant serves."""

    meals_visible: int
    """Orderable meal-sized compositions in the frame. Two or more stops the
    pipeline -- see ``docs/decisions/multi-meal-photos.md``."""

    vessel: SlotValue | None = None
    protein: SlotValue | None = None
    rice: SlotValue | None = None
    beans: SlotValue | None = None
    """The single-valued slots. ``None`` means the model did not see one, which
    is a question for the visitor and not a reason to pick the popular answer."""

    salsas: tuple[SlotValue, ...] = ()
    toppings: tuple[SlotValue, ...] = ()

    @property
    def several_meals(self) -> bool:
        """Whether the frame holds more than one orderable meal."""
        return self.meals_visible >= 2

    def slots(self) -> tuple[tuple[str, SlotValue], ...]:
        """Every filled slot as ``(slot name, value)``, in schema order.

        A multi-valued slot contributes one entry per value, so the toppings of
        one meal arrive as several ``("toppings", ...)`` pairs rather than as a
        list somebody has to remember to flatten.
        """
        filled: list[tuple[str, SlotValue]] = []
        for name in ("vessel", "protein", "rice", "beans"):
            single: SlotValue | None = getattr(self, name)
            if single is not None:
                filled.append((name, single))
        for name in ("salsas", "toppings"):
            filled.extend((name, value) for value in getattr(self, name))
        return tuple(filled)

    def confidences(self) -> tuple[float, ...]:
        """Every filled slot's confidence, in the order :meth:`slots` returns."""
        return tuple(value.confidence for _, value in self.slots())


@dataclass(frozen=True, slots=True)
class Description:
    """One stage-4 answer: the meal, and the sentence shown to the visitor.

    The split is the point. :attr:`meal` goes to stage 5. :attr:`notes` goes to
    the renderer and nowhere else, and there is no path from one to the other:
    :class:`DescribedMeal` does not hold a reference to this object, does not
    carry the raw response, and has no field the sentence could hide in.
    ``tests/test_describe.py`` walks the whole object graph reachable from
    :attr:`meal` and asserts the notes text is not in it.
    """

    meal: DescribedMeal
    """The structured description. The only thing stage 5 is given."""

    notes: str = ""
    """Display-only prose. **Nothing downstream may parse this.**"""

    image_ref: BlobRef | None = None
    """Which photograph this describes, for a trace and for a UI that shows it
    back. A reference, never bytes -- see :mod:`chip_chat.vision.store`."""

    content_version: str | None = None
    """The catalogue build whose vocabulary the model was constrained to."""


class VisionModel(Protocol):
    """The one call stage 4 makes to a vision deployment."""

    @property
    def deployment(self) -> str:
        """The deployment name, for the span. Configuration, never a literal."""
        ...

    def describe(
        self,
        *,
        image: bytes,
        media_type: str,
        response_format: Mapping[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Return the model's raw response text.

        Args:
            image: The stored JPEG bytes -- the ones Content Safety screened.
            media_type: Their media type, for the data URI.
            response_format: The structured-output request, from
                :meth:`~chip_chat.vision.vocabulary.Vocabulary.response_format`.
                An implementation must pass this to the API rather than fold it
                into the prompt: enforcement by the decoder is what makes an
                off-catalogue term unreachable rather than unlikely.
            system_prompt: The instructions.
            user_prompt: The turn's single user message.

        Returns:
            The response content, still unparsed.

        Raises:
            DescribeUnavailableError: If the deployment could not be reached or
                returned no content. Stage 4 declines rather than guessing, so
                an implementation must raise rather than return something
                plausible.
        """
        ...


class MealDescriber:
    """Stage 4. One instance per process is enough."""

    __slots__ = ("_images", "_model", "_vocabulary")

    def __init__(
        self,
        model: VisionModel,
        *,
        images: BlobReader,
        vocabulary: Vocabulary,
    ) -> None:
        """Assemble the describer.

        Args:
            model: The vision deployment.
            images: How a stored photograph is read back. The describer reads
                the blob itself rather than being handed bytes, so that the
                image reaches exactly one place -- the model call -- and no
                caller ends up holding it next to a span.
            vocabulary: The generated vocabulary. Required and without a
                default, for the same reason
                :class:`~chip_chat.vision.intake.PhotoIntake` requires a
                moderator: a describer that could be built without one would be
                a describer whose vocabulary nobody generated.
        """
        self._model = model
        self._images = images
        self._vocabulary = vocabulary

    @property
    def vocabulary(self) -> Vocabulary:
        """The vocabulary in force, for an ops surface that wants to report it."""
        return self._vocabulary

    def describe(self, ref: BlobRef) -> Description:
        """Describe one stored photograph.

        Emits ``vision.describe``, which RFC-001 section 09 places under
        ``tool.<tool_name>`` -- so this runs inside a tool call. Use
        :meth:`describe_as_tool` if there is not one already open.

        Args:
            ref: The reference stage 3 returned. Only a photograph that passed
                moderation has one of these, which is the ordering requirement
                of RFC-001 section 07 arriving as a type.

        Returns:
            The :class:`Description`. Its ``meal`` is what stage 5 is given and
            its ``notes`` is what the visitor is shown.

        Raises:
            DescribeUnavailableError: If the image cannot be read back, or the
                deployment cannot be reached.
            DescriptionRejectedError: If the response violates the generated
                schema. Nothing is coerced: see the module docstring.
        """
        with vision_describe(
            image_ref=str(ref), model=self._model.deployment
        ) as recorder:
            if self._vocabulary.content_version is not None:
                recorder.set_metadata(
                    catalogue_content_version=self._vocabulary.content_version
                )
            try:
                payload = self._answer(ref)
            except DescribeError as error:
                recorder.record_failure(error)
                raise
            # Recorded verbatim, notes included: a trace is where an operator
            # reads what the model actually said. That is not a downstream
            # parser, which is what the ban on reading `notes` is about.
            recorder.record_description(payload)

        return _description(payload, ref, self._vocabulary.content_version)

    def describe_as_tool(self, ref: BlobRef, *, step: int = 0) -> Description:
        """Describe one photograph, opening the spans above it as well.

        ``vision.describe`` sits under ``agent.step`` and
        ``tool.match_meal_from_photo`` in RFC-001 section 09's tree, and
        :mod:`chip_chat.otel` enforces that rather than documenting it. When the
        agent calls stage 4 those spans are already open and :meth:`describe` is
        the entry point; this exists for the callers that are not the agent --
        a batch evaluation over the labeled photo set, or a script.

        Args:
            ref: The photograph to describe.
            step: The ``agent.step`` index to record.

        Returns:
            The :class:`Description`.

        Raises:
            DescribeUnavailableError: As :meth:`describe`.
            DescriptionRejectedError: As :meth:`describe`.
        """
        with (
            agent_step(index=step),
            tool_call(
                ToolName.MATCH_MEAL_FROM_PHOTO,
                # The ref, and only the ref. RFC-001 section 07 is explicit that
                # the image does not cross a tool boundary, and tool arguments
                # are recorded on the span.
                arguments={"image_ref": str(ref)},
            ),
        ):
            return self.describe(ref)

    def _answer(self, ref: BlobRef) -> dict[str, Any]:
        """Read the image, ask the model, and check what came back."""
        try:
            image = self._images.read(ref)
        except (OSError, KeyError, ValueError) as error:
            # The three a BlobReader is documented to raise: gone, wrong
            # container, and the transport failing. All three are "no photograph
            # to describe", which RFC-001 section 10 says is a declining lane
            # rather than a failing turn.
            raise DescribeUnavailableError(f"could not read {ref}: {error}") from error

        content = self._model.describe(
            image=image,
            media_type=NORMALIZED_MEDIA_TYPE,
            response_format=self._vocabulary.response_format(),
            system_prompt=SYSTEM_PROMPT,
            user_prompt=_USER_PROMPT,
        )

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            # Structured output should make this unreachable, which is exactly
            # why it is checked: the day it is reachable is the day something
            # changed about the deployment, and a parser that fell back to
            # reading prose would be the free-text path D3 removed.
            raise DescriptionRejectedError(
                SchemaViolationError("", "the response is not JSON")
            ) from error

        try:
            return self._vocabulary.validate(parsed)
        except SchemaViolationError as violation:
            raise DescriptionRejectedError(violation) from violation


def _description(
    payload: Mapping[str, Any], ref: BlobRef, content_version: str | None
) -> Description:
    """Turn a validated payload into the two objects the caller gets.

    Where ``notes`` and the meal part company, and the only place they are ever
    in the same scope.
    """
    meal = DescribedMeal(
        is_chipotle_style=bool(payload["is_chipotle_style"]),
        meals_visible=int(payload["meals_visible"]),
        vessel=_slot(payload.get("vessel")),
        protein=_slot(payload.get("protein")),
        rice=_slot(payload.get("rice")),
        beans=_slot(payload.get("beans")),
        salsas=_slots(payload.get("salsas")),
        toppings=_slots(payload.get("toppings")),
    )
    notes = payload.get("notes")
    return Description(
        meal=meal,
        notes=notes if isinstance(notes, str) else "",
        image_ref=ref,
        content_version=content_version,
    )


def _slot(value: object) -> SlotValue | None:
    if not isinstance(value, Mapping):
        return None
    return SlotValue(value=str(value["value"]), confidence=float(value["confidence"]))


def _slots(values: object) -> tuple[SlotValue, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str):
        return ()
    return tuple(slot for slot in (_slot(value) for value in values) if slot is not None)


# --- calibration ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfidenceProfile:
    """How a run's slot confidences were distributed.

    Issue #53's fourth acceptance criterion -- *"confidences are meaningfully
    distributed, not pinned at 1.0 on every slot"* -- is a property of a set of
    descriptions rather than of any one, so it needs somewhere to be computed
    that both a unit test and the labeled photo set (issue #56) can call. This
    is that place, and it lives beside the describer rather than in the eval
    package because the thing being measured is here.
    """

    confidences: tuple[float, ...]

    @property
    def slots(self) -> int:
        """How many filled slots the run produced."""
        return len(self.confidences)

    @property
    def pinned(self) -> int:
        """How many of them came back at exactly 1.0."""
        return sum(1 for value in self.confidences if value == CONFIDENCE_PINNED_AT)

    @property
    def pinned_fraction(self) -> float:
        """The share of slots at 1.0. ``0.0`` for a run with no slots at all."""
        return self.pinned / self.slots if self.slots else 0.0

    @property
    def distinct(self) -> int:
        """How many different values appeared."""
        return len(set(self.confidences))

    @property
    def spread(self) -> float:
        """Population standard deviation, or ``0.0`` with fewer than two slots."""
        return statistics.pstdev(self.confidences) if self.slots > 1 else 0.0

    def is_meaningfully_distributed(
        self, *, max_pinned_fraction: float = 0.5, min_distinct: int = 3
    ) -> bool:
        """Whether this run's confidences carry information.

        Args:
            max_pinned_fraction: The largest share of slots that may sit at 1.0.
                A model certain about half its slots is plausible on clear
                photographs; one certain about nearly all of them is not
                reporting confidence, it is reporting that it answered.
            min_distinct: How many different values must appear. Two would be
                met by a model that only ever says 1.0 or 0.5.

        Returns:
            Whether both bounds are satisfied. A run with no slots is ``False``:
            there is nothing to conclude from it, and reporting "well
            distributed" would be the wrong kind of wrong.
        """
        if not self.slots:
            return False
        return (
            self.pinned_fraction <= max_pinned_fraction and self.distinct >= min_distinct
        )


def confidence_profile(meals: Iterable[DescribedMeal]) -> ConfidenceProfile:
    """Gather the confidences of every filled slot across ``meals``.

    Args:
        meals: The descriptions a run produced -- one per photograph in the
            labeled set, or one per photograph in a trace export.

    Returns:
        The :class:`ConfidenceProfile`.
    """
    confidences: list[float] = []
    for meal in meals:
        confidences.extend(meal.confidences())
    return ConfidenceProfile(confidences=tuple(confidences))


# --- the real client --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AzureVisionModel:
    """Calls a Foundry vision deployment through the Azure OpenAI data plane.

    The deployment name is configuration rather than a literal, which is issue
    #8's acceptance criterion and Phase 9's whole method: point
    ``CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT`` at a different deployment and this
    lane runs on a different model with no code change.

    The image is sent as a data URI. It has to be: the uploads account has
    shared keys disabled and its blobs are readable only by the app's identity,
    so there is no URL the model could fetch. That is the *one* place a
    photograph leaves this process, and it is a model call rather than a tool
    argument -- the span records :class:`~chip_chat.vision.store.BlobRef`.
    """

    client: "AzureOpenAI"
    deployment: str
    max_tokens: int = 600
    temperature: float = 0.0
    """Zero, because two runs over the labeled photo set should differ because
    the model changed and not because sampling did."""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AzureVisionModel":
        """Build a model client from the Foundry variables.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            A client bound to the account endpoint and the vision deployment.

        Raises:
            RuntimeError: If the endpoint or the deployment is missing. Failing
                at startup is the point: the alternative is a lane that declines
                every photograph, which looks exactly like an outage.
        """
        # Imported here rather than at module scope so that the schema, the
        # validation and the notes/meal split stay importable -- and testable --
        # without the OpenAI SDK or an identity chain to resolve.
        from openai import AzureOpenAI

        source = os.environ if env is None else env
        endpoint = source.get(ENDPOINT_VARIABLE, "").strip()
        deployment = source.get(DEPLOYMENT_VARIABLE, "").strip()
        missing = [
            name
            for name, value in (
                (ENDPOINT_VARIABLE, endpoint),
                (DEPLOYMENT_VARIABLE, deployment),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"the vision deployment is not configured: {', '.join(missing)}"
            )
        api_version = source.get(API_VERSION_VARIABLE, "").strip() or _DEFAULT_API_VERSION
        api_key = source.get(API_KEY_VARIABLE, "").strip() or None

        if api_key is not None:
            client = AzureOpenAI(
                azure_endpoint=endpoint, api_key=api_key, api_version=api_version
            )
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            client = AzureOpenAI(
                azure_endpoint=endpoint,
                azure_ad_token_provider=get_bearer_token_provider(
                    DefaultAzureCredential(),
                    "https://cognitiveservices.azure.com/.default",
                ),
                api_version=api_version,
            )
        return cls(client=client, deployment=deployment)

    def describe(
        self,
        *,
        image: bytes,
        media_type: str,
        response_format: Mapping[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        from openai import OpenAIError

        encoded = base64.b64encode(image).decode("ascii")
        # The schema arrives as a plain mapping because
        # `chip_chat.vision.vocabulary` builds it out of what the catalogue
        # rendered and has no business importing a vendor's TypedDict to do it.
        # It matches the shape the SDK declares; the cast says so once, here, at
        # the boundary where the vendor's types begin.
        enforced = cast("ResponseFormatJSONSchema", response_format)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{encoded}",
                            # Low detail is the model's own downscale. Stage 2
                            # already fits the working resolution, so paying for
                            # tiles here would buy detail the provider discards.
                            "detail": "low",
                        },
                    },
                ],
            },
        ]
        try:
            completion = self.client.chat.completions.create(
                model=self.deployment,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format=enforced,
                messages=messages,
            )
        except OpenAIError as error:
            raise DescribeUnavailableError(str(error)) from error

        choices = completion.choices or ()
        content = choices[0].message.content if choices else None
        if not content:
            # A refusal, a length stop, or a content filter. None of them is a
            # description, and none of them may become an empty one.
            reason = choices[0].finish_reason if choices else "no choices"
            raise DescribeUnavailableError(
                f"the vision deployment returned no content ({reason})"
            )
        return content
