"""Replay Quillfern's scripted workspace into Company Brain.

Two ways to run it.

In-process (default): starts the fake Slack Web API, points the app at it, seeds
Quillfern into the graph and imports its Sales team (examples/teams/quillfern-sales.toml),
loads the scripted repos, plays the chat history and the demo steps (the seven, then the
Sales beat) through the real routes, and prints every reply. Then a new public pull request,
notebook#58, arrives as a signed ``pull_request`` webhook on the real GitHub route, and the
heads-up it posts to #company-brain is printed (``COORDINATION_ALERTS`` is on in-process).
Nothing reaches Slack. The graph it leaves behind is the demo graph.

    python scripts/replay_workspace.py --graph company_demo

Against a running instance (``--app-url``): seeds the graph that instance uses (unless
``--no-seed``), loads the repos into it, and posts the same signed events to its
``/slack/events``. Scripted ids are mapped to the real workspace with ``--map``; the
replies land in Slack, not here. This is how the demo workspace is loaded before the
seven steps are typed live.

    python scripts/replay_workspace.py --app-url http://localhost:8000 --team T0123 --bot U0BOT \\
        --map CENGINEERING=C0ENG --map GPAYMENTS=G0PAY --map UMAYA=U0MAYA ...

To rehearse that path without a workspace, serve the fake Slack API on its own
(``python scripts/fake_slack.py``), start the instance pointed at it, and add
``--fake-slack http://127.0.0.1:8899``: messages are recorded there, replies are
printed here, and Approve is clicked for you.

The signing secret is SLACK_SIGNING_SECRET from .env (a throwaway is generated in-process
when it is empty), so the instance accepts the events as Slack's. Approve clicks only
work in-process; against Slack, click Approve on the card.

In-process runs blank LLM_API_KEY unless ``--with-model`` is given: without a model the
bot keeps channel messages as raw memories and nothing calls the API. A running instance
keeps its own configuration; with a model, channel-observe distills memories on its
schedule instead.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The home channel the heads-up lands in, added to the fake workspace for the last step.
HOME_CHANNEL = "CBRAIN"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", help="FalkorDB graph to load (default: FALKORDB_GRAPH from .env)")
    parser.add_argument("--app-url", help="a running instance to post events to; omit to run in-process with the fake Slack")
    parser.add_argument("--team", help="real team id (with --app-url)")
    parser.add_argument("--bot", help="real bot user id (with --app-url)")
    parser.add_argument("--map", action="append", default=[], metavar="SCRIPTED=REAL", help="map a scripted user/channel id to the real one")
    parser.add_argument("--steps", choices=["all", "history", "demo"], help="what to play (default: all in-process, history with --app-url)")
    parser.add_argument("--no-seed", action="store_true", help="keep the graph as it is; only play")
    parser.add_argument(
        "--no-sales",
        action="store_true",
        help="seed Quillfern without its Sales team and skip the Sales steps, to import examples/teams/quillfern-sales.toml live over POST /ops/import",
    )
    parser.add_argument("--no-github", action="store_true", help="skip the scripted repos")
    parser.add_argument(
        "--fake-slack",
        metavar="URL",
        help="with --app-url: the standalone fake Slack API (scripts/fake_slack.py) the instance is pointed at; "
        "registers the scripted workspace, records each message there, prints the replies, and clicks Approve",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="in-process only: keep LLM_API_KEY from .env, so triage and chime-in call the model (memories then come from channel-observe, not raw retention)",
    )
    return parser.parse_args()


def _print_replayed(rows) -> None:
    for row in rows:
        who = row.message.user
        where = row.channel
        kind = row.message.kind
        if kind == "approve":
            print(f"  [{where}] {who} clicks Approve (HTTP {row.status})")
        else:
            print(f"  [{where}] {who}: {row.message.text} (HTTP {row.status})")
        for reply in row.replies:
            indented = "\n".join("      " + line for line in reply.splitlines())
            print(f"    bot:\n{indented}")


def _play(replayer, steps: str, sales: bool = True) -> None:
    from src.seed import quillfern_workspace as script

    if steps in ("all", "history"):
        print("\n# History")
        _print_replayed(replayer.play(script.HISTORY))
    if steps in ("all", "demo"):
        # Without the team, the Sales questions only have Slack provision placeholder people for its members.
        for step, rows in replayer.play_steps(script.DEMO_STEPS if sales else script.ENGINEERING_STEPS):
            print(f"\n# {step.title}")
            _print_replayed(rows)


def _seed_and_repos(args) -> None:
    from src.core.config import settings
    from src.seed import quillfern
    from src.seed.quillfern_sales import seed_sales
    from src.seed.seed import reset_and_seed
    from scripts.replay.driver import load_github_history

    if not args.no_seed:
        print(f"seeded quillfern graph={settings.falkordb_graph} nodes={reset_and_seed(quillfern)}")
        if not args.no_sales:
            print(f"imported {seed_sales().team.TEAM_ID} team from the example team file")
    if not args.no_github:
        for result in load_github_history():
            print(f"loaded repo={result['repo']} pull_requests={result['pull_requests']} issues={result['issues']}")


def _play_heads_up(replayer, fake) -> None:
    """A new public pull request arrives as a signed webhook, and the heads-up it posts lands in #company-brain.

    notebook#58 is Maya's, touches services/auth/session.py and has no reviewers. The fake
    GitHub client knows its files (the webhook payload carries the rest), so it never joins
    the scripted repos the history loads.
    """
    import dataclasses
    from unittest import mock

    from src.core.config import settings
    from src.db.falkordb_client import kv
    from src.domain.github import alerts, webhooks
    from src.db.falkordb_client import connect
    from src.domain.slack.workspace import get_home_channel, set_home_channel
    from src.seed import quillfern_workspace as script
    from src.seed.quillfern_workspace import RepoPullRequest
    from scripts.replay.github_api import FakeGitHubClient, pull_request_payload, repo_payload
    from scripts.replay.slack_api import FakeSlackChannel

    notebook = script.REPOS[0]
    pr = RepoPullRequest(58, "Shorten session token TTL", "Session tokens expire after 15 minutes instead of an hour.", "maya-ortiz", ("services/auth/session.py",), merged=False)
    github = FakeGitHubClient((dataclasses.replace(notebook, pull_requests=notebook.pull_requests + (pr,)), *script.REPOS[1:]), script.INSTALLATION_ID)
    if HOME_CHANNEL not in fake.channels:
        fake.add_channel(FakeSlackChannel(id=HOME_CHANNEL, name="company-brain", members=[script.BOT_USER_ID]))
    previous_home = (get_home_channel(script.TEAM_ID) or {}).get("channel_id")
    set_home_channel(script.TEAM_ID, HOME_CHANNEL)
    # A rerun on the same graph posts again.
    kv().delete(alerts.dedupe_key(notebook.id, pr.number))
    payload = {"action": "opened", "installation": {"id": script.INSTALLATION_ID}, "repository": repo_payload(notebook), "pull_request": pull_request_payload(notebook, pr)}

    print("\n# A new pull request, and who should hear about it")
    seen = len(fake.messages)
    with mock.patch.object(webhooks, "client_for", return_value=github):
        status = replayer.post_github_event(settings.github_webhook_secret, "pull_request", payload)
    print(f"  [github] {pr.author} opens {notebook.full_name.split('/')[-1]}#{pr.number} “{pr.title}”, touching {', '.join(pr.files)}, no reviewers (HTTP {status})")
    posted = [message for message in fake.messages[seen:] if message.channel == HOME_CHANNEL and message.user == fake.bot_user_id]
    for message in posted:
        indented = "\n".join("      " + line for line in message.text.splitlines())
        print(f"    bot in #company-brain:\n{indented}")
    if not posted:
        print("    (no heads-up was posted)")
    # The demo graph keeps the scripted install; a live instance on it must not post to a channel only the fake has.
    _, graph = connect()
    graph.query("MATCH (w:SlackWorkspace {team_id: $team}) SET w.home_channel_id = $home", {"team": script.TEAM_ID, "home": previous_home})


def run_in_process(args) -> None:
    from scripts.replay.driver import (
        WorkspaceReplayer,
        clear_team_state,
        fake_workspace_from_script,
        register_fake_workspace,
        use_agent_loop,
    )
    from scripts.replay.slack_api import FakeSlackServer

    fake = fake_workspace_from_script()
    with FakeSlackServer(fake) as server:
        # Settings are read once, when src.core.config first loads, so the fake's address and
        # a blank bot token go into the environment before anything imports it.
        if "src.core.config" in sys.modules:
            sys.exit("src.core.config loaded before the fake Slack API was configured; nothing was replayed")
        os.environ["SLACK_API_BASE_URL"] = server.base_url
        os.environ["SLACK_BOT_TOKEN"] = ""
        if not args.with_model:
            # No model: the bot keeps channel messages as raw memories and never calls the API.
            os.environ["LLM_API_KEY"] = ""
        if not args.no_github:
            # The last step sends a signed pull_request webhook; GitHub's API is the fake client.
            os.environ["COORDINATION_ALERTS"] = "true"
            os.environ["GITHUB_APP_ID"] = os.environ.get("GITHUB_APP_ID") or "replay"
            os.environ["GITHUB_WEBHOOK_SECRET"] = os.environ.get("GITHUB_WEBHOOK_SECRET") or secrets.token_hex(16)
            if not (os.environ.get("GITHUB_PRIVATE_KEY") or os.environ.get("GITHUB_PRIVATE_KEY_PATH")):
                os.environ["GITHUB_PRIVATE_KEY"] = "unused"
        from fastapi.testclient import TestClient

        from src.core.config import settings
        from src.main import app

        _seed_and_repos(args)
        register_fake_workspace()
        use_agent_loop()
        replayer = WorkspaceReplayer(
            TestClient(app),
            signing_secret=settings.slack_signing_secret,
            fake=fake,
            fake_base_url=server.base_url,
            response_url=server.response_url("replay"),
        )
        _play(replayer, args.steps or "all", sales=not args.no_sales)
        if not args.no_github and (args.steps or "all") in ("all", "demo"):
            _play_heads_up(replayer, fake)
        # Queued reactions and expiry jobs would wait for a scheduler that never runs here.
        clear_team_state(replayer)
        print(f"\nfake Slack calls={len(fake.calls)} bot messages={sum(1 for m in fake.messages if m.user == fake.bot_user_id and not m.deleted)}")


def run_against_instance(args) -> None:
    import httpx

    from src.core.config import settings
    from scripts.replay.driver import FakeSlackRemote, WorkspaceReplayer, register_fake_workspace

    if not (args.team and args.bot):
        sys.exit("--app-url needs --team and --bot")
    ids = dict(item.split("=", 1) for item in args.map)
    print(f"posting signed events to {args.app_url}/slack/events for team {args.team} (bot {args.bot})")
    print("id map: " + (", ".join(f"{k}->{v}" for k, v in ids.items()) or "none; scripted ids are used as they are"))
    _seed_and_repos(args)
    fake = None
    if args.fake_slack:
        register_fake_workspace()
        fake = FakeSlackRemote(args.fake_slack, bot_user_id=args.bot)
        print(f"registered the scripted workspace install; messages and replies go through the fake at {args.fake_slack}")
    with httpx.Client(base_url=args.app_url, timeout=30) as http:
        replayer = WorkspaceReplayer(
            http,
            signing_secret=settings.slack_signing_secret,
            team_id=args.team,
            bot_user_id=args.bot,
            fake=fake,
            ids=ids,
            response_url=f"{args.fake_slack.rstrip('/')}/response/replay" if args.fake_slack else "https://example.invalid/response",
        )
        _play(replayer, args.steps or "history", sales=not args.no_sales)
    if not args.fake_slack:
        print("\nreplies are in Slack; approve cards there by hand")
    print("the new-pull-request heads-up step runs in-process only: a running instance would ask the real GitHub API for its files")


def main() -> None:
    args = parse_args()
    if args.graph:
        os.environ["FALKORDB_GRAPH"] = args.graph
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("SLACK_SIGNING_SECRET"):
        os.environ["SLACK_SIGNING_SECRET"] = secrets.token_hex(16)
    if args.app_url:
        run_against_instance(args)
    else:
        run_in_process(args)


if __name__ == "__main__":
    main()
