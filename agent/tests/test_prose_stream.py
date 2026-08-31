"""The display filter that keeps a streamed answer from leaking its envelope.

``chip-2ky`` was the visitor reading
``{"claim_class":"food","citations":[...]}`` at the end of every food answer,
and :func:`chip_chat.agent.envelope.parse` closed it by reading that object off
the *finished* reply. Streaming reopens the same hole from the other side: the
provider writes the model's raw output, envelope and all, and anything
forwarded verbatim is on screen before the parser has seen a thing.

:class:`~chip_chat.agent.envelope.ProseStream` is the answer, and the property
these tests hold is one-directional. It may show less than it could. It may
never show a brace.
"""

from chip_chat.agent.envelope import ProseStream, parse


def _streamed(fragments: list[str]) -> str:
    """Feed ``fragments`` through a fresh filter and return what was shown."""
    stream = ProseStream()
    return "".join(stream.feed(fragment) for fragment in fragments)


def test_the_envelope_never_reaches_the_visitor() -> None:
    """The deployed shape: prose, a newline, then the object on its own line."""
    shown = _streamed(
        [
            "Moderately.",
            " It's braised with chipotle",
            " chiles and cumin.",
            '\n{"claim_class":"food",',
            '"citations":["menu-barbacoa-1"]}',
        ]
    )

    assert "claim_class" not in shown
    assert "{" not in shown
    assert shown == "Moderately. It's braised with chipotle chiles and cumin."


def test_what_is_shown_matches_what_the_parser_concludes_was_said() -> None:
    """The two halves of the same reply, which must not disagree.

    The filter runs forward over fragments and the parser runs backwards over
    the whole, so agreement between them is a property worth asserting rather
    than assuming -- it is the reason the trailing newline is withheld.
    """
    content = 'Moderately.\n{"claim_class":"food","citations":["menu-barbacoa-1"]}'
    fragments = [content[at : at + 7] for at in range(0, len(content), 7)]

    assert _streamed(fragments) == parse(content).text


def test_a_brace_arriving_mid_fragment_is_still_caught() -> None:
    """A chunk boundary is the provider's business and may fall anywhere."""
    shown = _streamed(['Sure thing.\n{"claim_class"', ':"none","citations":[]}'])

    assert shown == "Sure thing."


def test_prose_with_no_envelope_is_shown_whole() -> None:
    """The common case, and the one a filter must not make worse."""
    fragments = ["We have ", "barbacoa, ", "steak and ", "sofritas."]

    assert _streamed(fragments) == "We have barbacoa, steak and sofritas."


def test_a_fence_is_held_back_too() -> None:
    """``_trailing_object`` accepts a fenced object, so this must anticipate one."""
    shown = _streamed(["Moderately.\n", '```json\n{"claim_class":"food"}\n```'])

    assert "```" not in shown
    assert shown == "Moderately."


def test_holding_the_wrong_thing_costs_prose_and_never_correctness() -> None:
    """A brace in genuine prose is withheld, which is the safe way to be wrong.

    The visitor is not left reading a half-answer: ``_frames`` sends the parsed
    reply as a ``text_final`` frame and the widget replaces what it painted.
    This test pins the trade rather than pretending it does not exist.
    """
    content = "Use the {store} placeholder."
    shown = _streamed([content])

    assert shown == "Use the"
    # And the authority that repairs it says the whole sentence.
    assert parse(content).text == content
