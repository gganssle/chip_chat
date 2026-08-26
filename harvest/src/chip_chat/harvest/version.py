"""Single source of truth for the package version.

It lives in its own module so ``transport.py`` can build the User-Agent string
without importing the package ``__init__``, which imports ``transport`` in turn.
"""

__version__ = "0.0.0"
