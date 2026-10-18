"""Choosing a tool: one model call picks from the catalogue, or the keyword router does.

The model never writes a query. It sees the catalogue (tool names, descriptions, and
argument schemas) and the question, and returns ``{tool, args}``. The choice is checked
against the registry and the tool's own schema before anything runs. If no model is
configured, the call fails, or the choice is invalid, the keyword router in
``ask_service`` picks the tool instead, so every path works with no key.

A tool that writes the graph is accepted from the model only when the question itself
asks for a write. Thread text goes into the prompt, so a planted message must not be
able to steer a turn into filing something; the approval card is the second guard.

A follow-up in a thread ("and two hops out", "who should look at it") names nothing
by itself. When a question resolves to nothing and offers no candidates, the subject
comes from the thread: the most recent node a person named, with the tool and depth
the last reply used. A refusal that does name candidates is never overridden. When nobody in
the thread named anything ("who knows about refunds" names a topic), the subject is what the
last reply was about: the service its path lines point at most.

A follow-up to a ``what_changed`` reply that changes only the window, the kind or the scope
("and last month?", "only decisions", "what about platform?") asks ``what_changed`` again
about the same question with that one thing changed, without a model call; and a
``what_changed`` question that names nothing, asked after another tool's reply ("what changed
there?" after a search), reads around the thread's subject instead of the whole company.

This module knows nothing about Slack beyond the shape of a thread message. The turn
that renders a reply lives in ``src/domain/slack/agent_turn.py``.
"""

import json
import logging
import re
from dataclasses import dataclass

from src.core import query_metrics
from src.domain.knowledge import algorithms, ask_service, catalogue, compound, coordination, resolve
from src.domain.knowledge.dto import ToolResult, ToolSpec
from src.domain.knowledge.resolve import Entity
from src.integrations.llm.model import ModelRequest, ModelUnavailable, generate_object

log = logging.getLogger(__name__)

MODEL_PROFILE = "fast"
THREAD_CONTEXT_MESSAGES = 6
THREAD_MESSAGE_CHARS = 300
THREAD_NAMED_LIMIT = 5
THREAD_LABELS = ("Service", "Document", "Ticket", "Decision")
# Tools asked once per thing when a question lists several ("who knows auth and postgres"), with the
# labels each takes. path_between and connectors take two things on purpose and are not here.
PER_OBJECT = {
    "who_owns": ask_service.ANSWER_LABELS, "who_knows": ask_service.ANSWER_LABELS, "blast_radius": ("Service",),
    "reading_list": ("Service",), "if_person_leaves": ("Person",), "person_profile": ("Person",),
}

# The footer names the tool: "Tool `who_knows`, picked by ..." in the blocks. Slack's plain-text
# fallback of the same reply drops the backticks and, stripping italics, the underscores in the
# name ("whatchanged"); ``reply_tool`` maps that back.
# A reply to a question in two parts names both tools ("Tool `blast_radius + who_knows`"); the last is its subject's.
_TOOL_IN_REPLY = re.compile(r"(?<![A-Za-z])Tool `?(?:[a-z_]+ \+ )?([a-z_]+)`?, (?:picked|run)\b")
_DEPTH_IN_REPLY = re.compile(r"depth (\d+)")
# An approved write's reply names what it filed: "Filed *TKT-104* about Billing Service."
_FILED_IN_REPLY = re.compile(r"Filed \*([^*]+)\*")
# What a reply's path lines are about: "• Billing runbook -[DESCRIBES]-> Billing Service".
_PATH_LINE = re.compile(r"^\s*• (.+?) -\[([A-Z_]+)\]-> (.+?)\s*$", re.MULTILINE)
_POINTS_AT = frozenset({"ABOUT", "DESCRIBES", "TOUCHES", "MENTIONS", "OWNS", "RUNS"})
_MENTION = re.compile(r"<@[A-Z0-9]+>")
# How a follow-up to what_changed opens, and "everything" to drop a kind filter.
_FOLLOW_UP_OPENER = re.compile(r"^\s*(?:and|only|just|what about|how about|same for|over|for|in)\b", re.IGNORECASE)
_EVERY_KIND = re.compile(r"\b(?:everything|all of it|all kinds|every kind)\b", re.IGNORECASE)
CHANGE_FOLLOW_UP_WORDS = 6
_DEEPER = re.compile(r"\b(?:deeper|further out|more hops|next hop|one more hop|keep going)\b", re.IGNORECASE)
_ALL_THE_WAY = re.compile(r"\b(?:all the way|everything|the whole chain|the full chain|every hop)\b", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You route questions about a company to one tool from a fixed catalogue. You never write queries "
    "and never answer the question yourself.\n"
    "Pick exactly one tool. Fill its arguments with words copied from the question, or from the thread "
    "when the question refers back to it. Do not invent names. Keep each argument short: the thing "
    "named, not the whole sentence.\n"
    "When unsure which tool fits, pick who_knows."
)

# The question itself has to ask for a write before a writing tool may be chosen.
WRITE_INTENT = re.compile(
    r"^(?:ticket|file ticket|new ticket)\s*[:\-]"
    r"|\b(?:file|create|open|record|log|note|remember|decided|decide|decision|we're moving|we are moving|going with)\b",
    re.IGNORECASE,
)


class InvalidToolChoice(ValueError):
    """The model named a tool the catalogue lacks, or arguments the tool's schema rejects."""


@dataclass(frozen=True)
class ToolChoice:
    tool: str
    args: dict
    # "model" when the model picked it, "keywords" when the router did, "thread" when a
    # follow-up took its subject from the thread.
    via: str


@dataclass(frozen=True)
class ThreadSubject:
    entity: Entity
    # The tool and depth the last reply in the thread used, read from its footer and path header.
    tool: str | None = None
    depth: int | None = None


# JSON schema keywords the structured-output API rejects. The bounds stay in the ToolSpec
# for validate_args to clamp against; the model sees them in the description instead.
_UNSUPPORTED_BY_MODEL = ("minimum", "maximum", "default", "format", "pattern", "minLength", "maxLength", "minItems", "maxItems")


def model_property(schema: dict) -> dict:
    """A property schema the model API accepts, with any bounds moved into the description."""
    clean = {key: value for key, value in schema.items() if key not in _UNSUPPORTED_BY_MODEL}
    low, high = schema.get("minimum"), schema.get("maximum")
    if low is not None or high is not None:
        bounds = f"Between {low} and {high}." if low is not None and high is not None else f"At least {low}." if low is not None else f"At most {high}."
        clean["description"] = f"{schema.get('description', '').rstrip()} {bounds}".strip()
    if isinstance(schema.get("items"), dict):
        clean["items"] = model_property(schema["items"])
    return clean


def catalogue_schema(specs: list[ToolSpec] | None = None) -> dict:
    """The JSON schema the model's reply must match: a tool name, and that tool's arguments as JSON text.

    Kept deliberately small. The structured-output API rejects bounds on integers, and an
    object holding the union of every tool's optional arguments is refused as "too
    complex" once the catalogue passes a dozen tools. Each tool's argument names and types
    are in the prompt instead (``describe_catalogue``), and ``validate_args`` checks the
    reply against the chosen tool's own schema before anything runs.
    """
    specs = catalogue.list_tools() if specs is None else specs
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": [spec.name for spec in specs]},
            "args": {
                "type": "string",
                "description": "The chosen tool's arguments as one JSON object, using only the argument names listed for that tool.",
            },
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }


def parse_args(raw) -> dict:
    """The model's arguments: a JSON object written as text (or already an object)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise InvalidToolChoice("args must be a JSON object")
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise InvalidToolChoice(f"args were not JSON: {error.msg}") from None
    if not isinstance(value, dict):
        raise InvalidToolChoice("args must be a JSON object")
    return value


def describe_catalogue(specs: list[ToolSpec]) -> str:
    lines = []
    for spec in specs:
        note = " Writes the graph; the asker approves first." if spec.needs_approval else ""
        shown = {key: model_property(schema) for key, schema in spec.args_schema.get("properties", {}).items()}
        lines.append(f"- {spec.name}: {spec.description}{note}\n  args: {json.dumps(shown)}")
    return "\n".join(lines)


def thread_context(thread: list[dict] | None, limit: int = THREAD_CONTEXT_MESSAGES) -> str:
    lines = []
    for message in (thread or [])[-limit:]:
        text = " ".join(str(message.get("text") or "").split())
        if not text:
            continue
        speaker = "app" if message.get("bot_id") or message.get("app_id") else (message.get("user") or "someone")
        lines.append(f"{speaker}: {text[:THREAD_MESSAGE_CHARS]}")
    return "\n".join(lines)


def write_intent(question: str) -> bool:
    return bool(WRITE_INTENT.search(question or ""))


def _is_bot(message: dict, bot_ids: frozenset[str]) -> bool:
    return bool(message.get("bot_id") or message.get("app_id") or message.get("user") in bot_ids)


def reply_tool(text: str) -> str | None:
    """The catalogue tool a reply's footer names, from its blocks or its plain-text fallback."""
    match = _TOOL_IN_REPLY.search(text or "")
    if match is None:
        return None
    named = match.group(1)
    if catalogue.get_tool(named) is not None:
        return named
    return next((name for name in catalogue.tool_names() if name.replace("_", "") == named.replace("_", "")), named)


def message_text(message: dict) -> str:
    """What a reader sees: a message's markdown blocks when it has them, else its text.

    A reply's ``text`` is Slack's plain-text fallback, cut to one section and stripped of
    backticks, so the footer that names the tool can be missing from it.
    """
    blocks = [block.get("text") for block in message.get("blocks") or [] if block.get("type") == "markdown" and block.get("text")]
    return "\n".join(blocks) if blocks else str(message.get("text") or "")


def thread_entities(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset(), skip_text: str = "") -> list[Entity]:
    """Nodes named in the thread, most recent first.

    People's messages are resolved whole. Bot replies name many things at once, so only
    what an approved write filed ("Filed *TKT-104*") is taken from them.
    """
    named: list[Entity] = []
    for message in reversed(thread or []):
        text = str(message.get("text") or "")
        if not text or (skip_text and skip_text in text):
            continue
        if _is_bot(message, bot_ids):
            filed = _FILED_IN_REPLY.search(text)
            found = resolve.resolve_name(filed.group(1), ("Ticket", "Decision"), scopes) if filed else None
        else:
            found = resolve.resolve_name(text, THREAD_LABELS, scopes)
        if found is not None and found.found and found.entity not in named:
            named.append(found.entity)
        if len(named) == THREAD_NAMED_LIMIT:
            break
    return named


def thread_subject(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset(), skip_text: str = "") -> ThreadSubject | None:
    named = thread_entities(thread, scopes, bot_ids, skip_text) or reply_things(thread, scopes, bot_ids)
    if not named:
        return None
    tool = depth = None
    for message in reversed(thread or []):
        if _is_bot(message, bot_ids):
            text = message_text(message)
            tool = reply_tool(text) or tool
            if match := _DEPTH_IN_REPLY.search(text):
                depth = int(match.group(1))
            break
    return ThreadSubject(named[0], tool, depth)


def reply_things(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset()) -> list[Entity]:
    """The services the last reply was about, most cited first: what its path lines point at.

    Only for a thread where nobody named anything; a person's own words always come first.
    """
    reply = _last_reply(thread, bot_ids)
    if reply is None:
        return []
    counts: dict[str, int] = {}
    for _source, rel, target in _PATH_LINE.findall(reply[1]):
        if rel in _POINTS_AT:
            counts[target] = counts.get(target, 0) + 1
    things: list[Entity] = []
    for target in sorted(counts, key=lambda name: -counts[name]):
        found = resolve.resolve_name(target, ("Service",), scopes)
        if found.found and found.entity not in things:
            things.append(found.entity)
    return things


def changed_follow_up(question: str, thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset()) -> ToolChoice | None:
    """``what_changed`` again, with only what the follow-up changes, or about the thread's subject. None otherwise."""
    from src.domain.knowledge import what_changed

    reply = _last_reply(thread, bot_ids)
    if reply is None:
        return None
    index, text = reply
    last_tool = reply_tool(text)
    routed = ask_service.route(question)[0] == what_changed.TOOL
    window, only, every = what_changed.window_in(question), what_changed.only_in(question), bool(_EVERY_KIND.search(question))
    names_scope = _names_scope(question, scopes)
    if last_tool == what_changed.TOOL:
        opener = bool(_FOLLOW_UP_OPENER.search(question))
        short = len(resolve.tokens(question)) <= CHANGE_FOLLOW_UP_WORDS
        if not (routed or (opener and (window or only or every or names_scope)) or (short and (window or only or every))):
            return None
        # Newest first: this follow-up, then each question of the what_changed run it follows.
        asked = [question, *_changed_run(thread, index, bot_ids)]
        name = question if names_scope else next((earlier for earlier in asked[1:] if _names_scope(earlier, scopes)), asked[-1])
        days = next((found for found in map(what_changed.window_in, asked) if found), None)
        kind = None
        for earlier in asked:
            if _EVERY_KIND.search(earlier):
                break
            if found := what_changed.only_in(earlier):
                kind = found
                break
    else:
        if not routed or names_scope:
            return None
        subject = thread_subject(thread, scopes, bot_ids, skip_text=question)
        if subject is None or subject.entity.label != "Service":
            return None
        name, days, kind = subject.entity.name, window, only
    args = {key: value for key, value in (("name", name), ("days", days), ("only", kind)) if value}
    return ToolChoice(what_changed.TOOL, args, "thread")


def _last_reply(thread: list[dict] | None, bot_ids: frozenset[str]) -> tuple[int, str] | None:
    for index in range(len(thread or []) - 1, -1, -1):
        message = thread[index]
        if _is_bot(message, bot_ids) and (text := message_text(message)):
            return index, text
    return None


def _changed_run(thread: list[dict], index: int, bot_ids: frozenset[str]) -> list[str]:
    """What people asked for each what_changed reply in the run ending at ``index``, newest first, without the bot's mention."""
    asked: list[str] = []
    for message in reversed(thread[: index + 1]):
        text = message_text(message)
        if _is_bot(message, bot_ids):
            if reply_tool(text) != "what_changed":
                break
        elif text:
            asked.append(" ".join(_MENTION.sub(" ", text).split()))
    return asked


def _names_scope(question: str, scopes: list[str]) -> bool:
    """Whether the question names a team or a service of its own, so it sets what_changed's scope."""
    return any(resolve.resolve_name(question, (label,), scopes).found for label in ("Team", "Service"))


def thread_pull_request(thread: list[dict] | None, scopes: list[str], bot_ids: frozenset[str] = frozenset(), skip_text: str = "") -> Entity | None:
    """The pull request a person named most recently in the thread ("see notebook#45"), for who_should_know.

    Looked up by number and repo, like the tool itself. Bot replies are skipped, as in
    ``thread_entities``: they name many things at once.
    """
    for message in reversed(thread or []):
        text = str(message.get("text") or "")
        if not text or (skip_text and skip_text in text) or _is_bot(message, bot_ids):
            continue
        if (named := coordination.pull_request_in(text, scopes)) is not None:
            return named
    return None


def follow_up_choice(question: str, choice: ToolChoice, subject: ThreadSubject) -> ToolChoice | None:
    """The same question asked about the thread's subject. Depth words, or a thread about blast radius, mean blast_radius."""
    about_blast = subject.tool == "blast_radius" and choice.tool in ("who_knows", "who_owns")
    if about_blast and _ALL_THE_WAY.search(question):
        # No depth means the walk runs to saturation.
        return ToolChoice("blast_radius", {"name": subject.entity.name}, "thread")
    depth = algorithms.depth_in(question)
    if depth is None and _DEEPER.search(question):
        depth = algorithms.clamp_depth((subject.depth or 1) + 1)
    if depth is not None or about_blast:
        return ToolChoice("blast_radius", {"name": subject.entity.name, "depth": depth or algorithms.clamp_depth((subject.depth or 1) + 1)}, "thread")
    spec = catalogue.get_tool(choice.tool)
    properties = spec.args_schema.get("properties", {}) if spec else {}
    for key in ("name", "topic", "query"):
        if key in properties:
            return ToolChoice(choice.tool, {**choice.args, key: subject.entity.name}, "thread")
    return None


def run_choice(
    choice: ToolChoice, question: str, scopes: list[str], thread: list[dict] | None = None, bot_ids: frozenset[str] = frozenset()
) -> tuple[ToolResult, ToolChoice]:
    """Run the chosen tool. A question that names nothing is retried about the thread's subject.

    A question in two parts that keywords or the model chose for answers both (``compound.py``)."""
    first, rest = ask_service.split_compound(question) if choice.via in ("keywords", "model") else (question, None)
    if rest is None:
        return _run_noted(choice, question, scopes, thread, bot_ids)
    if choice.via == "model":
        # The model read both parts; the first is chosen again on its own words.
        first_choice = choose_tool(first, thread, scopes=scopes, bot_ids=bot_ids)
        choice = first_choice if not catalogue.get_tool(first_choice.tool).needs_approval else choose_with_keywords(first)
    result, ran = _run_noted(choice, first, scopes, thread, bot_ids)
    rest_choice = choose_with_keywords(rest) if choice.via == "keywords" else choose_tool(rest, thread, scopes=scopes, bot_ids=bot_ids)
    if compound.CONTACT.search(rest) or catalogue.get_tool(rest_choice.tool).needs_approval:
        # "who to contact": the people who know it. The keyword router reads "who to ping about the outage" as blast radius.
        rest_choice = ToolChoice("who_knows", {"name": rest}, rest_choice.via)
    asked = rest
    # A person or a team named in the rest is its own subject: "and what if maya leaves".
    if compound.names_nothing(rest, scopes, (*THREAD_LABELS, "Person", "Team")):
        subject = _subject_of(ran, first, scopes) if result.found else None
        if subject is None:
            # Nothing to ask it about: a search on "who to contact" would answer something else.
            return compound.offer(rest, result), ran
        rest_choice = follow_up_choice(rest, rest_choice, ThreadSubject(subject)) or rest_choice
        asked = compound.about(rest, subject)
    # The rest's subject comes from the first part, never from the thread.
    rest_result, _rest_ran = _run_noted(rest_choice, rest, scopes, None, bot_ids)
    if not rest_result.found:
        return compound.offer(rest, result), ran
    return compound.combine((first, result), (asked, rest_result)), ran


def _subject_of(ran: ToolChoice, first: str, scopes: list[str]) -> Entity | None:
    """What the first part was about: the name it was answered with, else its own words."""
    named = next((ran.args[key] for key in ("name", "topic", "query") if isinstance(ran.args.get(key), str)), first)
    for text in dict.fromkeys((named, first)):
        resolution = resolve.resolve_name(text, THREAD_LABELS, scopes)
        if resolution.found:
            return resolution.entity
    return None


def _run_noted(
    choice: ToolChoice, question: str, scopes: list[str], thread: list[dict] | None = None, bot_ids: frozenset[str] = frozenset()
) -> tuple[ToolResult, ToolChoice]:
    named = choice.args.get("name") if choice.tool in PER_OBJECT else None
    listed = compound.objects(named, PER_OBJECT[choice.tool], scopes) if isinstance(named, str) else None
    if listed:
        # "who knows auth and postgres": the same question about each, never one of them silently.
        parts = []
        for thing, asked in listed[: compound.MAX_OBJECTS]:
            part, ran = _run_noted(ToolChoice(choice.tool, {**choice.args, "name": asked}, choice.via), asked, scopes, None, bot_ids)
            parts.append((thing, part))
        return compound.each(parts, [thing for thing, _asked in listed[compound.MAX_OBJECTS :]]), choice
    with resolve.noting_taken() as taken:
        result, ran = _run_choice(choice, question, scopes, thread, bot_ids)
    if result.found and taken:
        # A name taken from part of it ("billing" for Billing Service) is said, so a wrong guess shows.
        result.answer = resolve.taken_note(taken, result.answer) + result.answer
    return ask_service.deal_window_note(question, ran.tool, ran.args, result), ran


def _run_tool(name: str, args: dict, scopes: list[str]) -> ToolResult:
    """One tool run, its graph queries counted under the tool on /ops/metrics (not read off the call stack)."""
    with query_metrics.tool_scope(name):
        return catalogue.get_tool(name).run(args, scopes)


def _run_choice(
    choice: ToolChoice, question: str, scopes: list[str], thread: list[dict] | None = None, bot_ids: frozenset[str] = frozenset()
) -> tuple[ToolResult, ToolChoice]:
    result = _run_tool(choice.tool, choice.args, scopes)
    if result.found or result.candidates or not thread:
        return result, choice
    if choice.tool == coordination.TOOL:
        if coordination.pr_reference(question) or coordination.pr_reference(str(choice.args.get("name") or "")):
            # A pull request the graph does not have is said so, never swapped for the thread's subject.
            return result, choice
        named = thread_pull_request(thread, scopes, bot_ids, skip_text=question)
        if named is not None:
            retry = ToolChoice(choice.tool, {**choice.args, "name": named.name}, "thread")
            log.info("[agent] %r named nothing; asking %s about %s from the thread", question, retry.tool, named.name)
            retried = _run_tool(retry.tool, retry.args, scopes)
            if retried.found:
                return retried, retry
    subject = thread_subject(thread, scopes, bot_ids, skip_text=question)
    if subject is None:
        return result, choice
    retry = follow_up_choice(question, choice, subject)
    if retry is None:
        return result, choice
    log.info("[agent] %r named nothing; asking %s about %s from the thread", question, retry.tool, subject.entity.name)
    retried = _run_tool(retry.tool, retry.args, scopes)
    return (retried, retry) if retried.found else (result, choice)


def choose_tool(
    question: str,
    thread: list[dict] | None = None,
    specs: list[ToolSpec] | None = None,
    scopes: list[str] | None = None,
    bot_ids: frozenset[str] = frozenset(),
) -> ToolChoice:
    """One structured model call, or the keyword router when the model is unavailable or wrong."""
    specs = catalogue.list_tools() if specs is None else specs
    if thread and scopes:
        # A what_changed follow-up keeps the thread's scope; a model reading the thread could drop it.
        follow_up = changed_follow_up(question, thread, scopes, bot_ids)
        if follow_up is not None:
            return follow_up
    # Real names for the model to copy: what people named, else what the last reply was about.
    named = (thread_entities(thread, scopes, bot_ids, skip_text=question) or reply_things(thread, scopes, bot_ids)) if thread and scopes else []
    try:
        choice = _choose_with_model(question, thread, specs, named)
        spec = next(candidate for candidate in specs if candidate.name == choice.tool)
        if spec.needs_approval and not write_intent(question):
            raise InvalidToolChoice(f"{spec.name} writes the graph but the question does not ask for a write")
        return choice
    except ModelUnavailable as error:
        log.info("[agent] model unavailable (%s); keyword router", error)
    except InvalidToolChoice as error:
        log.warning("[agent] model choice rejected (%s); keyword router", error)
    return choose_with_keywords(question)


def _choose_with_model(question: str, thread: list[dict] | None, specs: list[ToolSpec], named: list[Entity] | None = None) -> ToolChoice:
    prompt = "Tools:\n" + describe_catalogue(specs)
    context = thread_context(thread)
    if context:
        prompt += f"\n\nThread so far:\n{context}"
    if named:
        # Real names from the graph, so the model copies one instead of inventing one.
        prompt += "\n\nThings named so far in this thread: " + ", ".join(f"{entity.name} ({entity.label})" for entity in named)
    prompt += f"\n\nQuestion: {question}\n\nReply with the tool and its arguments as a JSON object, for example {{\"name\": \"billing\"}}."
    reply = generate_object(
        ModelRequest(profile=MODEL_PROFILE, system=SYSTEM_PROMPT, prompt=prompt, schema=catalogue_schema(specs), max_tokens=400)
    )
    name = reply.get("tool")
    spec = next((candidate for candidate in specs if candidate.name == name), None)
    if spec is None:
        raise InvalidToolChoice(f"unknown tool {name!r}")
    return ToolChoice(spec.name, validate_args(spec, parse_args(reply.get("args"))), via="model")


def choose_with_keywords(question: str) -> ToolChoice:
    tool, args = ask_service.route(question)
    if catalogue.get_tool(tool) is None:
        tool, args = "who_knows", {"name": question}
    return ToolChoice(tool, args, via="keywords")


def validate_args(spec: ToolSpec, args) -> dict:
    """Keep only the arguments the tool declares, with the declared types. Required ones must be present."""
    if not isinstance(args, dict):
        raise InvalidToolChoice(f"{spec.name}: args must be an object")
    clean: dict = {}
    for key, schema in spec.args_schema.get("properties", {}).items():
        value = args.get(key)
        if value is None:
            continue
        kind = schema.get("type")
        if kind == "string":
            if not isinstance(value, str) or not value.strip():
                raise InvalidToolChoice(f"{spec.name}.{key}: expected text")
            clean[key] = value.strip()
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise InvalidToolChoice(f"{spec.name}.{key}: expected an integer")
            try:
                number = int(value)
            except ValueError:
                raise InvalidToolChoice(f"{spec.name}.{key}: expected an integer") from None
            if schema.get("minimum") is not None:
                number = max(int(schema["minimum"]), number)
            if schema.get("maximum") is not None:
                number = min(int(schema["maximum"]), number)
            clean[key] = number
        elif kind == "array":
            if not isinstance(value, list):
                raise InvalidToolChoice(f"{spec.name}.{key}: expected a list")
            clean[key] = [str(item).strip() for item in value if str(item).strip()]
        else:
            clean[key] = value
    missing = [key for key in spec.args_schema.get("required", []) if key not in clean]
    if missing:
        raise InvalidToolChoice(f"{spec.name}: missing {', '.join(missing)}")
    return clean
