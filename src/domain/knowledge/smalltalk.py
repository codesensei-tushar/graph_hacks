"""Greetings, "what can you do" and thanks: answered as help or an acknowledgement, never as a graph question.

"hi" or "thanks!" resolved against the graph comes back as "Nothing in the graph matches", which
reads as broken. ``reply`` recognises the whole message as small talk (nothing else in it) and
returns what to say, or None. Slack (``domain/slack/compute.py``) and the ask page both use it.
"""

import re

HELP = "help"
THANKS = "thanks"

_GREETING = r"(?:hi|hello|hey|hiya|howdy|yo|good (?:morning|afternoon|evening)|greetings)(?: there| all| everyone| team| brain| company brain)?"
_HELP = (
    r"(?:what (?:can|do) you do|what are you|who are you|how do (?:i|you) use (?:this|you|it)|help(?: me)?|how does this work"
    r"|what can i ask(?: you)?|what should i ask(?: you)?)"
)
_THANKS = r"(?:thanks?(?: you)?(?: so much| a lot)?|thx|ty|cheers|much appreciated|great|perfect|awesome|nice|got it|ok(?:ay)?|cool)"
_END = r"[\s!.?,:)]*"
_MESSAGE = re.compile(rf"^\s*(?:(?P<greeting>{_GREETING}){_END})?(?:(?P<help>{_HELP})|(?P<thanks>{_THANKS}))?{_END}$", re.IGNORECASE)

HELP_TEXT = (
    "Ask me about the company: who owns or knows something, why a decision was made, what breaks if a service goes down, "
    "what changed lately, what a ticket's status is, or who someone is. For example: “who owns billing”, "
    "“what breaks if auth goes down”, “what changed this week”, “what is PAY-12”."
)
THANKS_TEXT = "You're welcome. Ask me anything else about the company."


def kind(message: str) -> str | None:
    """``help`` for a greeting or "what can you do", ``thanks`` for an acknowledgement, else None."""
    match = _MESSAGE.match(message or "")
    if match is None or not (match.group("greeting") or match.group("help") or match.group("thanks")):
        return None
    if match.group("thanks") and not match.group("help"):
        return THANKS
    return HELP


def reply(message: str) -> str | None:
    """What to say to small talk, or None when the message is a question for the graph."""
    found = kind(message)
    if found == THANKS:
        return THANKS_TEXT
    if found == HELP:
        greeting = _MESSAGE.match(message).group("greeting")
        return (f"{greeting.strip().capitalize()}! " if greeting else "") + HELP_TEXT
    return None
