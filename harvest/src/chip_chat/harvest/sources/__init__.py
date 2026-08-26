"""Source-specific harvesters, one subpackage per site.

Everything here is a *client* of the framework in :mod:`chip_chat.harvest`. A
source decides which URLs matter and what their bodies mean; the framework
decides how, and how politely, they are fetched. A source that imports an HTTP
client, sleeps between requests, or keeps its own cache is a bug in the
framework that should have been fixed in the framework instead.

Each source is split the same way, for the same reason: a *fetch* step that
lands raw bytes in the blob store and a *parse* step that reads only from the
cache. A parser bug is then a re-run, not a re-fetch, and the site never pays
for our mistakes twice.
"""
