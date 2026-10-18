"""Slack mrkdwn for brain replies. Every answer cites its graph path and where memories came from."""

from src.domain.memory.writeback import scope_label

MAX_PATH_STEPS = 20
MAX_MEMORY_SNIPPET = 220

HELP_TEXT = "\n".join(
    [
        "*Ask me about the company.* Who owns something, why a decision was made, what breaks if a service goes down, what changed lately, or who knows about a topic.",
        "",
        "• In a thread, follow up with `and last month?`, `only decisions` or `what about platform?`; a question that names nothing is about what the thread is about",
        "• `remember: <note>` saves a note to this conversation's memory",
        "• `ticket: <title>` files a ticket in the company graph after you approve it",
        "• `decision: <what was decided>` records a decision, and what it replaces, after you approve it",
        "• `onboard team: <name> | members: @a @b | owns: Renewals (process)` adds a team, its people and what it runs, after you approve it",
        "• `what can you see` lists the memory I can read from here",
        "• `link github` (in a DM) connects your GitHub account so I can use private repos you work on",
        "• Admins: `promote @user`, `demote @user`, `forget this channel`, `backfill public channels`",
        "",
        "_In a public channel I only use public memory. In a private channel I add that channel. In a DM I also use every private channel you're in and every private repo your linked GitHub account can see._",
    ]
)


def _clip(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


def format_path(path: list[dict]) -> list[str]:
    # The same citation PathStep.cite() produces, and the shape the README promises.
    lines = [f"• {step['from']} -[{step['rel']}]-> {step['to']}" for step in path[:MAX_PATH_STEPS]]
    if len(path) > MAX_PATH_STEPS:
        lines.append(f"_…and {len(path) - MAX_PATH_STEPS} more edges_")
    return lines


def format_answer(
    result: dict,
    memories: list[dict],
    read_tags: list[str],
    channel_names: dict[str, str],
    chosen_by: str | None = None,
) -> str:
    """``chosen_by`` is "model" or "keywords" when the agent picked the tool; the footer then names both."""
    parts = [result.get("answer") or "I couldn't find that in the company graph."]
    path = result.get("path") or []
    if path:
        # The header says what was walked, how deep, and how many edges, so a reader can judge
        # the answer before reading the list: "Path (blast_radius, depth 2, 7 edges)".
        header = f"`{result.get('tool', 'graph')}`"
        if result.get("depth") is not None:
            header += f", depth {result['depth']}"
        header += f", {len(path)} edge{'s' if len(path) != 1 else ''}"
        parts += ["", f"*Path* ({header})", *format_path(path)]
    if result.get("found", True) and result.get("candidates"):
        parts += ["", f"_Also matched: {', '.join(result['candidates'])}. Ask again with that name to switch._"]
    if memories:
        parts += ["", "*From conversations*"]
        for memory in memories:
            where = scope_label(memory["scope"], channel_names)
            if memory.get("channel_id") and memory["scope"] == "org_shared":
                where = f"<#{memory['channel_id']}>"
            learned = f", learned {memory['learned_at']}" if memory.get("learned_at") else ""
            parts.append(f"• “{_clip(memory['text'], MAX_MEMORY_SNIPPET)}” — {memory['author']}, {where}{learned}")
    footer = f"Read from: {', '.join(scope_label(tag, channel_names) for tag in read_tags)}"
    if chosen_by == "approval":
        footer = f"Tool `{result.get('tool', 'graph')}`, run after your approval. {footer}"
    elif chosen_by:
        picker = "the model" if chosen_by == "model" else "keywords"
        footer = f"Tool `{result.get('tool', 'graph')}`, picked by {picker}. {footer}"
        if chosen_by == "thread":
            footer = footer.replace(". Read from:", ". Subject from this thread. Read from:", 1)
    parts += ["", f"_{footer}_"]
    return "\n".join(parts)


def format_scopes(read_tags: list[str], write_tag: str | None, channel_names: dict[str, str]) -> str:
    lines = ["*From here I can read:*", *[f"• {scope_label(tag, channel_names)}" for tag in read_tags]]
    if write_tag:
        lines += ["", f"*New memories from here go to:* {scope_label(write_tag, channel_names)}"]
    else:
        lines += ["", "_Nothing said here is saved._"]
    return "\n".join(lines)
