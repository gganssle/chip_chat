"""Export configuration. One instrumentation, two backends, no branching code.

Decision D6 of RFC-001 says the agent-observability vendor is a configuration
value: Phoenix runs locally from week one and the exporter repoints at Arize AX
for the public phase by changing an endpoint. That claim has to be *true*, not
merely intended, so this module names no vendor at all. There is one OTLP slot
and one Application Insights slot, and which product answers on the OTLP
endpoint is none of this package's business.

Everything is read from the standard OpenTelemetry environment variables, so the
same configuration also works for any sidecar or collector that speaks OTLP.

===================================== =======================================
Variable                              Meaning
===================================== =======================================
``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` Full traces URL, used verbatim.
``OTEL_EXPORTER_OTLP_ENDPOINT``        Base URL; ``/v1/traces`` is appended.
``OTEL_EXPORTER_OTLP_TRACES_HEADERS``  ``k=v,k=v``; falls back to the next row.
``OTEL_EXPORTER_OTLP_HEADERS``         ``k=v,k=v`` -- API keys and space ids.
``APPLICATIONINSIGHTS_CONNECTION_STRING`` Enables the App Insights exporter.
``CHIP_CHAT_ENVIRONMENT``              ``deployment.environment``; default ``local``.
``CHIP_CHAT_OTEL_CONSOLE``             Truthy adds a console exporter.
===================================== =======================================

Every slot is optional. A configuration with no exporters at all is valid and
useful: spans are still built and still validated against the schema, they are
simply dropped, which is what tests and one-off scripts want.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from chip_chat.otel.service import SERVICE_NAMESPACE, service_name

__all__ = ["TelemetryConfig"]

_TRACES_PATH = "/v1/traces"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_EMPTY_HEADERS: Mapping[str, str] = MappingProxyType({})


def _parse_headers(raw: str) -> Mapping[str, str]:
    """Parse the OTLP ``key=value,key=value`` header form."""
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        if not pair.strip():
            continue
        key, separator, value = pair.partition("=")
        if not separator:
            raise ValueError(f"malformed OTLP header entry: {pair!r}")
        headers[key.strip()] = value.strip()
    return MappingProxyType(headers)


def _traces_endpoint(env: Mapping[str, str]) -> str | None:
    """Resolve the traces endpoint the way the OTLP specification prescribes."""
    signal_specific = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if signal_specific:
        return signal_specific
    base = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not base:
        return None
    parts = urlsplit(base)
    path = parts.path.rstrip("/") + _TRACES_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Where a component's spans go, and what they are labelled with."""

    component: str
    """The package directory name, e.g. ``"api"``. Becomes ``service.name``."""

    otlp_endpoint: str | None = None
    """Full OTLP/HTTP traces URL. Phoenix today, Arize AX later, same field."""

    otlp_headers: Mapping[str, str] = _EMPTY_HEADERS
    """Headers for the OTLP endpoint -- where an API key or space id belongs."""

    otlp_timeout_seconds: float | None = None

    azure_monitor_connection_string: str | None = None
    """Enables the Application Insights exporter when set."""

    console_export: bool = False
    """Adds a console exporter. Development affordance, never on in Azure."""

    environment: str = "local"
    """``deployment.environment`` -- keeps local traces out of production views."""

    extra_resource_attributes: Mapping[str, str] = field(
        default_factory=lambda: _EMPTY_HEADERS
    )

    def __post_init__(self) -> None:
        # Validates the component name and raises early if it is malformed.
        service_name(self.component)

    @property
    def service_name(self) -> str:
        """The ``service.name`` these spans are published under."""
        return service_name(self.component)

    @property
    def exports_anywhere(self) -> bool:
        """True when at least one backend is configured."""
        return bool(
            self.otlp_endpoint
            or self.azure_monitor_connection_string
            or self.console_export
        )

    def resource_attributes(self) -> Mapping[str, str]:
        """The resource attributes every span from this component carries."""
        attributes = {
            "service.name": self.service_name,
            "service.namespace": SERVICE_NAMESPACE,
            "deployment.environment": self.environment,
        }
        attributes.update(self.extra_resource_attributes)
        return MappingProxyType(attributes)

    @classmethod
    def from_env(
        cls,
        component: str,
        env: Mapping[str, str] | None = None,
    ) -> "TelemetryConfig":
        """Build a configuration from the environment.

        Args:
            component: The package directory name, e.g. ``"api"``.
            env: Environment mapping to read; defaults to :data:`os.environ`.

        Returns:
            The configuration described by ``env``.

        Raises:
            ValueError: If ``component`` is malformed or a header entry is.
        """
        source = os.environ if env is None else env
        raw_headers = source.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or source.get(
            "OTEL_EXPORTER_OTLP_HEADERS", ""
        )
        timeout = source.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "").strip()
        return cls(
            component=component,
            otlp_endpoint=_traces_endpoint(source),
            otlp_headers=_parse_headers(raw_headers),
            otlp_timeout_seconds=float(timeout) if timeout else None,
            azure_monitor_connection_string=(
                source.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip() or None
            ),
            console_export=source.get("CHIP_CHAT_OTEL_CONSOLE", "").strip().lower()
            in _TRUTHY,
            environment=source.get("CHIP_CHAT_ENVIRONMENT", "").strip() or "local",
        )
