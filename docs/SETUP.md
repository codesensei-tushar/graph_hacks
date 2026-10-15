# Setup

Local template for the Company Brain project. Nothing here is the agent yet. It gets FalkorDB running and records the stack we will build on.

## Tech stack

| Piece | Choice | Why |
| --- | --- | --- |
| Graph database | **FalkorDB** (Docker) | Required. Stores people, docs, tickets, and decisions as nodes and edges. Query language is Cypher. |
| Graph browser | FalkorDB Browser on port 3000 | Inspect the graph while building. Ships with the Docker image. |
| API and agent | Python 3.11+, FastAPI | Official `falkordb` client. Agent tools are Python functions that run Cypher. |
| Agent model | Any chat model with tool calling | The model picks a tool. The graph supplies the facts. |
| UI | Small web page served by the API | Question in, answer plus source path out. |
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

Check it from Python:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/check_graph.py
```

You should see the `company` graph name and a node count. On a fresh database the count is `0` until seed data is loaded.

## Environment

Copy the example file and fill in a model key when the agent exists:

```bash
cp .env.example .env
```

| Variable | Default | Used for |
| --- | --- | --- |
| `FALKORDB_HOST` | `localhost` | Graph host |
| `FALKORDB_PORT` | `6379` | Graph port |
| `FALKORDB_GRAPH` | `company` | Graph name inside the instance |
| `LLM_API_KEY` | empty | Chat model, later |
| `LLM_MODEL` | empty | Model name, later |

## How the pieces will connect

1. `docker-compose.yml` runs FalkorDB.
2. A seed script writes the company into the `company` graph.
3. FastAPI exposes ask and ingest endpoints.
4. Each agent tool runs one Cypher query and returns nodes and edges.
5. The UI prints the answer and that path.

Official references: [Getting started](https://docs.falkordb.com/getting-started), [Docker](https://docs.falkordb.com/operations/docker), [Cypher](https://docs.falkordb.com/cypher).
