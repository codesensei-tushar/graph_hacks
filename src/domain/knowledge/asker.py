"""The asker, in a Slack turn: "I", "me" and "my" are the person the Slack user is.

Both Slack answerers (the agent turn and the built-in graph answerer) call ``for_the_asker`` with
the tool chosen, so a first-person question reads around the asker's own Person, named to the tool
by id (``people.PERSON_REF``), never by name. A Slack user no Person is linked to gets
``in_slack``'s reply: Slack is where they are, so the reply says how to link, never "ask in Slack".
"""

import re

from src.db.falkordb_client import connect
from src.domain.knowledge import people, resolve
from src.domain.knowledge.agent import ToolChoice
from src.domain.knowledge.dto import ToolResult

FIRST_PERSON = re.compile(r"\b(?:i|me|my|mine)\b", re.IGNORECASE)
# Tools that take a person, and the argument that names them.
PERSON_ARGS = {"person_profile": ("name",), "if_person_leaves": ("name",), "find": ("person", "owner", "author")}
SLACK_SELF_MISS = "I don't know which person you are yet. Send `link account` to me in a DM to connect your Slack account, then ask again."


def person_ref(slack_user_id: str | None) -> str | None:
    """The asker's Person as ``person:<id>``, through the workspace's SlackUser -IS-> Person link, or None."""
    if not slack_user_id:
        return None
    _, graph = connect()
    rows = graph.ro_query("MATCH (:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.id LIMIT 1", {"id": slack_user_id}).result_set
    return people.PERSON_REF + rows[0][0] if rows else None


def for_the_asker(choice: ToolChoice, question: str, slack_user_id: str | None, read_tags: list[str]) -> ToolChoice:
    """A first-person question about the asker's own person: ``what_changed`` about their things, and the
    person tools (profile, leaving, their lists) about them. Unchanged when someone else is named."""
    if not FIRST_PERSON.search(question or ""):
        return choice
    if choice.tool == "what_changed":
        if choice.args.get("person"):
            return choice
        person = person_ref(slack_user_id)
        return ToolChoice(choice.tool, {**choice.args, "person": person}, choice.via) if person else choice
    keys = [key for key in PERSON_ARGS.get(choice.tool, ()) if key in choice.args and (key == "name" or FIRST_PERSON.search(str(choice.args[key])))]
    if not keys or (choice.tool != "find" and not FIRST_PERSON.search(str(choice.args.get("name") or ""))):
        return choice
    if choice.tool != "find" and resolve.resolve_name(str(choice.args["name"]), ("Person",), read_tags).found:
        # "what if Arjun leaves, should I worry": someone else is named; the question is about them.
        return choice
    person = person_ref(slack_user_id)
    return ToolChoice(choice.tool, {**choice.args, **{key: person for key in keys}}, choice.via) if person else choice


def in_slack(result: ToolResult) -> ToolResult:
    """A reply sent in Slack never says "ask in Slack": an unknown asker is told how to link instead."""
    if not result.found and result.answer.startswith(resolve.SELF_MISS):
        result.answer = SLACK_SELF_MISS + result.answer[len(resolve.SELF_MISS) :]
    return result
