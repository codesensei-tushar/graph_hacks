"""Quillfern's Sales team, imported from examples/teams/quillfern-sales.toml.

It goes through the team importer like any other team file, so the demo graph is the proof
that the importer works. It is kept out of ``quillfern.py`` on purpose: tests that merge
their own Sales rows onto Quillfern seed the company without this team.
"""

from pathlib import Path

from src.seed import quillfern

SALES_FILE = Path(__file__).resolve().parents[2] / "examples" / "teams" / "quillfern-sales.toml"


def seed_sales():
    """Merge the Sales team into the current graph; returns the importer's ``ImportResult``."""
    from src.domain.onboarding.team_import import import_team

    return import_team(SALES_FILE.read_bytes(), quillfern.COMPANY["id"])
