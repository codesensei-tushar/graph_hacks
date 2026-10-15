"""Slack-generic parts of company-brain `brain/turn/approval-requests.ts`.

Kept: the human summary of a tool call shown on an approval card, batching several
pending approvals into one card, and cancelling a card whose turn was superseded by
a newer instruction (from ``runSlackApprovalDecision`` in `brain/slack/turn.ts`).
Left out: Gmail draft/send handling and AI-SDK tool-approval message plumbing,
which belong to connectors and the agent loop.
"""

import re
from dataclasses import dataclass, replace

from src.domain.access import approvals
from src.domain.access.approvals import PendingApproval
from src.integrations.slack.client import resolved_approval_blocks, update_interaction_response
from src.integrations.slack.format import humanize_tool_action

SUPERSEDED_TEXT = "That approval was superseded by a newer instruction in the thread."

RECIPIENT_KEYS = [
    "to", "toEmail", "to_email", "toEmailAddress", "to_email_address", "recipient", "recipients",
    "recipientEmail", "recipient_email", "recipientEmailAddress", "recipient_email_address", "receiver",
    "receivers", "receiverEmail", "receiver_email", "email", "emails",
]
EXTRA_RECIPIENT_KEYS = ["extraRecipients", "extra_recipients", "additionalTo", "additional_to"]
CC_KEYS = ["cc", "ccEmail", "cc_email", "ccEmails", "cc_emails", "additionalRecipients", "additional_recipients"]
BCC_KEYS = ["bcc", "bccEmail", "bcc_email", "bccEmails", "bcc_emails"]
HIDDEN_RECIPIENT_KEYS = ["hiddenRecipients", "hidden_recipients", "hiddenRecipient", "hidden_recipient"]
ASSIGNEE_KEYS = [
    "assignee", "assigneeId", "assignee_id", "assigneeEmail", "assignee_email", "assignees", "owner",
    "ownerId", "owner_id", "ownerEmail", "owner_email",
]
RECIPIENT_EMAIL_KEYS = ["email", "emailAddress", "email_address", "address"]
RECIPIENT_NAME_KEYS = ["name", "fullName", "full_name", "displayName"]
TARGET_KEYS = [
    ("threadId", "Thread"), ("thread_id", "Thread"), ("id", "ID"), ("channel", "Channel"), ("channelId", "Channel"),
    ("issueId", "Issue"), ("customerId", "Customer"), ("repository", "Repository"), ("repo", "Repository"), ("url", "URL"),
]
CODE_TARGET_KEYS = {"threadId", "thread_id", "id", "issueId", "customerId", "repository", "repo"}
MESSAGE_CONTENT_KEYS = [
    "textContent", "markdownContent", "markdown", "body", "message", "messageBody", "message_body", "content",
    "emailBody", "email_body", "htmlContent", "html_content", "comment", "description",
]
SUBJECT_KEYS = ["subject", "title"]
FALLBACK_OMIT_KEYS = {
    "arguments", "slug", *(key for key, _ in TARGET_KEYS), *RECIPIENT_KEYS, *EXTRA_RECIPIENT_KEYS, *CC_KEYS,
    *BCC_KEYS, *HIDDEN_RECIPIENT_KEYS, *ASSIGNEE_KEYS, *SUBJECT_KEYS, *MESSAGE_CONTENT_KEYS,
}


@dataclass
class TurnApprovalRequest:
    approval_id: str
    tool_name: str
    tool_input: dict
    summary: str
    slug: str | None = None


# --- turn/util.ts helpers -------------------------------------------------------------


def compact_text(value: str, max_chars: int = 1400) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", value.replace("\r\n", "\n")).strip()
    return f"{compact[: max_chars - 1].strip()}..." if len(compact) > max_chars else compact


def first_string(record: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def tool_input_slug(tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    slug = tool_input.get("slug", tool_input.get("tool"))
    return slug if isinstance(slug, str) else None


def tool_input_arguments(tool_input) -> dict:
    if not isinstance(tool_input, dict):
        return {}
    args = tool_input.get("arguments")
    return args if isinstance(args, dict) else tool_input


# --- summaries ------------------------------------------------------------------------


def _first_defined(record: dict, keys: list[str]):
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def _recipient_text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                email = first_string(item, RECIPIENT_EMAIL_KEYS)
                name = first_string(item, RECIPIENT_NAME_KEYS)
                parts.append(f"{name} <{email}>" if email and name else email or name or "")
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else None
    if isinstance(value, dict):
        email = first_string(value, RECIPIENT_EMAIL_KEYS)
        name = first_string(value, RECIPIENT_NAME_KEYS)
        return f"{name} <{email}>" if email and name else email or name
    return None


def _merge_recipient_text(*values) -> str | None:
    parts: list[str] = []
    for value in values:
        text = _recipient_text(value)
        if not text:
            continue
        for part in re.split(r"\s*,\s*", text):
            if part and part not in parts:
                parts.append(part)
    return ", ".join(parts) if parts else None


def _format_target_value(key: str, value: str) -> str:
    return f"`{value.replace('`', chr(39))}`" if key in CODE_TARGET_KEYS else value


def _approval_targets(args: dict, skip_thread_target: bool = False) -> list[str]:
    targets = []
    for key, label in TARGET_KEYS:
        if skip_thread_target and key in ("threadId", "thread_id"):
            continue
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(f"{label}: {_format_target_value(key, value.strip())}")
    return targets


def _send_to_approval_summary(args: dict) -> str:
    lines = []
    channel = args.get("channel").strip() if isinstance(args.get("channel"), str) and args.get("channel").strip() else None
    user = args.get("slackUserId").strip() if isinstance(args.get("slackUserId"), str) and args.get("slackUserId").strip() else None
    if channel:
        if re.match(r"^[CG][A-Z0-9]{6,}$", channel, re.IGNORECASE):
            label = f"<#{channel}>"
        elif channel.startswith("#") or channel.startswith("<#"):
            label = channel
        else:
            label = f"#{channel}"
        lines.append(f"To: {label}")
    elif user:
        lines.append(f"To: DM <@{user}>")
    message = args.get("message") if isinstance(args.get("message"), str) else ""
    lines.append(compact_text(f"Message:\n{message}", 1_800) if message.strip() else "Message: (empty)")
    lines.append("Action: Deliver with your name attributed")
    return "\n".join(lines)


def _forget_memories_approval_summary(args: dict) -> str:
    count = len(args["ids"]) if isinstance(args.get("ids"), list) else 0
    lines = [f"Forget: {count} memor{'y' if count == 1 else 'ies'} previewed above"]
    query = first_string(args, ["query"])
    if query:
        lines.append(compact_text(f"Match: {query}", 300))
    reason = first_string(args, ["reason"])
    if reason:
        lines.append(compact_text(f"Reason: {reason}", 300))
    lines.append("Action: Forget after requester approval")
    return "\n".join(lines)


def approval_summary(tool_name: str, tool_input) -> str:
    slug = tool_input_slug(tool_input)
    action = slug or tool_name
    args = tool_input_arguments(tool_input)
    if tool_name == "send_to":
        return _send_to_approval_summary(args)
    if tool_name == "forget_memories":
        return _forget_memories_approval_summary(args)
    lines: list[str] = []
    to = _merge_recipient_text(_first_defined(args, RECIPIENT_KEYS), _first_defined(args, EXTRA_RECIPIENT_KEYS))
    skip_thread_target = bool(to) or re.search(r"\b(email|gmail|mail)\b", action, re.IGNORECASE) is not None
    lines += _approval_targets(args, skip_thread_target)
    if to:
        lines.append(f"To: {to}")
    cc = _recipient_text(_first_defined(args, CC_KEYS))
    if cc:
        lines.append(f"Cc: {cc}")
    bcc = _merge_recipient_text(_first_defined(args, BCC_KEYS), _first_defined(args, HIDDEN_RECIPIENT_KEYS))
    if bcc:
        lines.append(f"Bcc: {bcc}")
    subject = first_string(args, SUBJECT_KEYS)
    if subject:
        lines.append(f"Subject: {subject}")
    assignee = _merge_recipient_text(_first_defined(args, ASSIGNEE_KEYS))
    if assignee:
        lines.append(f"Assignee: {assignee}")

    main = first_string(args, MESSAGE_CONTENT_KEYS)
    if main:
        prefix = "\n".join(lines) + "\n\n" if lines else ""
        return compact_text(f"{prefix}{main}")

    details = [
        f"{key}: {str(value).lower() if isinstance(value, bool) else value}"
        for key, value in args.items()
        if key not in FALLBACK_OMIT_KEYS and isinstance(value, (str, int, float, bool))
    ][:6]
    if details:
        lines.append("\n".join(details))
    return "\n".join(lines) if lines else f"Run {humanize_tool_action(action)}"


def batch_approval(requests: list[TurnApprovalRequest]) -> TurnApprovalRequest | None:
    if not requests:
        return None
    primary = requests[0]
    if len(requests) == 1:
        return primary
    items = [f"*{index + 1}. {request.slug or request.tool_name}*\n{request.summary}" for index, request in enumerate(requests)]
    summary = "\n\n".join([f"I want to run {len(requests)} actions — approve to run all of them, deny to skip all:", *items])
    return replace(primary, summary=summary)


# --- superseded cards -----------------------------------------------------------------


def cancel_superseded_approval(approval: PendingApproval, response_url: str | None = None) -> bool:
    """Mark a pending approval cancelled and collapse its card, as upstream does when the
    turn that asked for it is no longer current. True when this call cancelled it."""
    from src.domain.slack.turn.approval_expiry import approval_card

    if not approvals.mark_approval_terminal(approval.approval_id, "cancelled"):
        return False
    blocks = resolved_approval_blocks(approval_card(approval), "Cancelled")
    if response_url:
        update_interaction_response(response_url, SUPERSEDED_TEXT, blocks=blocks, replace_original=True)
        return True
    if approval.card_ts:
        from src.domain.slack.workspace import get_workspace_by_team_id

        workspace = get_workspace_by_team_id(approval.team_id)
        if workspace is not None:
            client = workspace.client
            update = getattr(client, "update_slack_message", None) or client.update_message
            update(approval.channel, approval.card_ts, SUPERSEDED_TEXT, blocks)
    return True
