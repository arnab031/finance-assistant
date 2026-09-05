"""
Conversational turns that are not questions about the data.

"hi" used to be answered with the coverage decline - a paragraph explaining that
this database holds bank statement lines and has no vendor master, no expense
categories, no budgets. Every word of it true, and none of it a reply to what
was actually said. A greeting is not a question with no answer; it is not a
question at all, and the two read very differently to someone deciding in their
first ten seconds whether the thing works.

WHY A REGEX AND NOT ONLY A PROMPT RULE. The same argument as `absent_concepts`
in api/profiles/base.py: when the right response is fixed and knowable from the
text alone, deciding it in code is free and cannot regress on a model swap. It
also skips the extraction call entirely - "hi" is the cheapest turn there is and
should not cost a hosted API round-trip to answer.

The prompt carries an intent="smalltalk" rule for the tail this cannot match
("yo, hope your day is going well"). Both paths land in the same branch of
api/routes/ask.py, so the reply is identical either way.

MATCHING IS WHOLE-UTTERANCE ON PURPOSE. `fullmatch` against the normalised text,
so "hi" is a greeting while "hi, how much did we spend in August?" is a question
that happens to open politely. A substring match here would swallow real work,
which is a far worse failure than missing a greeting.
"""

from __future__ import annotations

import re
from typing import Any, Literal

Kind = Literal["greeting", "thanks", "farewell", "capability", "identity"]

# Keep '?' - it is the only punctuation that carries meaning here - and drop the
# rest, so "hi!!", "hi..." and a trailing emoji all normalise to "hi".
_STRIP = re.compile(r"[^\w\s?]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", _STRIP.sub(" ", text.casefold())).strip(" ?")


# Ordered: the first fullmatch wins. Identity and capability come before
# greeting because "hey what can you do" should be answered, not waved at.
_PATTERNS: tuple[tuple[Kind, re.Pattern[str]], ...] = (
    ("capability", re.compile(
        r"(hi|hey|hello)?[ ,]*"
        r"(help|help me|what can (you|u) do|what can (i|we) ask|what do you do"
        r"|what (kind of )?(questions|things) can (i|we) ask"
        r"|what (data|info|information) (do you have|is (in )?(this|the)( \w+)?)"
        r"|how (do|does) (this|it|you) work|show me (an )?examples?|examples?"
        r"|what should (i|we) ask|give me (some )?examples?)"
    )),
    ("identity", re.compile(
        r"(hi|hey|hello)?[ ,]*"
        r"(who are (you|u)|what are (you|u)|who r u|what is this|whats this"
        r"|what'?s this|introduce yourself|are you (an? )?(ai|bot|robot|human|llm|model))"
    )),
    ("thanks", re.compile(
        r"(thanks|thank you|thanks a lot|thank you so much|thanx|thx|ty|tysm"
        r"|cheers|much appreciated|appreciate it|perfect|great|awesome|nice"
        r"|got it|makes sense|ok|okay|cool)"
        r"( (thanks|thank you|so much|a lot|mate|team))?"
    )),
    ("farewell", re.compile(
        r"(bye|goodbye|good ?bye|see (you|ya)( later)?|see u|later|ttyl"
        r"|good night|goodnight|gn|that'?s all|that is all|that'?ll be all"
        r"|i'?m done|we'?re done|nothing else)"
    )),
    ("greeting", re.compile(
        r"(hi+|hey+|hello+|helo|hiya|howdy|yo|sup|hola|namaste|greetings"
        r"|good (morning|afternoon|evening|day))"
        r"([ ,]+(there|team|folks|everyone|all|bot|assistant))?"
        r"([ ,]+(how are (you|u)( doing)?|how'?s it going|how are things"
        r"|what'?s up|whats up|hope you'?re well))?"
    )),
)

# An utterance longer than this is not a greeting even if it starts like one.
# Belt and braces behind fullmatch: it bounds the damage from any future pattern
# written a little too loosely.
_MAX_WORDS = 8


def classify(question: str) -> Kind | None:
    """Which conversational turn this is, or None when it is a real question."""
    text = _normalise(question)
    if not text or len(text.split()) > _MAX_WORDS:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.fullmatch(text):
            return kind
    return None


def text_for(kind: Kind, prof: Any) -> str:
    """The reply, built from the ACTIVE PROFILE.

    Same reasoning as `unsupported_note`: a hardcoded "I can tell you about
    vendors and departments" would describe a different database the moment the
    dataset changes, and nothing in the canary would notice.
    """
    can_do = getattr(prof, "capability_note", "") or (
        "I answer questions about the financial data loaded here.")

    if kind == "thanks":
        return "Happy to help. Ask another whenever you need one."
    if kind == "farewell":
        return "Cheers - come back whenever you need a number."

    opening = {
        "greeting": "Hi.",
        "capability": "Here is what I can do.",
        "identity": "I am the finance assistant for this dataset.",
    }[kind]

    parts = [f"{opening} {can_do}".strip()]
    examples = list(getattr(prof, "suggestions", []) or [])[:3]
    if examples:
        parts.append("For example:\n" + "\n".join(f"• {e}" for e in examples))
    return "\n\n".join(parts)


def reply(question: str, prof: Any) -> str | None:
    """The whole layer: a reply for a conversational turn, None for a question."""
    kind = classify(question)
    return None if kind is None else text_for(kind, prof)
