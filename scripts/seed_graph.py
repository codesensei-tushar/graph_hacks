"""Load a company graph into FalkorDB.

The default is Company Brain itself. ``--company quillfern`` seeds the synthetic demo
company instead; ``scripts/replay_workspace.py`` then plays its chat and repo history on top.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import settings
from src.seed import company_brain, quillfern
from src.seed.seed import reset_and_seed

COMPANIES = {"company_brain": company_brain, "quillfern": quillfern}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", choices=sorted(COMPANIES), default="company_brain", help="which company to seed")
    args = parser.parse_args()
    count = reset_and_seed(COMPANIES[args.company])
    print(f"seeded {args.company} graph={settings.falkordb_graph} nodes={count}")
