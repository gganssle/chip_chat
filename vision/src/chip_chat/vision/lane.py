"""The photo lane as one tool call: describe, then resolve, under one span.

RFC-001 section 09 puts both halves of the photo path inside a single
``tool.match_meal_from_photo``::

    tool.match_meal_from_photo
    |- vision.describe        image ref, structured output, tokens
    `- matcher.resolve        slot confidences, resolved SKUs

and issue #64's second acceptance criterion is that reading of it: *a photo
turn's trace holds image, structured description, and resolved SKUs together*.
Stages 4 and 5 each ship a ``*_as_tool`` convenience that opens its own
``agent.step`` and its own tool span, which is right for a batch evaluation over
one stage and wrong for a turn -- run back to back they produce two tool calls,
the image under one and the SKUs under the other, and the trace stops answering
"what did the lane make of this photograph" in one place.

This module is the composition. :class:`PhotoLane` runs stage 4 and stage 5
inside whatever ``tool.match_meal_from_photo`` its caller has already opened, so
the agent's tool body is one call and the tree is the one the RFC drew.

**It holds no client and no deployment.** Stage 4's model and stage 5's
catalogue are passed in already built, because a lane that could construct them
would be a second place where a deployment name is resolved -- see
:mod:`chip_chat.vision.describe` on why that reader lives where it does.

**A declining lane is a declining lane, not a failing turn** (RFC-001 section
10). :meth:`PhotoLane.match` lets stage 4's two errors out unchanged, because
the caller has to answer them differently -- "the model is down" asks the
visitor to type the order, "the model answered nonsense" does too but is a
deployment problem -- and swallowing them here would erase the distinction the
two exception types exist to preserve.
"""

from dataclasses import dataclass

from chip_chat.otel import (
    TokenUsage,
    ToolName,
    agent_step,
    tool_call,
)
from chip_chat.vision.describe import Description, MealDescriber
from chip_chat.vision.matcher import MealMatcher, Resolution
from chip_chat.vision.store import PHOTO_REF_ARGUMENT, BlobRef

__all__ = ["PhotoLane", "PhotoMatch"]


@dataclass(frozen=True, slots=True)
class PhotoMatch:
    """What the photo lane made of one photograph, both halves together."""

    description: Description
    """Stage 4's answer. Its ``notes`` is display-only, here as everywhere."""

    resolution: Resolution
    """Stage 5's answer: the catalogue rows, or the question to ask instead."""

    @property
    def usage(self) -> TokenUsage | None:
        """What the lane's model calls cost, for the tool span's rollup.

        Stage 5 has no model in it -- that is D3's second half -- so this is
        stage 4's usage and nothing else. ``None`` when the provider reported
        none, never a zero: a lane that looks free is worse than a lane that
        admits it does not know.
        """
        return self.description.usage


class PhotoLane:
    """Stage 4 and stage 5, composed into the one tool the agent calls."""

    __slots__ = ("_describer", "_matcher", "_restaurant_id")

    def __init__(
        self,
        describer: MealDescriber,
        matcher: MealMatcher,
        *,
        restaurant_id: int | None = None,
    ) -> None:
        """Assemble the lane.

        Args:
            describer: Stage 4, already holding its vocabulary and deployment.
            matcher: Stage 5, already holding its catalogue and floors.
            restaurant_id: Whose prices to quote. ``None`` defers to the
                catalogue's reference restaurant, which is what a demo wants
                and what :meth:`~chip_chat.vision.matcher.MealMatcher.resolve`
                already does with it.
        """
        self._describer = describer
        self._matcher = matcher
        self._restaurant_id = restaurant_id

    def match(self, ref: BlobRef) -> PhotoMatch:
        """Describe ``ref`` and resolve what it showed to catalogue rows.

        Must be called inside ``tool.match_meal_from_photo``; the span helpers
        refuse otherwise, which is how the tree in the module docstring is
        enforced rather than described. :meth:`match_as_tool` opens that span
        for a caller that is not the agent.

        The catalogue build stage 4 was constrained to is handed to stage 5 and
        checked there, so a describer and a matcher built from different
        catalogue versions raise rather than resolve a term that has moved.

        Args:
            ref: The photograph, as stage 3 stored it. Only a photograph that
                passed moderation has one of these.

        Returns:
            The :class:`PhotoMatch`: both halves, and what they cost.

        Raises:
            DescribeUnavailableError: The photograph could not be read, or the
                deployment could not be reached.
            DescriptionRejectedError: The response violated the schema.
            CatalogueDriftError: The two stages disagree about the catalogue.
        """
        description = self._describer.describe(ref)
        resolution = self._matcher.resolve(
            description.meal,
            restaurant_id=self._restaurant_id,
            content_version=description.content_version,
        )
        return PhotoMatch(description=description, resolution=resolution)

    def match_as_tool(self, ref: BlobRef, *, step: int = 0) -> PhotoMatch:
        """Run the lane, opening ``agent.step`` and the tool span as well.

        For the callers that are not the agent -- a batch evaluation over the
        labeled photo set, or a script. When the agent calls the lane both
        spans are already open and :meth:`match` is the entry point.

        The tool span carries the lane's token rollup, so "what does the photo
        lane cost per call" is one attribute on one span rather than a tree
        walk. See
        :attr:`~chip_chat.otel.attributes.ChipChatAttributes.TOKENS_TOTAL`.

        Args:
            ref: The photograph to run.
            step: The ``agent.step`` index to record.

        Returns:
            The :class:`PhotoMatch`.

        Raises:
            DescribeUnavailableError: As :meth:`match`.
            DescriptionRejectedError: As :meth:`match`.
            CatalogueDriftError: As :meth:`match`.
        """
        with (
            agent_step(index=step),
            tool_call(
                ToolName.MATCH_MEAL_FROM_PHOTO,
                # The ref, and only the ref: RFC-001 section 07 is explicit that
                # the image itself does not cross a tool boundary, and tool
                # arguments are recorded on the span.
                arguments={PHOTO_REF_ARGUMENT: str(ref)},
            ) as recorder,
        ):
            match = self.match(ref)
            if match.usage is not None:
                recorder.record_token_rollup(match.usage)
            return match
