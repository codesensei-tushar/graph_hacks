"""Quillfern's scripted Slack workspace and GitHub repos: dummy data for the replay.

Everything here is invented and lines up with ``quillfern.py``: the Slack users are its
six people plus the three on the Sales team (examples/teams/quillfern-sales.toml), the
channels are where they talk, and the repos hold the pull requests that changed their
services. ``scripts/replay_workspace.py`` plays it into the app through the real routes,
and ``tests/integration/test_demo_replay.py`` asserts the seven demo steps and the three
Sales steps from docs/STRATEGY.md on top of it.

The history is what the bot has already seen when the demo starts. The demo steps are
what gets typed live, in order.
"""

from dataclasses import dataclass

TEAM_ID = "TQUILLFERN"
TEAM_NAME = "Quillfern"
BOT_USER_ID = "UBRAIN"
# Only the fake Slack API accepts this token; it opens nothing real, and it does not look
# like a Slack token on purpose, so secret scanners stay quiet.
BOT_TOKEN = "replay-quillfern-bot-token"
INSTALLATION_ID = 7001


@dataclass(frozen=True)
class WorkspaceUser:
    id: str
    person_id: str
    name: str
    real_name: str
    email: str


@dataclass(frozen=True)
class WorkspaceChannel:
    id: str
    name: str
    is_private: bool = False
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceMessage:
    """One scripted message.

    ``kind``: ``say`` is a plain message the bot only remembers; ``ask`` @mentions the
    bot; ``dm`` is sent in the user's DM with the bot; ``follow_up`` replies in the
    thread of the last ``ask`` in that channel; ``approve`` clicks Approve on the last
    card the bot posted in that thread (the text is ignored).
    """

    channel: str
    user: str
    text: str
    kind: str = "say"


@dataclass(frozen=True)
class DemoStep:
    title: str
    messages: tuple[WorkspaceMessage, ...]


@dataclass(frozen=True)
class RepoPullRequest:
    number: int
    title: str
    body: str
    author: str
    files: tuple[str, ...]
    reviewers: tuple[str, ...] = ()
    merged: bool = True


@dataclass(frozen=True)
class RepoIssue:
    number: int
    title: str
    body: str
    author: str
    assignees: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repo:
    id: int
    full_name: str
    private: bool
    collaborators: tuple[str, ...]
    pull_requests: tuple[RepoPullRequest, ...]
    issues: tuple[RepoIssue, ...] = ()


# --- Slack ---------------------------------------------------------------------------------

USERS = (
    WorkspaceUser("UMAYA", "maya", "maya", "Maya Ortiz", "maya@quillfern.example.com"),
    WorkspaceUser("UARJUN", "arjun", "arjun", "Arjun Mehta", "arjun@quillfern.example.com"),
    WorkspaceUser("ULENA", "lena", "lena", "Lena Fischer", "lena@quillfern.example.com"),
    WorkspaceUser("UKOFI", "kofi", "kofi", "Kofi Mensah", "kofi@quillfern.example.com"),
    WorkspaceUser("USARA", "sara", "sara", "Sara Lindqvist", "sara@quillfern.example.com"),
    WorkspaceUser("UTOMAS", "tomas", "tomas", "Tomas Reyes", "tomas@quillfern.example.com"),
    # The Sales team; their slack_id values in the team file are these ids.
    WorkspaceUser("UPRIYA", "priya", "priya", "Priya Shah", "priya@quillfern.example.com"),
    WorkspaceUser("UDEV", "dev", "dev", "Dev Patel", "dev@quillfern.example.com"),
    WorkspaceUser("UNOOR", "noor", "noor", "Noor Haddad", "noor@quillfern.example.com"),
)

ENGINEERING = "CENGINEERING"
PAYMENTS = "GPAYMENTS"
SALES = "CSALES"

CHANNELS = (
    WorkspaceChannel(ENGINEERING, "engineering", members=tuple(u.id for u in USERS) + (BOT_USER_ID,)),
    WorkspaceChannel(PAYMENTS, "payments", is_private=True, members=("UARJUN", "ULENA", "UTOMAS", BOT_USER_ID)),
    WorkspaceChannel(SALES, "sales", members=("UPRIYA", "UDEV", "UNOOR", BOT_USER_ID)),
)

# Plain channel talk the bot has already kept as memories (no model: raw messages are retained).
# The public #engineering talk is in the seed (quillfern.MEMORIES), dated, so production has
# it too; replaying it here would keep each line twice. Only the private channel is replayed.
HISTORY = (
    WorkspaceMessage(PAYMENTS, "UARJUN", "renewal webhooks that retry are charging twice, TKT-102 tracks it, refunds go out monday"),
    WorkspaceMessage(PAYMENTS, "ULENA", "invoice worker retries will move to a queue once billing retries land there"),
)

# The seven demo moves from docs/STRATEGY.md, in order.
ENGINEERING_STEPS = (
    DemoStep(
        "Who owns billing, and who else has worked on it",
        (WorkspaceMessage(ENGINEERING, "UTOMAS", "who owns billing, and who else has worked on it", "ask"),),
    ),
    DemoStep(
        "Trace a decision to its source",
        (WorkspaceMessage(ENGINEERING, "ULENA", "why did we move the memory layer off Postgres", "ask"),),
    ),
    DemoStep(
        "A plan changes in the channel",
        (
            WorkspaceMessage(ENGINEERING, "UARJUN", "we're moving billing retries to the invoice worker, decided in today's sync"),
            # Without a model the bot only remembers that; `decision:` records it, after approval.
            WorkspaceMessage(ENGINEERING, "UARJUN", "decision: move billing retries to the invoice worker", "ask"),
            WorkspaceMessage(ENGINEERING, "UARJUN", "", "approve"),
            WorkspaceMessage(ENGINEERING, "UTOMAS", "why did we move billing retries to the invoice worker", "ask"),
        ),
    ),
    # Right after the write: the decision just recorded is the newest thing that changed.
    DemoStep(
        "What changed this week, then only decisions",
        (
            WorkspaceMessage(ENGINEERING, "UTOMAS", "what changed this week", "ask"),
            WorkspaceMessage(ENGINEERING, "UTOMAS", "only decisions", "follow_up"),
        ),
    ),
    DemoStep(
        "Blast radius, then two hops out",
        (
            WorkspaceMessage(ENGINEERING, "USARA", "what breaks if auth goes down, one hop", "ask"),
            WorkspaceMessage(ENGINEERING, "USARA", "and two hops out", "follow_up"),
        ),
    ),
    DemoStep(
        "A new ticket, approved, then who should look at it",
        (
            WorkspaceMessage(ENGINEERING, "UKOFI", "ticket: billing exports time out", "ask"),
            WorkspaceMessage(ENGINEERING, "UKOFI", "", "approve"),
            WorkspaceMessage(ENGINEERING, "UKOFI", "who should look at it", "follow_up"),
        ),
    ),
    DemoStep(
        "Something the graph does not know",
        (WorkspaceMessage(ENGINEERING, "ULENA", "who knows the payments gateway", "ask"),),
    ),
    DemoStep(
        "A private repo, seen only by its collaborator",
        (
            WorkspaceMessage("", "UKOFI", "who knows billing", "dm"),
            WorkspaceMessage("", "UARJUN", "who knows billing", "dm"),
        ),
    ),
)

# The Sales beat (round 5 in docs/STRATEGY.md): the same tools, for a team that is not
# engineering. ``replay_workspace.py --no-sales`` leaves these out along with the team.
SALES_STEPS = (
    DemoStep(
        "S1 Who owns renewals, and who else has worked on it",
        (WorkspaceMessage(SALES, "UNOOR", "who owns renewals, and who else has worked on it", "ask"),),
    ),
    DemoStep(
        "S2 What breaks if HubSpot goes down",
        (WorkspaceMessage(SALES, "UPRIYA", "what breaks if hubspot goes down", "ask"),),
    ),
    DemoStep(
        "S3 Why did we change pricing",
        (WorkspaceMessage(SALES, "UDEV", "why did we change pricing", "ask"),),
    ),
)

DEMO_STEPS = ENGINEERING_STEPS + SALES_STEPS


# --- GitHub --------------------------------------------------------------------------------

GITHUB_USERS = {
    "maya-ortiz": 9101,
    "arjun-mehta": 9102,
    "lena-fischer": 9103,
    "kofi-mensah": 9104,
    "sara-lindqvist": 9105,
    "tomas-reyes": 9106,
}

REPOS = (
    Repo(
        id=9001,
        full_name="quillfern/notebook",
        private=False,
        collaborators=tuple(GITHUB_USERS),
        pull_requests=(
            RepoPullRequest(
                41,
                "Move notebook links to the graph store",
                "Closes #12. Linked-note lookups become one traversal instead of a five-way join.",
                "kofi-mensah",
                ("services/memory/links.py", "infra/graph/schema.cypher"),
                ("sara-lindqvist", "maya-ortiz"),
            ),
            RepoPullRequest(
                45,
                "Rotate session refresh tokens hourly",
                "Clients holding an old refresh token get a new one instead of a logout.",
                "maya-ortiz",
                ("services/auth/session.py",),
                ("arjun-mehta",),
            ),
            RepoPullRequest(
                52,
                "Retry renewal webhooks once, idempotently",
                "A retried renewal webhook no longer creates a second charge.",
                "lena-fischer",
                ("services/billing/webhooks.py", "workers/invoice/retry.py"),
                ("arjun-mehta",),
            ),
        ),
        issues=(
            RepoIssue(
                12,
                "Memory queries time out on large workspaces",
                "Linked-note lookups over five hops take more than 8 seconds on Postgres.",
                "kofi-mensah",
                ("kofi-mensah",),
            ),
        ),
    ),
    Repo(
        id=9002,
        full_name="quillfern/billing-internal",
        private=True,
        collaborators=("arjun-mehta", "tomas-reyes"),
        pull_requests=(
            RepoPullRequest(
                7,
                "Guard refunds against double charges",
                "Refunds check the ledger before issuing, so a retried refund is a no-op.",
                "arjun-mehta",
                ("services/billing/refunds.py",),
                ("tomas-reyes",),
            ),
        ),
    ),
)


def user_by_id(user_id: str) -> WorkspaceUser | None:
    return next((u for u in USERS if u.id == user_id), None)
