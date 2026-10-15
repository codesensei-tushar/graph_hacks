# Company Brain

Hackathon project for Graph Hacks track 03. FalkorDB holds how a company is connected. An agent answers from that graph and shows the path behind each answer.

- What to build: [docs/PROBLEM.md](docs/PROBLEM.md)
- How to run the graph: [docs/SETUP.md](docs/SETUP.md)

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/check_graph.py
```

Graph browser: [http://localhost:3000](http://localhost:3000)
