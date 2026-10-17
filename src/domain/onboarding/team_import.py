"""A team file: who is on a team, what it runs and what it decided, merged into the graph.

A team writes itself down in TOML (docs/ONBOARDING-TEAMS.md is the guide,
docs/templates/team.example.toml the blank template). ``parse_team`` reads the file into
the same company-shaped object every seed module is (``COMPANY``, ``PEOPLE``, ``TEAMS``,
``SERVICES``, ``DEPENDS_ON``, ``WORK``, ``DOCUMENTS``, ``TICKETS``, ``DECISIONS``,
``CHATS``), and ``import_team`` merges it with the seed's own loader: MERGE by id and never
delete, so importing the same file twice writes nothing the second time.

The file only ever supplies values. Each section maps to a label here, only the fields in
``FIELDS`` are copied into a fresh row, ``kind`` goes through ``kinds.normalize_kind``, and
every id is slugged, so nothing from the file reaches Cypher except as a query parameter.
A reference names a member or thing in the same file, by id or name, or something already
in the graph; anything else is refused rather than left dangling. A file that breaks a rule
is refused whole, one message per problem, before anything is written.
"""

import dataclasses
import difflib
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from src.db.falkordb_client import connect
from src.domain.knowledge.kinds import KIND_OF, NOT_DEPENDABLE, SERVICE_KINDS, normalize_kind
from src.seed.seed import SeedReport, seed_company

MAX_FILE_BYTES = 256 * 1024
_MAX_TEXT = 200
_MAX_SUMMARY = 1000

# What each section may say. Anything else is refused by name, so a typo is a message, not a silent drop.
FIELDS: dict[str, tuple[str, ...]] = {
    "team": ("id", "name", "summary"),
    "members": ("id", "name", "role", "email", "slack_id"),
    "runs": ("id", "name", "kind", "owner", "worked_on_by", "aliases", "summary", "depends_on"),
    "decisions": ("id", "name", "summary", "aliases", "proposer", "approver", "about", "based_on", "replaces", "decided_on"),
}
# Names an earlier draft of the template used, so an old file gets a pointer instead of a puzzle.
_RENAMED = {"owns": "runs", "worked_on": "worked_on_by", "supersedes": "replaces", "decided_at": "decided_on"}
# What a decision's ``based_on`` may point at when it is already in the graph.
_SOURCE_LABELS = ("Service", "Decision", "Document", "Ticket", "PullRequest")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{2,31}$")
_PLURAL = {"process": "processes", "policy": "policies", "person": "people"}


class TeamFileError(ValueError):
    """The file was refused; ``problems`` says why, one line each."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Found:
    """Something already in the graph that a reference named."""

    label: str
    id: str
    name: str
    kind: str | None = None


class GraphLookup:
    """Reads what a team file points at outside itself. Every value is a query parameter;
    labels come from the fixed tuples in this module."""

    def __init__(self, graph, scopes: list[str] | None = None):
        self.graph = graph
        self.scopes = list(scopes or ["org_shared"])

    def find(self, labels: tuple[str, ...], text: str) -> list[Found]:
        found = []
        for label in labels:
            kind = KIND_OF.format(var="n") if label == "Service" else "null"
            rows = self.graph.ro_query(
                f"""
                MATCH (n:{label})
                WHERE (n.id = $id OR toLower(n.name) = $name) AND coalesce(n.scope, 'org_shared') IN $scopes
                RETURN n.id, n.name, {kind}
                LIMIT 3
                """,
                {"id": slug(text), "name": text.strip().lower(), "scopes": self.scopes},
            ).result_set
            found += [Found(label, ident, name or ident, kind) for ident, name, kind in rows]
        return found

    def run_by(self, service_id: str) -> list[tuple[str, str]]:
        """The teams already running a service, as (id, name)."""
        rows = self.graph.ro_query("MATCH (t:Team)-[:RUNS]->(:Service {id: $id}) RETURN t.id, t.name", {"id": service_id}).result_set
        return [(ident, name or ident) for ident, name in rows]

    def slack_person(self, slack_user: str) -> str | None:
        """The person a Slack user is already linked to, if any."""
        rows = self.graph.ro_query("MATCH (:SlackUser {id: $id})-[:IS]->(p:Person) RETURN p.id LIMIT 1", {"id": slack_user}).result_set
        return rows[0][0] if rows else None

    def has_company(self, company_id: str) -> bool:
        return bool(self.graph.ro_query("MATCH (c:Company {id: $id}) RETURN count(c)", {"id": company_id}).result_set[0][0])


def slug(text: str) -> str:
    """An id from a name: lower case, letters, digits and dashes, at most 64 characters."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:64].strip("-")


def parse_team(text: str | bytes, company_id: str, lookup: GraphLookup | None = None) -> SimpleNamespace:
    """The company-shaped object for one team file, or ``TeamFileError`` with every problem found.

    Without ``lookup`` the file must be self-contained: a reference to anything not in the
    file is refused. With it, such a reference may name something already in the graph.
    """
    if isinstance(text, bytes):
        if len(text) > MAX_FILE_BYTES:
            raise TeamFileError([f"the file is {len(text)} bytes; the limit is {MAX_FILE_BYTES}"])
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise TeamFileError(["the file is not UTF-8 text"]) from None
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise TeamFileError([f"the file is more than {MAX_FILE_BYTES} bytes"])
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise TeamFileError([f"this is not valid TOML: {error}"]) from None
    return _Reader(data, company_id, lookup).read()


def parse_team_file(path: str | Path, company_id: str, lookup: GraphLookup | None = None) -> SimpleNamespace:
    return parse_team(Path(path).read_bytes(), company_id, lookup)


@dataclass
class ImportResult:
    team: SimpleNamespace
    report: SeedReport

    def to_dict(self) -> dict:
        return {"team": self.team.TEAM_ID, "contents": describe(self.team), "counts": contents(self.team), **dataclasses.asdict(self.report)}


def check_team(text: str | bytes, company_id: str) -> SimpleNamespace:
    """Everything ``import_team`` checks, against the graph, without writing anything."""
    _, graph = connect()
    return parse_team(text, company_id, GraphLookup(graph))


def import_team(text: str | bytes, company_id: str) -> ImportResult:
    """Check a team file against the graph, then merge it. Nothing is written unless every
    row and every reference checks out."""
    team = check_team(text, company_id)
    return ImportResult(team, seed_company(team))


def contents(team: SimpleNamespace) -> dict:
    """What a parsed file will create, counted: people, things by kind, decisions."""
    return {
        "people": len(team.PEOPLE),
        "runs": dict(Counter(service["kind"] for service in team.SERVICES)),
        "decisions": len(team.DECISIONS),
    }


def describe(team: SimpleNamespace) -> str:
    """One line, kinds in ``SERVICE_KINDS`` order: "Sales: 3 people, 1 system, 1 process, 1 account, 1 playbook, 2 decisions"."""
    counts = contents(team)
    parts = [_count(counts["people"], "person")]
    parts += [_count(counts["runs"][kind], kind) for kind in SERVICE_KINDS if kind in counts["runs"]]
    parts.append(_count(counts["decisions"], "decision"))
    return f"{team.TEAMS[0]['name']}: {', '.join(parts)}"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun if number == 1 else _PLURAL.get(noun, noun + 's')}"


def _millis(value) -> int:
    """Epoch milliseconds at UTC midnight for a TOML date or a "YYYY-MM-DD" string."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        value = date.fromisoformat(value.strip())
    if not isinstance(value, date):
        raise ValueError(value)
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1000)


class _Reader:
    """One pass over a parsed file. Collects every problem instead of stopping at the first."""

    def __init__(self, data: dict, company_id: str, lookup: GraphLookup | None):
        self.data = data
        self.company_id = company_id
        self.lookup = lookup
        self.problems: list[str] = []
        # Reference keys (the slug of an id or a name) to ids, per section.
        self.people: dict[str, str] = {}
        self.things: dict[str, str] = {}
        self.decisions: dict[str, str] = {}
        self.kind_of: dict[str, str] = {}

    def read(self) -> SimpleNamespace:
        # Merging the team onto a company the graph lacks would create a nameless one.
        if self.lookup is not None and not self.lookup.has_company(self.company_id):
            self.problems.append(f"the graph has no company '{self.company_id}' to add the team to; check SEED_COMPANY")
        for key in self.data:
            if key not in FIELDS:
                self._unknown(key, "the file", tuple(FIELDS))
        team = self._team()
        members = self._section("members")
        runs = self._section("runs")
        decisions = self._section("decisions")

        # Ids first, so a reference can name a row that comes after it.
        people = [self._member(row, where) for row, where in members]
        things = [self._thing(row, where) for row, where in runs]
        choices = [self._decision(row, where) for row, where in decisions]
        if team is None:
            raise TeamFileError(self.problems)
        self._slack_users_once(people, members)

        services, depends_on, work = [], [], []
        for (row, where), thing in zip(runs, things):
            if thing is None:
                continue
            services.append({**thing, "team": team["id"]})
            if self.lookup is not None:
                self._not_run_by_another_team(thing, team, where)
            owner = self._person(row.get("owner"), "owner", where) if row.get("owner") else None
            if owner:
                work.append((owner, "OWNS", thing["id"]))
            for person in self._people(row, "worked_on_by", where):
                if person != owner:
                    work.append((person, "WORKED_ON", thing["id"]))
            parents = self._list(row, "depends_on", where)
            if parents and self._account(thing["id"]):
                self.problems.append(f"{where} is an account, and an account depends on nothing; leave depends_on off it")
                parents = []
            for other in parents:
                parent = self._thing_ref(other, "depends_on", where)
                if parent is None:
                    continue
                if parent == thing["id"]:
                    self.problems.append(f"{where} depends on itself")
                elif self._account(parent):
                    self.problems.append(f"depends_on in {where}: '{other}' is an account, and nothing depends on an account")
                else:
                    depends_on.append((thing["id"], parent))

        rows = []
        for (row, where), decision in zip(decisions, choices):
            if decision is None:
                continue
            # The team page finds a team's decisions through ABOUT, so every decision needs one.
            reported = len(self.problems)
            if not (named := self._list(row, "about", where)) and len(self.problems) == reported:
                self.problems.append(f"{where} needs about: the things in [[runs]] it is about")
            about = [ident for text in named if (ident := self._thing_ref(text, "about", where))]
            decision["based_on"] = [ident for text in self._list(row, "based_on", where) if (ident := self._source_ref(text, where))]
            for role in ("proposer", "approver"):
                if row.get(role):
                    decision[role] = self._person(row[role], role, where)
            if row.get("replaces"):
                older = self._ref(row["replaces"], self.decisions, ("Decision",), "decision", "replaces", where)
                if older == decision["id"]:
                    self.problems.append(f"{where} replaces itself")
                elif older:
                    decision["supersedes"] = older
            if row.get("decided_on") is not None:
                try:
                    decision["decided_at"] = _millis(row["decided_on"])
                except ValueError:
                    self.problems.append(f"decided_on in {where} should be a date like \"2026-09-01\"")
            rows.append({key: value for key, value in decision.items() if value not in (None, [])} | {"about": about})

        if self.problems:
            raise TeamFileError(self.problems)
        return SimpleNamespace(
            TEAM_ID=team["id"],
            COMPANY={"id": self.company_id},
            PEOPLE=[person for person, _ in people if person],
            TEAMS=[{**team, "members": [person["id"] for person, _ in people if person]}],
            SERVICES=services,
            DEPENDS_ON=list(dict.fromkeys(depends_on)),
            WORK=list(dict.fromkeys(work)),
            SLACK_USERS=[(slack_user, person["id"]) for person, slack_user in people if person and slack_user],
            DOCUMENTS=[],
            TICKETS=[],
            DECISIONS=rows,
            CHATS=[],
        )

    # --- rows -------------------------------------------------------------------------------

    def _team(self) -> dict | None:
        row = self.data.get("team")
        if not isinstance(row, dict):
            self.problems.append("the file needs a [team] section with the team's name")
            return None
        self._fields(row, "team", "[team]")
        name = self._text(row, "name", "[team]", required=True)
        ident = self._id(row, "[team]")
        if not (name and ident):
            return None
        return {"id": ident, "name": name, "summary": self._text(row, "summary", "[team]", limit=_MAX_SUMMARY)}

    def _section(self, section: str) -> list[tuple[dict, str]]:
        """The rows of an array-of-tables section, each with how a message names it."""
        value = self.data.get(section, [])
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            self.problems.append(f"write each entry of {section} as its own [[{section}]] block")
            return []
        rows = []
        for number, row in enumerate(value, 1):
            name = row.get("name")
            where = f"[[{section}]] '{name}'" if isinstance(name, str) and name.strip() else f"[[{section}]] entry {number}"
            self._fields(row, section, where)
            rows.append((row, where))
        return rows

    def _member(self, row: dict, where: str) -> tuple[dict | None, str | None]:
        name, ident = self._text(row, "name", where, required=True), self._id(row, where)
        if not (name and ident):
            return None, None
        if not self._claim(self.people, ident, name, "member", where):
            return None, None
        email = self._text(row, "email", where)
        if email and "@" not in email:
            self.problems.append(f"email in {where} does not look like an email address")
        slack_user = self._text(row, "slack_id", where)
        if slack_user and not _SLACK_USER.match(slack_user):
            self.problems.append(f"slack_id in {where} should look like U012ABCDEF (Slack profile, ⋮, Copy member ID)")
            slack_user = None
        person = {"id": ident, "name": name, "role": self._text(row, "role", where), "email": email}
        return {key: value for key, value in person.items() if value is not None}, slack_user

    def _thing(self, row: dict, where: str) -> dict | None:
        name, ident = self._text(row, "name", where, required=True), self._id(row, where)
        try:
            kind = normalize_kind(row.get("kind"))
        except ValueError:
            self.problems.append(f"kind in {where} is {row.get('kind')!r}; use one of {', '.join(SERVICE_KINDS)}")
            kind = None
        # A bad kind still claims the id, so references to the entry do not pile up false misses.
        if not (name and ident) or not self._claim(self.things, ident, name, "thing", where) or kind is None:
            return None
        self.kind_of[ident] = kind
        thing = {"id": ident, "name": name, "kind": kind, "aliases": self._aliases(row, where), "summary": self._text(row, "summary", where, limit=_MAX_SUMMARY)}
        return {key: value for key, value in thing.items() if value is not None}

    def _decision(self, row: dict, where: str) -> dict | None:
        name = self._text(row, "name", where, required=True)
        ident = self._id(row, where, prefix="decision-")
        if not (name and ident) or not self._claim(self.decisions, ident, name, "decision", where):
            return None
        return {"id": ident, "name": name, "summary": self._text(row, "summary", where, limit=_MAX_SUMMARY), "aliases": self._aliases(row, where)}

    def _slack_users_once(self, people: list, members: list) -> None:
        """One Slack user is one person: not two members here, and not someone else already."""
        seen: dict[str, str] = {}
        for (person, slack_user), (_, where) in zip(people, members):
            if not (person and slack_user):
                continue
            if slack_user in seen:
                self.problems.append(f"{where} has the same slack_id as '{seen[slack_user]}'")
            seen[slack_user] = person["id"]
            linked = self.lookup.slack_person(slack_user) if self.lookup is not None else None
            if linked and linked != person["id"]:
                self.problems.append(f"slack_id in {where} already belongs to '{linked}' in Company Brain")

    def _not_run_by_another_team(self, thing: dict, team: dict, where: str) -> None:
        others = [name for ident, name in self.lookup.run_by(thing["id"]) if ident != team["id"]]
        if others:
            self.problems.append(
                f"{where}: '{thing['id']}' is already run by {', '.join(others)}. To lean on it, list it under depends_on; "
                "to add a separate one, give this entry its own id"
            )

    # --- references -------------------------------------------------------------------------

    def _person(self, text, field: str, where: str) -> str | None:
        return self._ref(text, self.people, ("Person",), "member", field, where)

    def _people(self, row: dict, field: str, where: str) -> list[str]:
        return [ident for text in self._list(row, field, where) if (ident := self._person(text, field, where))]

    def _thing_ref(self, text: str, field: str, where: str) -> str | None:
        return self._ref(text, self.things, ("Service",), "thing", field, where)

    def _source_ref(self, text: str, where: str) -> str | None:
        local = {**self.things, **self.decisions}
        return self._ref(text, local, _SOURCE_LABELS, "thing, decision or document", "based_on", where)

    def _ref(self, text, local: dict[str, str], labels: tuple[str, ...], what: str, field: str, where: str) -> str | None:
        if not isinstance(text, str) or not text.strip():
            self.problems.append(f"{field} in {where} should be a name in quotes")
            return None
        if (key := slug(text)) in local:
            return local[key]
        if self.lookup is None:
            self.problems.append(f"{field} in {where}: '{text}' is not a {what} in this file")
            return None
        found = self.lookup.find(labels, text)
        ids = list(dict.fromkeys(item.id for item in found))
        if not ids:
            self.problems.append(f"{field} in {where}: no {what} named '{text}' in this file or in Company Brain")
            return None
        if len(ids) > 1:
            self.problems.append(f"{field} in {where}: '{text}' matches more than one thing ({', '.join(ids)}); use its id")
            return None
        if found[0].kind:
            self.kind_of[ids[0]] = found[0].kind
        return ids[0]

    def _account(self, ident: str) -> bool:
        return self.kind_of.get(ident) in NOT_DEPENDABLE

    # --- values -----------------------------------------------------------------------------

    def _fields(self, row: dict, section: str, where: str) -> None:
        for key in row:
            if key not in FIELDS[section]:
                self._unknown(key, where, FIELDS[section])

    def _unknown(self, key: str, where: str, allowed: tuple[str, ...]) -> None:
        renamed = _RENAMED.get(str(key))
        close = [renamed] if renamed in allowed else difflib.get_close_matches(str(key), allowed, n=1)
        hint = f"did you mean '{close[0]}'?" if close else f"expected one of {', '.join(allowed)}"
        self.problems.append(f"unknown field '{str(key)[:40]}' in {where}; {hint}")

    def _claim(self, index: dict[str, str], ident: str, name: str, what: str, where: str) -> bool:
        """Record a row's id and name as reference keys; a second row with the same id is refused."""
        if ident in index.values():
            self.problems.append(f"{where}: two entries have the id '{ident}'; give one of them its own id")
            return False
        index[ident] = ident
        index.setdefault(slug(name), ident)
        return True

    def _id(self, row: dict, where: str, prefix: str = "") -> str | None:
        raw = row.get("id")
        if raw is not None and not isinstance(raw, str):
            self.problems.append(f"id in {where} should be text in quotes")
            return None
        if isinstance(raw, str) and raw.strip():
            ident = slug(raw)
        elif isinstance(row.get("name"), str):
            ident = slug(f"{prefix}{row['name']}")
        else:
            return None
        if not ident:
            self.problems.append(f"{where} needs a name or id with at least one letter or digit")
            return None
        return ident

    def _text(self, row: dict, key: str, where: str, required: bool = False, limit: int = _MAX_TEXT) -> str | None:
        value = row.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                self.problems.append(f"{where} needs a {key}")
            return None
        if not isinstance(value, str):
            self.problems.append(f"{key} in {where} should be text in quotes")
            return None
        value = " ".join(value.split())
        if len(value) > limit:
            self.problems.append(f"{key} in {where} is {len(value)} characters; the limit is {limit}")
            return None
        return value

    def _list(self, row: dict, key: str, where: str) -> list[str]:
        value = row.get(key)
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.problems.append(f'{key} in {where} should be a list of names in quotes, like ["Renewals", "HubSpot"]')
            return []
        return [item.strip() for item in value if item.strip()]

    def _aliases(self, row: dict, where: str) -> str | None:
        value = row.get("aliases")
        if value is None:
            return None
        names = [part.strip() for part in value.split(",")] if isinstance(value, str) else value
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            self.problems.append(f'aliases in {where} should be names in quotes, like ["renewals", "renewal process"]')
            return None
        joined = ", ".join(" ".join(name.split()) for name in names if name.strip())
        if len(joined) > _MAX_SUMMARY:
            self.problems.append(f"aliases in {where} are {len(joined)} characters; the limit is {_MAX_SUMMARY}")
            return None
        return joined or None
