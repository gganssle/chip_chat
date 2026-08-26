"""Running the set: every frame through the real lane, one failure at a time.

Issue #56's last scope bullet is the one that decides this module's shape --
*"runs as a repeatable experiment so a prompt or model change can be scored
rather than eyeballed"*. Three things follow from it.

**The frames go through the real stages, not past them.**
:class:`PhotoSetImages` is a :class:`~chip_chat.vision.store.BlobReader` that
reads a file off disk and hands back what stage 2 would have written: validated
in stage 1, re-encoded and downscaled to
:attr:`~chip_chat.vision.limits.UploadLimits.max_edge` in stage 2. An evaluation
that fed the model a 4032-pixel original would be scoring a pipeline nobody
runs -- and would score it *better*, since issue #63 measured this deployment's
accuracy collapsing below about 512 pixels and the production ceiling is well
above that. The number has to come from the image production would send.

Stage 3 is the one production stage this does not run. Content Safety needs a
deployment and a network, the set is photographs we took ourselves, and
moderation is not what is being measured -- so the runner reads
``eval/photos`` directly rather than pretending to screen it. That is a
deliberate hole and it is the only one.

**One frame's failure is one frame's failure.** A deployment that refuses the
eleventh photograph must not cost the other twenty-nine, so every frame is run
inside its own ``try`` and a stage-4 error becomes a recorded :attr:`PhotoRun.
error` rather than a traceback. Scoring counts those apart from everything
else: an outage is not a model being wrong.

**The trace is the one RFC-001 section 09 draws.**
:meth:`~chip_chat.vision.lane.PhotoLane.match_as_tool` opens ``agent.step`` and
``tool.match_meal_from_photo`` around both stages, which is what stage 4's and
stage 5's ``*_as_tool`` helpers were built for -- issue #53's note on this bead
says so in as many words. Running the two stages back to back instead would
produce two tool calls per frame with the image under one and the SKUs under
the other, and a batch of thirty of those is a trace nobody can read.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chip_chat.eval.photos.labels import LabeledSet, PhotoLabel
from chip_chat.otel import chat_turn
from chip_chat.vision.describe import DescribeError, Description
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.limits import UploadLimits
from chip_chat.vision.matcher import CatalogueDriftError, Resolution
from chip_chat.vision.normalize import normalize
from chip_chat.vision.store import BlobRef
from chip_chat.vision.validate import UploadRejectedError, validate

__all__ = [
    "DEFAULT_SESSION",
    "EVAL_CONTAINER",
    "PhotoRun",
    "PhotoSetImages",
    "ref_for",
    "run_set",
]

DEFAULT_SESSION: Final = "labeled-photo-set"
"""What a run's turns are grouped under when the caller names nothing else."""

EVAL_CONTAINER: Final = "labeled-photos"
"""The container name every :class:`~chip_chat.vision.store.BlobRef` in a run carries.

Not ``uploads``. A ref that reached a real store would be a ref to a
photograph nobody uploaded, and the container is the part of a ref that says
which store it belongs to.
"""


@dataclass(frozen=True, slots=True)
class PhotoRun:
    """What the lane made of one frame, or why it could not.

    Attributes:
        photo_id: The label this answers, so a run and a set can be matched up
            after the fact without depending on order.
        description: Stage 4's answer, or ``None`` where stage 4 declined.
        resolution: Stage 5's answer, or ``None`` where stage 4 never got there.
        error: Why there is nothing here, in one line. ``None`` on success.
            Present *and* a description present is impossible: stage 5 runs
            inside the same call.
    """

    photo_id: str
    description: Description | None = None
    resolution: Resolution | None = None
    error: str | None = None

    @property
    def answered(self) -> bool:
        """Whether the lane produced anything at all for this frame."""
        return self.error is None and self.resolution is not None


def ref_for(label: PhotoLabel) -> BlobRef:
    """The reference a run uses for one frame.

    Args:
        label: The label. Its ``image`` path is the blob name, so the ref that
            appears on the tool span names the file a reader can go and look at.

    Returns:
        The ref, in :data:`EVAL_CONTAINER`.
    """
    return BlobRef(container=EVAL_CONTAINER, name=label.image)


class PhotoSetImages:
    """A :class:`~chip_chat.vision.store.BlobReader` over the labeled set on disk.

    Reads the file, runs stages 1 and 2, and returns what stage 3 would have
    stored. The result is cached per ref, because a run that scored the set
    twice -- once at two floors, say -- should not re-encode thirty JPEGs to do
    it, and because the bytes are then provably identical across the two runs.
    """

    __slots__ = ("_cache", "_labels", "_limits", "_root")

    def __init__(self, labels: LabeledSet, *, limits: UploadLimits | None = None) -> None:
        """Point a reader at a set.

        Args:
            labels: The set. Its ``root`` is where the files are.
            limits: The ceilings to normalize to. Defaults to
                :class:`~chip_chat.vision.limits.UploadLimits`, which is what
                production uses -- override it only to measure what a different
                ceiling would do, which is the experiment issue #63 leaves open.
        """
        self._labels = labels
        self._root = labels.root
        self._limits = limits
        self._cache: dict[str, bytes] = {}

    @property
    def root(self) -> Path:
        """The directory the frames are read from."""
        return self._root

    def read(self, ref: BlobRef) -> bytes:
        """Return the frame as stage 2 would have written it.

        Args:
            ref: A ref from :func:`ref_for`.

        Returns:
            Normalized JPEG bytes.

        Raises:
            ValueError: If ``ref`` names another container, or an upload gate
                refuses the file -- a frame the pipeline would not have
                accepted is not a frame it can be scored on.
            KeyError: If the file is not there.
            OSError: If it could not be read.
        """
        if ref.container != EVAL_CONTAINER:
            raise ValueError(f"{ref} is not in {EVAL_CONTAINER}")
        cached = self._cache.get(ref.name)
        if cached is not None:
            return cached
        path = self._root / ref.name
        if not path.is_file():
            raise KeyError(f"no photograph at {path}")
        data = path.read_bytes()
        try:
            normalized = normalize(
                validate(data, limits=self._limits), limits=self._limits
            )
        except UploadRejectedError as error:
            raise ValueError(
                f"{path} would be refused at upload: {error.message}"
            ) from error
        self._cache[ref.name] = normalized.data
        return normalized.data


def run_set(
    labels: LabeledSet,
    lane: PhotoLane,
    *,
    only: Sequence[str] | None = None,
    session_id: str = DEFAULT_SESSION,
) -> tuple[PhotoRun, ...]:
    """Run every frame in the set through the lane.

    One ``chat.turn`` per frame, indexed in set order, all under one session.
    A photograph is a turn -- that is what it is in production -- so a batch of
    thirty is thirty traces that an experiment can compare with real ones,
    rather than one trace thirty steps deep that resembles nothing.

    Args:
        labels: The set to run. Its files must be on disk;
            :meth:`~chip_chat.eval.photos.labels.LabeledSet.missing_files` is
            how a caller finds out before spending money.
        lane: The photo lane, already built from a describer and a matcher that
            agree about the catalogue build.
        only: Photo ids to run, for iterating on one frame. ``None`` runs all.
        session_id: What to group the run's turns under.

    Returns:
        One :class:`PhotoRun` per frame, in set order.
    """
    return tuple(_runs(labels, lane, only, session_id))


def _runs(
    labels: LabeledSet,
    lane: PhotoLane,
    only: Sequence[str] | None,
    session_id: str,
) -> Iterator[PhotoRun]:
    wanted = None if only is None else set(only)
    index = 0
    for label in labels:
        if wanted is not None and label.photo_id not in wanted:
            continue
        with chat_turn(session_id=session_id, turn_index=index, message=""):
            yield _run_one(label, lane)
        index += 1


def _run_one(label: PhotoLabel, lane: PhotoLane) -> PhotoRun:
    """Run one frame, turning any lane failure into a recorded line.

    The three caught here are the three the lane documents, and each is a
    property of the deployment or the build rather than of the photograph:
    stage 4 could not read or reach, stage 4 answered off-schema, the two
    stages hold different catalogue builds. A frame that raises one of them is
    a frame with no prediction, which scores as a miss on every labeled
    component -- correctly, since nothing was produced -- and appears by name
    in the report so the miss can be read as the outage it is.

    Anything else propagates. A ``KeyboardInterrupt`` or a bug in the scorer is
    not a datum about the photograph and must not be recorded as one.
    """
    try:
        match = lane.match_as_tool(ref_for(label))
    except DescribeError as error:
        return PhotoRun(photo_id=label.photo_id, error=f"{type(error).__name__}: {error}")
    except CatalogueDriftError as error:
        return PhotoRun(photo_id=label.photo_id, error=f"CatalogueDriftError: {error}")
    return PhotoRun(
        photo_id=label.photo_id,
        description=match.description,
        resolution=match.resolution,
    )
