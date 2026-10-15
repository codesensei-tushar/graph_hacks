"""Confirm FalkorDB is up and the company graph can be opened."""

import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

host = os.getenv("FALKORDB_HOST", "localhost")
port = int(os.getenv("FALKORDB_PORT", "6379"))
name = os.getenv("FALKORDB_GRAPH", "company")

db = FalkorDB(host=host, port=port)
graph = db.select_graph(name)
result = graph.query("MATCH (n) RETURN count(n) AS nodes")
nodes = result.result_set[0][0] if result.result_set else 0
print(f"connected to {host}:{port} graph={name} nodes={nodes}")
