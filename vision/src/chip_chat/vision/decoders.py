"""Which formats Pillow can read, decided once, at import.

HEIC needs an opener registered before Pillow recognises it, and registration is
global process state. Doing it here -- in a module both :mod:`chip_chat.vision.validate`
and :mod:`chip_chat.vision.normalize` import -- means it has happened by the time
either of them looks at a file, whichever one the caller reached first, and that
it happens exactly once rather than per request.

Import this module for its side effect. :data:`HEIF_AVAILABLE` exists so a test
can assert the side effect actually took, because the failure mode of a
registration that silently did not run is one in five iPhone uploads being
rejected as an unreadable format.
"""

from PIL import Image
from pillow_heif import register_heif_opener

__all__ = ["HEIF_AVAILABLE", "MAX_DECODABLE_PIXELS"]

register_heif_opener()

HEIF_AVAILABLE = "HEIF" in Image.registered_extensions().values() or bool(
    Image.OPEN.get("HEIF")
)
"""True once Pillow will open a HEIC file.

See :data:`~chip_chat.vision.limits.SUPPORTED_MEDIA_TYPES` for why that matters.
"""

MAX_DECODABLE_PIXELS = Image.MAX_IMAGE_PIXELS
"""Pillow's own decompression-bomb ceiling, left at its default.

It is roughly 89 megapixels: above it Pillow warns, and at twice it Pillow
raises. Both are well above :data:`~chip_chat.vision.limits.DEFAULT_MAX_PIXELS`,
so in practice our ceiling refuses first and this one is the backstop for a
header claiming something absurd. :mod:`chip_chat.vision.validate` turns the
backstop's exception into an ordinary rejection rather than a 500.
"""
