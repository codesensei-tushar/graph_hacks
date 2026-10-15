"""Load the company graph into FalkorDB."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import reset_and_seed

if __name__ == "__main__":
    count = reset_and_seed()
    print(f"seeded company graph nodes={count}")
