"""Where the models are and which deployment answers for which lane.

This module exists because of one line in issue #8's acceptance criteria:
*"Model deployment names are configuration, not hardcoded, so they can be swapped
for eval experiments later."* A deployment name that appears as a literal in an
inference call is a name you cannot swap without a code change, and Phase 9's
whole method is swapping one.

So there are two levels of indirection and they are different things:

``var.model_deployments`` in Terraform
    What is *deployed*. Keyed by deployment name, which here is also the model
    name — ``gpt-5-mini``, ``gpt-4.1-mini``. Adding an eval candidate is a new
    entry in that map.

``CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT`` / ``..._VISION_DEPLOYMENT``
    Which deployment answers for which *lane*. This is the swap. Point the chat
    lane at a different deployment name and the agent runs on a different model
    with no code change and no redeploy of the model estate.

===================================== =======================================
Variable                              Meaning
===================================== =======================================
``CHIP_CHAT_FOUNDRY_ENDPOINT``         Account endpoint, ``https://<name>.cognitiveservices.azure.com/``.
``CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT``  Deployment answering the agent's chat lane.
``CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT`` Deployment answering the photo lane.
``CHIP_CHAT_FOUNDRY_API_VERSION``      Azure OpenAI data-plane API version.
``CHIP_CHAT_FOUNDRY_API_KEY``          Optional. Development escape hatch; see below.
===================================== =======================================

**On credentials.** The deployed app authenticates as the user-assigned managed
identity, which Terraform grants *Cognitive Services OpenAI User* on the account.
:func:`credential` returns a :class:`~azure.identity.DefaultAzureCredential`,
which resolves to that identity in Azure and to ``az login`` on a laptop — the
same code path in both places, which is the point.

``CHIP_CHAT_FOUNDRY_API_KEY`` exists because ``local_auth_enabled`` is on and a
key is occasionally the fastest way to prove a network problem is not an auth
problem. It is not how anything runs. If it is set in a deployed environment,
something has gone wrong.

**On the subscription's quota.** The models here are not the newest available;
they are the newest with non-zero TPM quota in East US 2 on this subscription.
See ``docs/phase-0-verification.md`` for the numbers and the command that
produced them.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from azure.core.credentials import TokenCredential
    from openai import AzureOpenAI

__all__ = [
    "COGNITIVE_SERVICES_SCOPE",
    "FoundryConfig",
    "FoundryConfigError",
    "chat_client",
    "credential",
]

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
"""Entra scope for the Foundry data plane. Not the management-plane scope."""

_DEFAULT_API_VERSION = "2024-10-21"

_DEFAULT_TIMEOUT_SECONDS = 90.0
"""How long one model call may take before the SDK gives up.

**This is a per-call number and it is not a turn budget.** A turn runs up to
:data:`chip_chat.agent.loop.DEFAULT_MAX_STEPS` model calls, so the worst case a
turn can reach is this multiplied by that and again by
:data:`_DEFAULT_MAX_RETRIES` attempts. Bounding the turn itself is a different
mechanism and is deliberately not this one -- a turn budget has to decide what to
*say* when it fires, which is a product decision, whereas this only has to stop
an HTTP request hanging forever.

Ninety seconds has headroom in it rather than a measurement behind it, and the
distinction matters. ``docs/deployment.md`` §3.13 measured p50 41 s, p95 72.8 s
and a longest of 95.2 s, but those are **whole turns** -- several model calls plus
tool time -- and nothing here has measured a single completion. So the number is
set above the longest observed whole turn, on the reasoning that a single call
taking longer than an entire slow conversation is a hang rather than a slow
answer. What it should actually be is unmeasured, and
``docs/decisions/model-call-timeout.md`` says so.

Without it the openai-python default applies: 600 seconds, twice, per call.
"""

_DEFAULT_MAX_RETRIES = 1
"""How many times the SDK retries one model call, beyond the first attempt.

One rather than the library's two, because retries multiply against
:data:`_DEFAULT_TIMEOUT_SECONDS` and against the step count, and because the
deployment this talks to is provisioned at capacity 10 and its observed failure
mode is ``RateLimitError`` under concurrency (``docs/deployment.md`` §3.13). A
third attempt into a deployment that is already refusing for want of capacity
spends ninety more seconds to be told the same thing.

Set explicitly rather than left to the default so that the number is a decision
somebody made and can find, which is the whole reason this bug existed.
"""
"""Last GA data-plane version. Pinned, not floating: a silently newer API
version is a silently different response shape, and this is the tier every
evaluation in Phase 9 reads its numbers from."""


class FoundryConfigError(RuntimeError):
    """Configuration is absent or malformed.

    Raised eagerly at construction rather than at the first inference call, so
    that a missing endpoint fails where it can be read as a missing endpoint.
    """


def _required(env: Mapping[str, str], name: str, hint: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise FoundryConfigError(f"{name} is not set. {hint}")
    return value


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    """Read ``name`` as a number of seconds, or return ``default``.

    Raises rather than falling back when the variable is set to something that
    is not a positive number. A timeout silently reverting to its default
    because somebody typed ``90s`` is the kind of quiet failure this whole
    change exists to remove.
    """
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise FoundryConfigError(
            f"{name} is {raw!r}, which is not a number of seconds."
        ) from None
    if value <= 0:
        raise FoundryConfigError(f"{name} is {value}; it must be greater than zero.")
    return value


def _non_negative_int(source: Mapping[str, str], name: str, default: int) -> int:
    """Read ``name`` as a retry count, or return ``default``.

    Zero is meaningful and allowed: it means one attempt and no retry.
    """
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise FoundryConfigError(
            f"{name} is {raw!r}, which is not a whole number of retries."
        ) from None
    if value < 0:
        raise FoundryConfigError(f"{name} is {value}; it cannot be negative.")
    return value


@dataclass(frozen=True, slots=True)
class FoundryConfig:
    """The Foundry account, and which deployment answers for which lane."""

    endpoint: str
    """Account endpoint. The SDK calls this ``azure_endpoint``."""

    chat_deployment: str
    """Deployment the agent's conversational and tool-calling turns go to."""

    vision_deployment: str
    """Deployment the photo lane's image calls go to.

    Deliberately a *different* deployment from :attr:`chat_deployment` rather
    than the same one used twice: the two draw on separate TPM quota pools, so a
    burst of photo uploads cannot starve the agent's conversation.
    """

    api_version: str = _DEFAULT_API_VERSION

    api_key: str | None = None
    """Development escape hatch. ``None`` means Entra, which is the real path."""

    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    """Per-call ceiling, in seconds. See :data:`_DEFAULT_TIMEOUT_SECONDS`."""

    max_retries: int = _DEFAULT_MAX_RETRIES
    """Retries per call, beyond the first attempt. See :data:`_DEFAULT_MAX_RETRIES`."""

    @property
    def uses_entra(self) -> bool:
        """True when this configuration authenticates as an identity."""
        return self.api_key is None

    def deployment_for(self, lane: str) -> str:
        """Return the deployment answering ``lane``.

        Args:
            lane: Either ``"chat"`` or ``"vision"``.

        Returns:
            The deployment name to pass as the ``model`` argument.

        Raises:
            FoundryConfigError: If ``lane`` is not a known lane.
        """
        match lane:
            case "chat":
                return self.chat_deployment
            case "vision":
                return self.vision_deployment
            case _:
                raise FoundryConfigError(
                    f"unknown lane {lane!r}; expected 'chat' or 'vision'"
                )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FoundryConfig":
        """Build a configuration from the environment.

        Args:
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configuration described by ``env``.

        Raises:
            FoundryConfigError: If a required variable is absent or empty.
        """
        source = os.environ if env is None else env
        return cls(
            endpoint=_required(
                source,
                "CHIP_CHAT_FOUNDRY_ENDPOINT",
                "Read it with: terraform -chdir=infra/terraform output "
                "-raw foundry_inference_endpoint",
            ),
            chat_deployment=_required(
                source,
                "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT",
                "One of: terraform -chdir=infra/terraform output model_deployment_names",
            ),
            vision_deployment=_required(
                source,
                "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT",
                "One of: terraform -chdir=infra/terraform output model_deployment_names",
            ),
            api_version=(
                source.get("CHIP_CHAT_FOUNDRY_API_VERSION", "").strip()
                or _DEFAULT_API_VERSION
            ),
            api_key=source.get("CHIP_CHAT_FOUNDRY_API_KEY", "").strip() or None,
            timeout_seconds=_positive_float(
                source,
                "CHIP_CHAT_FOUNDRY_TIMEOUT_SECONDS",
                _DEFAULT_TIMEOUT_SECONDS,
            ),
            max_retries=_non_negative_int(
                source,
                "CHIP_CHAT_FOUNDRY_MAX_RETRIES",
                _DEFAULT_MAX_RETRIES,
            ),
        )


@lru_cache(maxsize=1)
def credential() -> "TokenCredential":
    """Return the process's Azure credential.

    Cached because :class:`~azure.identity.DefaultAzureCredential` caches tokens
    per instance, and building a fresh one per call re-walks the whole provider
    chain — which on a laptop means shelling out to ``az`` again every time.
    """
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def chat_client(config: FoundryConfig) -> "AzureOpenAI":
    """Build the Azure OpenAI client for ``config``.

    Args:
        config: Where the models are and how to authenticate.

    Returns:
        A client bound to the account endpoint. The *deployment* is chosen
        per-call via the ``model`` argument, not baked in here, so one client
        serves both lanes.
    """
    from openai import AzureOpenAI

    # `timeout` and `max_retries` are passed explicitly on BOTH constructions.
    # Leaving them off is what made a turn unbounded: the library's defaults are
    # 600 seconds and two retries, and `loop.py` runs up to `DEFAULT_MAX_STEPS`
    # calls per turn, so a hung deployment could hold one conversation open for
    # the better part of an hour. Nothing downstream would have noticed --
    # `_held_open` in the API keeps writing its heartbeat for exactly as long as
    # the turn runs, so the fix that stopped ingress cutting slow turns also
    # removed the only thing that was cutting hung ones.
    if config.api_key is not None:
        return AzureOpenAI(
            azure_endpoint=config.endpoint,
            api_key=config.api_key,
            api_version=config.api_version,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    from azure.identity import get_bearer_token_provider

    return AzureOpenAI(
        azure_endpoint=config.endpoint,
        azure_ad_token_provider=get_bearer_token_provider(
            credential(), COGNITIVE_SERVICES_SCOPE
        ),
        api_version=config.api_version,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )
