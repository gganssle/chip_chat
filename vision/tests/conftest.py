"""Fixtures for the upload path.

The payloads themselves ship in :mod:`chip_chat.vision.testing`, alongside the
store double, because the abuse work in issue #80 wants the same set and a
package is a better place to find them than somebody else's ``tests`` directory.
"""

import pytest

from chip_chat.vision.testing import (
    ELF_BINARY,
    GZIP_ARCHIVE,
    HTML_PAGE,
    PDF_DOCUMENT,
    SHELL_SCRIPT,
    SVG_WITH_SCRIPT,
    ZIP_ARCHIVE,
)


@pytest.fixture(
    params=[
        pytest.param(ZIP_ARCHIVE, id="zip"),
        pytest.param(GZIP_ARCHIVE, id="gzip"),
        pytest.param(ELF_BINARY, id="elf"),
        pytest.param(SHELL_SCRIPT, id="shell-script"),
        pytest.param(HTML_PAGE, id="html"),
        pytest.param(SVG_WITH_SCRIPT, id="svg-with-script"),
        pytest.param(PDF_DOCUMENT, id="pdf"),
    ]
)
def disguised_payload(request: pytest.FixtureRequest) -> bytes:
    """Something that is not a photograph, whatever the request calls it."""
    payload: bytes = request.param
    return payload
