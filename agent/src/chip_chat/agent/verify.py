"""Phase 0 verification: prove the model deployments answer.

Issue #8 asks for two trivial scripts — one that completes a chat call and one
that completes a vision call against an image in blob storage. They are one
module with two subcommands because they share a client and a configuration, and
because what is being verified is a single claim: *the deployments in
``var.model_deployments`` are real and reachable with the credentials the app
will use.*

    uv run python -m chip_chat.agent.verify chat
    uv run python -m chip_chat.agent.verify vision

Both are read-only against the estate except that ``vision`` writes one small
PNG to the uploads container — which the container's own lifecycle rule deletes
within 24-48 hours, so it cleans up after itself.

**Why the vision check is shaped the way it is.** Asking a model to "describe
this photo" and eyeballing the answer verifies nothing: a model with no image at
all will happily describe a plausible photo, and so will a model whose image
failed to attach. So the check generates a four-quadrant image whose four
colours are not a set anything would guess, and fails unless the model names
exactly those four. That answer is not reachable without the image.

It asks for them in reading order but does **not** fail on the order, because
the order turned out to measure something other than provisioning — see
:func:`_quadrant_image` for what the sweep found and why the image is the size
it is.

These are scripts, not tests. They cost tokens and need Azure credentials, so
they are not in the pytest suite; the unit tests cover the configuration layer,
which is the part with logic in it.
"""

import argparse
import base64
import os
import struct
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from chip_chat.agent.foundry import FoundryConfig, chat_client, credential

__all__ = ["main"]

_UPLOAD_PREFIX = "phase-0-verification"

# Colours in an order chosen to be unguessable: not the rainbow, not RGB, not
# alphabetical. A model answering from a prior about "a four-colour test image"
# gets this wrong.
_QUADRANTS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("green", (0x1E, 0xA5, 0x4B)),
    ("purple", (0x7B, 0x2C, 0xBF)),
    ("orange", (0xF2, 0x7A, 0x1A)),
    ("blue", (0x1B, 0x6C, 0xD9)),
)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What one verification call proved, and what it cost."""

    lane: str
    deployment: str
    model: str
    """The model the service reports having served. Not the deployment name --
    the two differ the moment an eval experiment swaps one, and this is the
    field that says which one actually answered."""

    prompt_tokens: int
    completion_tokens: int
    detail: str

    def render(self) -> str:
        return (
            f"  lane        {self.lane}\n"
            f"  deployment  {self.deployment}\n"
            f"  served by   {self.model}\n"
            f"  tokens      {self.prompt_tokens} in / {self.completion_tokens} out\n"
            f"  {self.detail}"
        )


def _png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode raw RGB bytes as a PNG.

    Hand-rolled rather than pulled from Pillow: the whole point of this file is
    to verify Azure, and adding an image library to the dependency tree of the
    agent package to draw four squares would be a poor trade.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        pixels: ``width * height * 3`` bytes of RGB, row-major.

    Returns:
        The PNG file's bytes.
    """
    raw = b"".join(
        b"\x00" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))
        )

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _quadrant_image(size: int = 768) -> bytes:
    """Render the four-quadrant colour card the vision check asks about.

    768 pixels, and the number is load-bearing. Sweeping this on 2026-08-26
    against ``gpt-4.1-mini`` (2025-04-14):

    ======  ============  ==================================================
    Size    Image tokens  Colours returned (image is green, purple, orange, blue)
    ======  ============  ==================================================
    256     ~100          ``orange, blue, yellow, black`` -- wrong, twice
    512     ~446          ``purple, green, orange, blue`` -- right four, top row swapped
    768     ~965          ``green, purple, orange, blue`` -- exactly right
    ======  ============  ==================================================

    So the service downsamples aggressively, and below roughly 512 pixels this
    model stops resolving *which* region holds which colour, then stops
    resolving the colours at all. That matters well beyond this script: the
    Phase 6 photo lane (#53) is asking a model to distinguish a burrito from a
    bowl, which is a spatial question about the contents of a container. Uploads
    should be sent at a useful resolution rather than thumbnailed for economy,
    and the token cost of doing so is on the table above -- roughly 10x from 256
    to 768.
    """
    half = size // 2
    rows: list[bytes] = []
    for y in range(size):
        top = y < half
        left_colour = _QUADRANTS[0 if top else 2][1]
        right_colour = _QUADRANTS[1 if top else 3][1]
        rows.append(bytes(left_colour) * half + bytes(right_colour) * half)
    return _png(size, size, b"".join(rows))


def _upload(image: bytes, blob_name: str) -> str:
    """Put ``image`` in the uploads container and return its URL.

    Raises:
        RuntimeError: If the storage account or container is not configured.
    """
    from azure.storage.blob import BlobClient

    account = os.environ.get("CHIP_CHAT_UPLOADS_ACCOUNT", "").strip()
    container = os.environ.get("CHIP_CHAT_UPLOADS_CONTAINER", "").strip() or "uploads"
    if not account:
        raise RuntimeError(
            "CHIP_CHAT_UPLOADS_ACCOUNT is not set. Read it with: "
            "terraform -chdir=infra/terraform output -raw data_storage_account"
        )

    client = BlobClient(
        account_url=f"https://{account}.blob.core.windows.net",
        container_name=container,
        blob_name=blob_name,
        credential=credential(),
    )
    client.upload_blob(image, overwrite=True)
    return client.url


def _download(blob_url: str) -> bytes:
    """Read a blob back by URL, as the vision lane will have to.

    Shared keys are disabled on the uploads account and the container is
    private, so the image cannot simply be handed to the model as a URL — it is
    fetched with the caller's own identity and inlined. That is a real
    constraint on the Phase 6 photo lane, not an artefact of this script.
    """
    from azure.storage.blob import BlobClient

    client = BlobClient.from_blob_url(blob_url, credential=credential())
    return client.download_blob().readall()


def verify_chat(config: FoundryConfig) -> VerificationResult:
    """Complete a chat call against the deployed chat model."""
    deployment = config.deployment_for("chat")
    response = chat_client(config).chat.completions.create(
        model=deployment,
        messages=[
            {
                "role": "user",
                "content": (
                    "Reply with exactly one word and no punctuation: what is the "
                    "main ingredient of guacamole?"
                ),
            }
        ],
        max_completion_tokens=2000,
    )
    answer = (response.choices[0].message.content or "").strip()
    if "avocado" not in answer.lower():
        raise RuntimeError(
            f"deployment {deployment!r} answered {answer!r}, which is not an answer "
            "to the question asked. The call completed, so this is a model or "
            "prompt problem rather than a provisioning one."
        )
    usage = response.usage
    return VerificationResult(
        lane="chat",
        deployment=deployment,
        model=response.model,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        detail=f'answered "{answer}"',
    )


def verify_vision(config: FoundryConfig) -> VerificationResult:
    """Complete a vision call against an image in blob storage."""
    deployment = config.deployment_for("vision")
    blob_url = _upload(_quadrant_image(), f"{_UPLOAD_PREFIX}/quadrants.png")
    encoded = base64.b64encode(_download(blob_url)).decode("ascii")

    response = chat_client(config).chat.completions.create(
        model=deployment,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This image is divided into four equal quadrants, each a "
                            "solid colour. Name the four colours in reading order: "
                            "top-left, top-right, bottom-left, bottom-right. Reply "
                            "with exactly four lowercase words separated by commas "
                            "and nothing else."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        max_completion_tokens=2000,
    )

    answer = (response.choices[0].message.content or "").strip().lower()
    seen = [word.strip(" .") for word in answer.split(",")]
    expected = [name for name, _ in _QUADRANTS]

    # Set, not sequence. Naming these four colours is unreachable without the
    # image and is therefore the provisioning claim; getting them in the right
    # order is a statement about the model's spatial resolution, which is a
    # different question and is reported rather than enforced.
    if sorted(seen) != sorted(expected):
        raise RuntimeError(
            f"deployment {deployment!r} read the quadrants as {seen} but the image "
            f"was generated as {expected}. The call completed, so the model "
            "responded without seeing the image, or saw a different one."
        )
    ordering = "in order" if seen == expected else f"out of order as {', '.join(seen)}"
    usage = response.usage
    return VerificationResult(
        lane="vision",
        deployment=deployment,
        model=response.model,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        detail=f"read {', '.join(expected)} {ordering} from {blob_url}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one verification lane. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.agent.verify",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "lane",
        choices=["chat", "vision"],
        help="which deployment to verify",
    )
    lane = parser.parse_args(argv).lane

    config = FoundryConfig.from_env()
    auth = "Entra (DefaultAzureCredential)" if config.uses_entra else "API key"
    print(f"Foundry {config.endpoint} · api-version {config.api_version} · {auth}")

    result = verify_chat(config) if lane == "chat" else verify_vision(config)
    print(f"\nOK — {lane} deployment answered.\n{result.render()}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
