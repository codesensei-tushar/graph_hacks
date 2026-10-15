"""Confirm FalkorDB is up and the company graph can be opened."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings
from src.db.falkordb_client import connect

if __name__ == "__main__":
    _, graph = connect()
    result = graph.query("MATCH (n) RETURN count(n) AS nodes")
    nodes = result.result_set[0][0] if result.result_set else 0
    print(f"connected to {settings.falkordb_host}:{settings.falkordb_port} graph={settings.falkordb_graph} nodes={nodes}")
