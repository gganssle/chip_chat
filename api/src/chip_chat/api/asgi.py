"""The ASGI entry point the container runs: ``uvicorn chip_chat.api.asgi:app``.

One line of consequence, in its own module. :mod:`chip_chat.api.app` must be
importable without Azure configuration -- the tests import it on every run --
and a module-level ``app = create_app()`` there would make importing it read the
environment and start resolving credentials. So the application is assembled
here, where the only thing that imports it is the server.
"""

from chip_chat.api.app import create_app

__all__ = ["app"]

app = create_app()
"""The application, assembled from the environment."""
