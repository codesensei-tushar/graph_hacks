"""Which company a graph serves, when it holds more than one.

Production's legacy graph holds Quillfern (``SEED_COMPANY``) and, merged next to it by
``POST /ops/seed?company=company_brain``, this repo's own seed, so its pull requests have services
to land on. The page, the proof and the graph stats still speak for the configured company: it is
the primary one when the graph holds it. Any other graph (an org's, a test's) holds one company,
or none, and the first by id is taken, as before.
"""

import logging

from src.core.config import settings

log = logging.getLogger(__name__)


def seed_company_id() -> str | None:
    """The Company id of the configured ``SEED_COMPANY``, or None when the name is no seed."""
    from src.seed.seed import company_module  # the seed imports knowledge.kinds; imported here to keep the order free

    try:
        return company_module(settings.seed_company).COMPANY["id"]
    except KeyError:
        log.warning("[companies] SEED_COMPANY=%r names no seed", settings.seed_company)
        return None


def companies(graph) -> list[tuple[str, str]]:
    """Every (id, name) Company in the graph, the primary one first, then by id."""
    rows = graph.ro_query(
        "MATCH (c:Company) RETURN c.id, c.name ORDER BY CASE WHEN c.id = $primary THEN 0 ELSE 1 END, c.id",
        {"primary": seed_company_id()},
    ).result_set
    return [(row[0], row[1]) for row in rows]


def primary_company(graph) -> tuple[str | None, str | None]:
    """The (id, name) the graph serves: the configured seed company's when present, else the first by id."""
    found = companies(graph)
    return found[0] if found else (None, None)
