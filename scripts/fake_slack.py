"""Serve the fake Slack Web API on its own, for a rehearsal against a real app process.

The in-process replay starts the fake itself. For the path the demo loader takes against
a running instance, run this first, then start the app with its Slack client pointed
here and no real token:

    python scripts/fake_slack.py --port 8899
    SLACK_API_BASE_URL=http://127.0.0.1:8899/api/ SLACK_BOT_TOKEN= uvicorn src.main:app --port 8010
    python scripts/replay_workspace.py --app-url http://127.0.0.1:8010 --team TQUILLFERN --bot UBRAIN \\
        --fake-slack http://127.0.0.1:8899 --steps all

It serves the scripted Quillfern workspace and prints every message the bot posts, so
the replies that would land in Slack show up here instead.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.replay.driver import fake_workspace_from_script, reply_text  # noqa: E402
from scripts.replay.slack_api import FakeSlackServer  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    fake = fake_workspace_from_script()
    with FakeSlackServer(fake, port=args.port) as server:
        print(f"fake Slack Web API for team {fake.team_id} at {server.base_url} (bot {fake.bot_user_id}); Ctrl-C to stop", flush=True)
        print(f"point the app at it: SLACK_API_BASE_URL={server.base_url} SLACK_BOT_TOKEN=", flush=True)
        seen = 0
        try:
            while True:
                time.sleep(0.5)
                with fake._lock:
                    fresh = fake.messages[seen:]
                    seen = len(fake.messages)
                for message in fresh:
                    if message.user == fake.bot_user_id and not message.streaming:
                        where = f"{message.channel}" + (f" thread {message.thread_ts}" if message.thread_ts else "")
                        body = "\n".join("    " + line for line in reply_text(message).splitlines())
                        print(f"[bot -> {where}]\n{body}", flush=True)
        except KeyboardInterrupt:
            print(f"\nstopped after {len(fake.calls)} Slack calls", flush=True)


if __name__ == "__main__":
    main()
