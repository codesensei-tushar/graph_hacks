"""Replay Quillfern's scripted workspace into Company Brain.

Two ways to run it.

In-process (default): starts the fake Slack Web API, points the app at it, seeds
Quillfern into the graph, loads the scripted repos, plays the chat history and the
seven demo steps through the real routes, and prints every reply. Nothing reaches
Slack. The graph it leaves behind is the demo graph.

    python scripts/replay_workspace.py --graph company_demo

Against a running instance (``--app-url``): seeds the graph that instance uses (unless
``--no-seed``), loads the repos into it, and posts the same signed events to its
``/slack/events``. Scripted ids are mapped to the real workspace with ``--map``; the
replies land in Slack, not here. This is how the demo workspace is loaded before the
seven steps are typed live.

    python scripts/replay_workspace.py --app-url http://localhost:8000 --team T0123 --bot U0BOT \\
        --map CENGINEERING=C0ENG --map GPAYMENTS=G0PAY --map UMAYA=U0MAYA ...

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", help="FalkorDB graph to load (default: FALKORDB_GRAPH from .env)")
    parser.add_argument("--app-url", help="a running instance to post events to; omit to run in-process with the fake Slack")
    parser.add_argument("--team", help="real team id (with --app-url)")
    parser.add_argument("--bot", help="real bot user id (with --app-url)")
    parser.add_argument("--map", action="append", default=[], metavar="SCRIPTED=REAL", help="map a scripted user/channel id to the real one")
    parser.add_argument("--steps", choices=["all", "history", "demo"], help="what to play (default: all in-process, history with --app-url)")
    parser.add_argument("--no-seed", action="store_true", help="keep the graph as it is; only play")
    parser.add_argument("--no-github", action="store_true", help="skip the scripted repos")
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


def _play(replayer, steps: str) -> None:
    from src.seed import quillfern_workspace as script

    if steps in ("all", "history"):
        print("\n# History")
        _print_replayed(replayer.play(script.HISTORY))
    if steps in ("all", "demo"):
        for step, rows in replayer.play_steps(script.DEMO_STEPS):
            print(f"\n# {step.title}")
            _print_replayed(rows)


def _seed_and_repos(args) -> None:
    from src.core.config import settings
    from src.seed import quillfern
    from src.seed.seed import reset_and_seed
    from scripts.replay.driver import load_github_history

    if not args.no_seed:
        print(f"seeded quillfern graph={settings.falkordb_graph} nodes={reset_and_seed(quillfern)}")
    if not args.no_github:
        for result in load_github_history():
            print(f"loaded repo={result['repo']} pull_requests={result['pull_requests']} issues={result['issues']}")


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
        _play(replayer, args.steps or "all")
        # Queued reactions and expiry jobs would wait for a scheduler that never runs here.
        clear_team_state(replayer)
        print(f"\nfake Slack calls={len(fake.calls)} bot messages={sum(1 for m in fake.messages if m.user == fake.bot_user_id and not m.deleted)}")


def run_against_instance(args) -> None:
    import httpx

    from src.core.config import settings
    from scripts.replay.driver import WorkspaceReplayer

    if not (args.team and args.bot):
        sys.exit("--app-url needs --team and --bot")
    ids = dict(item.split("=", 1) for item in args.map)
    print(f"posting signed events to {args.app_url}/slack/events for team {args.team} (bot {args.bot})")
    print("id map: " + (", ".join(f"{k}->{v}" for k, v in ids.items()) or "none; scripted ids are used as they are"))
    _seed_and_repos(args)
    with httpx.Client(base_url=args.app_url, timeout=30) as http:
        replayer = WorkspaceReplayer(http, signing_secret=settings.slack_signing_secret, team_id=args.team, bot_user_id=args.bot, ids=ids)
        _play(replayer, args.steps or "history")
    print("\nreplies are in Slack; approve cards there by hand")


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
