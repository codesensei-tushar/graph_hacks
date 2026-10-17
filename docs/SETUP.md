# Setup

Local template for the Company Brain project. Nothing here is the agent yet. It gets FalkorDB running and records the stack we will build on.

## Tech stack

| Piece | Choice | Why |
| --- | --- | --- |
| Graph database | **FalkorDB** (Docker) | Required. Stores people, docs, tickets, and decisions as nodes and edges. Query language is Cypher. |
| Graph browser | FalkorDB Browser on port 3000 | Inspect the graph while building. Ships with the Docker image. |
| API and agent | Python 3.11+, FastAPI | Official `falkordb` client. Agent tools are Python functions that run Cypher. |
| Agent model | Any chat model with tool calling | The model picks a tool. The graph supplies the facts. |
| UI | Slack app (`slack_sdk`) | Question in, answer plus source path out. See [SLACK.md](SLACK.md). |
| Seed data | Cypher file loaded at startup | A fictional company so the demo does not depend on live integrations. |

Not in the first cut: a second database, a vector store, or a hosted graph. FalkorDB is the context layer.

## What you need installed

- Docker
- Python 3.11 or newer
- An API key for the chat model you pick (not required to boot the graph)

## Run FalkorDB

From the repo root:

```bash
docker compose up -d
```

That starts `falkordb/falkordb` with:

- Bolt / Redis protocol on `localhost:6379` (no password in this local template)
- Browser UI at [http://localhost:3000](http://localhost:3000)

Stop it with:

```bash
docker compose down
```

Check it from Python, then load the company and start the app:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/check_graph.py
python scripts/seed_graph.py
uvicorn src.main:app --reload
```

`scripts/seed_graph.py` replaces the `company` graph with Company Brain itself: this repo's components, docs, open work, and decisions. It's for local development; never run it that way against production, because it deletes the whole graph first. `python scripts/seed_graph.py --merge` refreshes the seed in place instead (every node and edge is merged by id), so memories, workspaces and GitHub history survive; a running instance does the same with `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://HOST/ops/seed`, which is how production picks up new seeded decisions before a recording. The API listens on [http://localhost:8000](http://localhost:8000) and has no web page; you talk to it in Slack. With `SEED_ON_STARTUP=true` it also seeds on startup when the graph is empty.

## Environment

Copy the example file and fill in the Slack values from [SLACK.md](SLACK.md):

```bash
cp .env.example .env
```

| Variable | Default | Used for |
| --- | --- | --- |
| `FALKORDB_HOST` | `localhost` | Graph host |
| `FALKORDB_PORT` | `6379` | Graph port |
| `FALKORDB_GRAPH` | `company` | Graph name inside the instance |
| `SEED_ON_STARTUP` | empty (off) | `true` seeds an empty graph on startup. Local development only. |
| `SEED_COMPANY` | `company_brain` | Which seed the startup seed and `POST /ops/seed` load: `company_brain` or `quillfern`. |
| `SLACK_BOT_TOKEN` | empty | Bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | empty | Verifies requests from Slack |
| `SLACK_OWNER_ID` | empty | Slack member id of the installer (owner role) |
| `SLACK_ADMIN_IDS` | empty | Comma-separated member ids that start as admins |
| `LLM_API_KEY` | empty | Anthropic API key. Turns on triage, chime-in and memory extraction (see docs/SLACK.md) |
| `LLM_MODEL` | `claude-haiku-5-5` | Model for every profile. Haiku only; the profile sets effort, not the model |

## Replay the synthetic company

This is the first thing to run after the graph is up: it needs no Slack app and no model key, and it is what the README's quickstart ends with.

`src/seed/quillfern.py` is a made-up company and `src/seed/quillfern_workspace.py` its scripted Slack history, demo steps, and GitHub repos. The replay plays that script into the app through the real routes, with real request signatures, so the graph ends up the way a used workspace would leave it.

Locally, with nothing but FalkorDB running:

```bash
python scripts/replay_workspace.py --graph company_demo
```

That starts a fake Slack Web API (`scripts/replay/slack_api.py`), points the app at it through `SLACK_API_BASE_URL`, seeds Quillfern and imports its Sales team from [`examples/teams/quillfern-sales.toml`](../examples/teams/quillfern-sales.toml), loads the repos, plays the chat history, the seven demo steps from [STRATEGY.md](STRATEGY.md) and the three Sales steps, and prints every reply. `--no-sales` leaves Sales out, to onboard it live ([ONBOARDING-TEAMS.md](ONBOARDING-TEAMS.md)). Nothing reaches Slack, and nothing calls the model unless you pass `--with-model`: without one, channel messages are kept as raw memories. `tests/integration/test_demo_replay.py` runs the same script and asserts the answers.

To load a real dev workspace before a live demo, run the app with `SEED_ON_STARTUP` off, seed Quillfern with `python scripts/seed_graph.py --company quillfern`, then post the history to the running instance, mapping the scripted ids to the real ones:

```bash
python scripts/replay_workspace.py --app-url http://localhost:8000 --team T0123 --bot U0BOT \
    --map CENGINEERING=C0ENG --map GPAYMENTS=G0PAY --map UMAYA=U0MAYA --map UARJUN=U0ARJUN ...
```

The instance verifies the events with `SLACK_SIGNING_SECRET` from `.env`, so the script reads the same file. Replies land in Slack. Add `--steps demo` to fire the seven steps too; Approve clicks happen in Slack by hand.

To rehearse that exact path without a workspace, serve the fake Slack API on its own and point a real instance at it:

```bash
python scripts/fake_slack.py --port 8899
SLACK_API_BASE_URL=http://127.0.0.1:8899/api/ SLACK_BOT_TOKEN= LLM_API_KEY= uvicorn src.main:app --port 8010
python scripts/replay_workspace.py --app-url http://127.0.0.1:8010 --team TQUILLFERN --bot UBRAIN \
    --fake-slack http://127.0.0.1:8899 --steps all
```

With `--fake-slack` the loader registers the scripted workspace in the instance's graph, records each message in the fake as if typed, waits for the bot's reply (a running instance answers in the background after the events route returns), clicks Approve on the cards, and prints every reply; the fake prints them too.

## How the pieces will connect

1. `docker-compose.yml` runs FalkorDB.
2. A seed script writes the company into the `company` graph.
3. FastAPI receives Slack events at `/slack/events` and button clicks at `/slack/interactions`.
4. Each agent tool runs one Cypher query and returns nodes and edges.
5. The bot replies in Slack with the answer and that path, reading only the scopes the asker may see.

Official references: [Getting started](https://docs.falkordb.com/getting-started), [Docker](https://docs.falkordb.com/operations/docker), [Cypher](https://docs.falkordb.com/cypher).
