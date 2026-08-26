from chip_chat.data_gen import SERVICE_NAME


def test_service_name_comes_from_the_shared_otel_package() -> None:
    assert SERVICE_NAME == "chip-chat-data-gen"
