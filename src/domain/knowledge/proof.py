"""Proof that the graph does the work.

The same catalogue and the same questions run twice: once against FalkorDB and once
with every graph read returning nothing (``dark_graph``). Each row records the tool,
both answers, and whether the capability collapsed. Questions go through the keyword
router and the catalogue, never the model, so the report is deterministic. The one
question about something the graph does not have refuses both ways; that row says so,
because the refusal must not depend on the graph being up.
"""

from dataclasses import asdict, dataclass

from src.db.falkordb_client import connect, dark_graph
from src.domain.knowledge import ask_service, catalogue
from src.domain.knowledge.dto import ToolResult

# Six read questions per seeded company: ownership, who knows, a decision, blast radius,
# a path question, and something not in the graph.
QUESTIONS: dict[str, list[str]] = {
    "quillfern": [
        "who owns billing",
        "who knows the memory layer",
        "why did we move the memory layer off postgres",
        "what breaks if auth goes down",
        "how is maya connected to the invoice worker",
        "who knows the payments gateway",
    ],
    "company-brain": [
        "who owns the memory layer",
        "who knows github ingest",
        "why falkordb",
        "what breaks if falkordb goes down",
        "how is the turn engine connected to the claude api",
        "who knows the payments gateway",
    ],
}
DEFAULT_SCOPES = ("org_shared",)


@dataclass(frozen=True)
class ProofOutcome:
    answer: str
    found: bool
    steps: int

    @property
    def answered(self) -> bool:
        return self.found and self.steps > 0


@dataclass(frozen=True)
class ProofRow:
    question: str
    tool: str
    with_graph: ProofOutcome
    without_graph: ProofOutcome
    collapsed: bool

    @property
    def verdict(self) -> str:
        if self.collapsed:
            return "collapsed"
        if not self.with_graph.found and not self.without_graph.found:
            return "refused both ways"
        return "unchanged"


@dataclass(frozen=True)
class ProofReport:
    company: str
    rows: list[ProofRow]
    collapsed: int
    total: int

    def to_dict(self) -> dict:
        return {
            "company": self.company,
            "collapsed": self.collapsed,
            "total": self.total,
            "rows": [{**asdict(row), "verdict": row.verdict} for row in self.rows],
        }

    def to_text(self) -> str:
        """A fixed-width table for a terminal."""
        header = ("question", "tool", "with graph", "without graph", "result")
        lines = [[row.question, row.tool, _cell(row.with_graph), _cell(row.without_graph), row.verdict] for row in self.rows]
        widths = [max(len(str(value)) for value in column) for column in zip(header, *lines)]
        fmt = "  ".join(f"{{:<{width}}}" for width in widths)
        out = [
            f"Proof that the graph does the work ({self.company}): {self.collapsed} of {self.total} capabilities collapse without FalkorDB.",
            "",
            fmt.format(*header),
            fmt.format(*("-" * width for width in widths)),
            *(fmt.format(*line) for line in lines),
        ]
        return "\n".join(out)


def _cell(outcome: ProofOutcome) -> str:
    state = "found" if outcome.found else "refused"
    return f"{state}, {outcome.steps} edge{'s' if outcome.steps != 1 else ''}"


def company_id() -> str:
    _, graph = connect()
    rows = graph.ro_query("MATCH (c:Company) RETURN c.id ORDER BY c.id LIMIT 1").result_set
    return rows[0][0] if rows else "quillfern"


def outcome_of(result: ToolResult) -> ProofOutcome:
    return ProofOutcome(answer=result.answer, found=result.found, steps=len(result.path))


def run_proof(scopes=DEFAULT_SCOPES, company: str | None = None) -> ProofReport:
    """Run every question with and without the graph. Read tools only; nothing is written."""
    company = company or company_id()
    questions = QUESTIONS.get(company) or QUESTIONS["quillfern"]
    read_scopes = list(scopes)
    rows: list[ProofRow] = []
    for question in questions:
        tool, args = ask_service.route(question)
        spec = catalogue.get_tool(tool)
        if spec is None or spec.needs_approval:
            continue
        with_graph = outcome_of(spec.run(args, read_scopes))
        with dark_graph():
            without_graph = outcome_of(spec.run(args, read_scopes))
        collapsed = with_graph.answered and not without_graph.answered
        rows.append(ProofRow(question=question, tool=tool, with_graph=with_graph, without_graph=without_graph, collapsed=collapsed))
    return ProofReport(company=company, rows=rows, collapsed=sum(row.collapsed for row in rows), total=len(rows))
