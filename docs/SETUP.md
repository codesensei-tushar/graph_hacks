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

`scripts/seed_graph.py` replaces the `company` graph with Company Brain itself: this repo's components, docs, open work, and decisions. It's for local development; never run it against production, because it deletes the whole graph first. The API listens on [http://localhost:8000](http://localhost:8000) and has no web page; you talk to it in Slack. With `SEED_ON_STARTUP=true` it also seeds on startup when the graph is empty.

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
| `SLACK_BOT_TOKEN` | empty | Bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | empty | Verifies requests from Slack |
| `SLACK_OWNER_ID` | empty | Slack member id of the installer (owner role) |
| `SLACK_ADMIN_IDS` | empty | Comma-separated member ids that start as admins |
| `LLM_API_KEY` | empty | Anthropic API key. Turns on triage, chime-in and memory extraction (see docs/SLACK.md) |
| `LLM_MODEL` | `claude-opus-5-5` | Model for every profile |

## How the pieces will connect

1. `docker-compose.yml` runs FalkorDB.
2. A seed script writes the company into the `company` graph.
3. FastAPI receives Slack events at `/slack/events` and button clicks at `/slack/interactions`.
4. Each agent tool runs one Cypher query and returns nodes and edges.
5. The bot replies in Slack with the answer and that path, reading only the scopes the asker may see.

Official references: [Getting started](https://docs.falkordb.com/getting-started), [Docker](https://docs.falkordb.com/operations/docker), [Cypher](https://docs.falkordb.com/cypher).
