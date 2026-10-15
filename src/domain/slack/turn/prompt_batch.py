"""Port of company-brain `brain/slack/prompt-batch.ts`."""

from dataclasses import dataclass


@dataclass
class SlackPromptBatch:
    messages: list[dict]
    prompt: str


def build_slack_prompt_batch(messages: list[dict], max_chars: int) -> SlackPromptBatch:
    included: list[dict] = []
    lines: list[str] = []
    length = 0

    for message in messages:
        line = f"{message.get('user') or '?'}: {message.get('text') or ''}"
        separator_length = 1 if lines else 0
        if length + separator_length + len(line) > max_chars:
            if included:
                break
            full_marker = " … [truncated]"
            budget = max(0, max_chars)
            marker = full_marker if budget > len(full_marker) else "…"[:budget]
            truncated = f"{line[: max(0, budget - len(marker))]}{marker}"
            included.append(message)
            lines.append(truncated)
            break
        included.append(message)
        lines.append(line)
        length += separator_length + len(line)

    return SlackPromptBatch(messages=included, prompt="\n".join(lines))
