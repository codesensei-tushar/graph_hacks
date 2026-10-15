# Company Brain

Hackathon project for Graph Hacks track 03. FalkorDB holds how a company is connected. An agent answers from that graph and shows the path behind each answer.

- What to build: [docs/PROBLEM.md](docs/PROBLEM.md)
- How to run the graph: [docs/SETUP.md](docs/SETUP.md)
- Slack app and permissions: [docs/SLACK.md](docs/SLACK.md)
- What's left for the product (agent, vector search): [docs/SLACK.md#whats-left-for-the-product-the-agent-and-vector-search](docs/SLACK.md#whats-left-for-the-product-the-agent-and-vector-search)
- Production deploy, CI/CD and operations: [docs/DEPLOY.md](docs/DEPLOY.md)

AI assistance: parts of this project were written with Claude Code (Anthropic), including the production infrastructure, deploy pipeline and their docs.

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/seed_graph.py
uvicorn src.main:app --reload
```

The interface is Slack. Create the app from [`slack_app/manifest.yaml`](slack_app/manifest.yaml) and follow [docs/SLACK.md](docs/SLACK.md). Health check: [http://localhost:8000/api/health](http://localhost:8000/api/health). Graph browser: [http://localhost:3000](http://localhost:3000).
