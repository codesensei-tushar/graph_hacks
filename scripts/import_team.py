"""Import a team file into the graph. docs/ONBOARDING-TEAMS.md explains the file.

    python scripts/import_team.py examples/teams/quillfern-sales.toml --company quillfern
    python scripts/import_team.py my-team.toml --check

The whole file is checked, against the graph too, before anything is written; a refused
file writes nothing and prints one line per problem. Writes merge by id and never delete,
so importing the same file twice writes nothing the second time. ``--check`` runs the
checks and writes nothing. A running instance does the same over ``POST /ops/import``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core import tenancy
from src.core.config import settings
from src.domain.onboarding.team_import import TeamFileError, check_team, describe, import_team
from src.seed.seed import company_module

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path, help="the team file (.toml)")
    parser.add_argument("--company", choices=["company_brain", "quillfern"], default=settings.seed_company, help="which company the team joins")
    parser.add_argument("--check", action="store_true", help="check the file and write nothing")
    args = parser.parse_args()
    # The legacy org's graph, said explicitly: TENANCY_WRITE_STRICT refuses a write with no org.
    with tenancy.org_context(tenancy.legacy_org()):
        company_id = company_module(args.company).COMPANY["id"]
        try:
            if args.check:
                print(f"checked {args.file} graph={settings.falkordb_graph}: {describe(check_team(args.file.read_bytes(), company_id))}; nothing written")
                sys.exit(0)
            result = import_team(args.file.read_bytes(), company_id)
        except TeamFileError as error:
            print(f"refused {args.file}; nothing was written:", file=sys.stderr)
            for problem in error.problems:
                print(f"  - {problem}", file=sys.stderr)
            sys.exit(1)
        report = result.report
        print(
            f"imported {describe(result.team)} into {args.company} graph={settings.falkordb_graph} nodes={report.nodes} "
            f"created nodes={report.nodes_created} edges={report.edges_created} properties set={report.properties_set}"
        )
