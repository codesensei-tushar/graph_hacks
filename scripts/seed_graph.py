"""Load a company graph into FalkorDB.

The default replaces the graph with Company Brain itself; ``--company quillfern`` seeds
the synthetic demo company instead, and ``scripts/replay_workspace.py`` then plays its
chat and repo history on top.

``--merge`` refreshes the seed without deleting anything: nodes and edges are merged by
id, so Slack and GitHub history survive. That is the only mode safe against a graph
that holds real history. A running instance does the same over ``POST /ops/seed``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import tenancy
from src.core.config import settings
from src.seed.seed import company_module, reset_and_seed, seed_company

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", choices=["company_brain", "quillfern"], default=settings.seed_company, help="which company to seed")
    parser.add_argument("--merge", action="store_true", help="merge into the existing graph instead of replacing it")
    args = parser.parse_args()
    # The legacy org's graph, said explicitly: TENANCY_WRITE_STRICT refuses a write with no org.
    with tenancy.org_context(tenancy.legacy_org()):
        company = company_module(args.company)
        if args.merge:
            report = seed_company(company)
            print(
                f"merged {args.company} graph={settings.falkordb_graph} nodes={report.nodes} "
                f"created nodes={report.nodes_created} edges={report.edges_created} properties set={report.properties_set}"
            )
        else:
            print(f"seeded {args.company} graph={settings.falkordb_graph} nodes={reset_and_seed(company)}")
