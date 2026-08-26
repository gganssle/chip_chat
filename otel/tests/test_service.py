import pytest

from chip_chat.otel import SERVICE_NAMESPACE, service_name


def test_service_name_is_namespaced() -> None:
    assert service_name("api") == "chip-chat-api"
    assert service_name("api").startswith(f"{SERVICE_NAMESPACE}-")


def test_service_name_normalises_underscores() -> None:
    assert service_name("data_gen") == service_name("data-gen") == "chip-chat-data-gen"


@pytest.mark.parametrize("bad", ["", "API", "-api", "api-", "api service", "api--x"])
def test_service_name_rejects_malformed_components(bad: str) -> None:
    with pytest.raises(ValueError, match="lowercase and hyphen-separated"):
        service_name(bad)
