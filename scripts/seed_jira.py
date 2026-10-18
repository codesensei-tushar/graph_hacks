"""Seed a Jira Cloud site with synthetic Quillfern projects and issues for the Jira connector.

    .venv/bin/python scripts/seed_jira.py --env-file ~/.config/company-brain/connectors.env           # plan only
    .venv/bin/python scripts/seed_jira.py --env-file ~/.config/company-brain/connectors.env --apply   # write it

Reads CONNECTOR_JIRA_BASE_URL, CONNECTOR_JIRA_EMAIL and CONNECTOR_JIRA_API_TOKEN from the
environment, or from ``--env-file`` (values already in the environment win). Nothing it
reads is printed except display names, project keys and issue keys.

Everything here is invented (the hackathon's synthetic-data rule) and lines up with the
Quillfern seed (``src/seed/quillfern.py``): two company-managed projects whose components
are named after Quillfern's services, so synced tickets land ``ABOUT`` those services. Issues
are assigned round-robin over the site's active people, a few left unassigned; the reporter
is the token's account. Issues are assigned last, with ``notifyUsers=false`` (the account must be a
Jira admin, which creating projects needs anyway), so nobody is emailed about synthetic work.

Dry run unless ``--apply``. Idempotent: a project it made carries the project property
``company-brain-seed``, and an issue it made the labels ``brain-seed`` and ``brain-seed-<id>``,
so a second run creates only what is missing. It never deletes anything, and it changes only
what it created: a project key that exists without its property stops the run before any write.
"""

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.connectors.base import RetryLater  # noqa: E402
from src.connectors.http import HttpError  # noqa: E402
from src.connectors.jira.client import JiraClient, site_host  # noqa: E402

MARKER_PROPERTY = "company-brain-seed"
LABEL = "brain-seed"
TEMPLATE = "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic"  # company-managed: has components
NOTE = "Synthetic data for Company Brain, created by scripts/seed_jira.py (brain-seed)."

PROJECTS = [
    {"key": "PAY", "name": "Quillfern Payments", "components": ["Billing Service", "Invoice Worker"]},
    {
        "key": "PLAT",
        "name": "Quillfern Platform",
        "components": ["Auth Service", "API Gateway", "Notification Service", "Postgres", "Search Service", "Memory Layer", "Graph Store"],
    },
]

# Quillfern's people, only to spread the work: each maps onto one of the site's real accounts.
PERSONAS = ["arjun", "maya", "lena", "sara", "kofi", "tomas"]

# (seed id, type, title, components, status, persona or None, priority, parent seed id, description)
ISSUES = [
    ("pay-01", "Epic", "Renewal retries without double charges", ["Billing Service"], "in_progress", "arjun", "High", None,
     "Retried renewal webhooks must never charge a customer twice."),
    ("pay-02", "Story", "Make renewal webhook retries idempotent", ["Billing Service"], "in_progress", "arjun", "High", "pay-01",
     "The billing service retries a failed renewal webhook once, keyed on the renewal id."),
    ("pay-03", "Bug", "Annual plan renewal charged twice after a retried webhook", ["Billing Service"], "done", "lena", "Highest", "pay-01",
     "A retried renewal webhook created a second charge for the same period."),
    ("pay-04", "Task", "Add an idempotency key column to the charges table", ["Billing Service"], "done", "arjun", "Medium", "pay-01",
     "Schema change so a charge can be looked up by the renewal that caused it."),
    ("pay-05", "Story", "Alert when a renewal retry fails twice", ["Billing Service"], "todo", "lena", "Medium", "pay-01",
     "Page the payments on-call when the single retry also fails."),
    ("pay-06", "Bug", "Proration rounds down on mid-cycle plan upgrades", ["Billing Service"], "todo", "tomas", "Medium", None,
     "Upgrading mid-cycle undercharges by up to one cent per seat."),
    ("pay-07", "Task", "Read subscription status through the gateway cache", ["Billing Service"], "todo", None, "Low", None,
     "Most status reads can be served from the API gateway's cache."),
    ("pay-08", "Story", "Show failed renewal reasons on the billing page", ["Billing Service"], "in_progress", "lena", "Medium", None,
     "Customers see why a renewal failed and how to fix their card."),
    ("pay-09", "Epic", "Invoice PDFs in every supported currency", ["Invoice Worker"], "in_progress", "lena", "Medium", None,
     "The invoice worker renders correct PDFs for all twelve billing currencies."),
    ("pay-10", "Story", "Render JPY invoices without decimal places", ["Invoice Worker"], "done", "lena", "Medium", "pay-09",
     "Zero-decimal currencies print whole amounts."),
    ("pay-11", "Bug", "Invoice PDF footer overlaps long company addresses", ["Invoice Worker"], "in_progress", "kofi", "Low", "pay-09",
     "Addresses over four lines run into the footer."),
    ("pay-12", "Task", "Add VAT numbers to EU invoices", ["Invoice Worker"], "todo", "arjun", "High", "pay-09",
     "EU customers need their VAT number on every invoice."),
    ("pay-13", "Story", "Send the receipt email from the invoice worker", ["Invoice Worker"], "done", "lena", "Medium", None,
     "Receipts go out when the invoice is rendered, not in the nightly batch."),
    ("pay-14", "Bug", "Invoice worker retries a failed PDF render forever", ["Invoice Worker"], "todo", "sara", "High", None,
     "A render that fails on bad input is retried without a limit."),
    ("pay-15", "Task", "Backfill missing invoice numbers for September", ["Invoice Worker"], "done", "tomas", "Low", None,
     "Forty-one September invoices were issued without a number."),
    ("pay-16", "Story", "Refunds write a credit note", ["Billing Service", "Invoice Worker"], "todo", "arjun", "Medium", None,
     "Every refund produces a credit note PDF next to the original invoice."),
    ("pay-17", "Task", "Document refund retries in the billing runbook", [], "todo", None, "Low", None,
     "The billing runbook should say how a refund is retried and by whom."),
    ("plat-01", "Epic", "Session refresh survives token rotation", ["Auth Service"], "in_progress", "maya", "High", None,
     "Rotating refresh tokens must not log anyone out."),
    ("plat-02", "Bug", "Clients holding an old refresh token get logged out", ["Auth Service"], "in_progress", "maya", "Highest", "plat-01",
     "After a rotation, a client that refreshes with the previous token is signed out."),
    ("plat-03", "Story", "Rotate signing keys without a deploy", ["Auth Service"], "todo", "sara", "Medium", "plat-01",
     "Signing keys come from the key store and rotate on a schedule."),
    ("plat-04", "Task", "Expire sessions after 30 days idle", ["Auth Service"], "done", "maya", "Medium", "plat-01",
     "Idle sessions end after thirty days."),
    ("plat-05", "Story", "Rate limits per workspace at the gateway", ["API Gateway"], "in_progress", "maya", "Medium", None,
     "One busy workspace cannot starve the others."),
    ("plat-06", "Bug", "Gateway returns 502 when billing is slow", ["API Gateway"], "todo", "sara", "High", None,
     "Slow billing responses surface as 502s instead of a retryable 503."),
    ("plat-07", "Task", "Gateway timeouts for subscription reads", ["API Gateway"], "done", "maya", "Low", None,
     "Subscription reads time out after two seconds at the gateway."),
    ("plat-08", "Story", "Batch digest emails per workspace", ["Notification Service"], "todo", "lena", "Low", None,
     "One digest email per workspace per day instead of one per event."),
    ("plat-09", "Bug", "Duplicate password reset emails", ["Notification Service"], "done", "lena", "High", None,
     "A double click on reset sent two emails with different links."),
    ("plat-10", "Task", "Upgrade Postgres to 17 on the primary", ["Postgres"], "in_progress", "sara", "High", None,
     "Upgrade the primary and both replicas; schema changes wait for it."),
    ("plat-11", "Bug", "Connection pool exhausted during the nightly invoice batch", ["Postgres"], "todo", "sara", "Highest", None,
     "The nightly batch opens more connections than the pool allows."),
    ("plat-12", "Task", "Point-in-time recovery drill", ["Postgres"], "done", "sara", "Medium", None,
     "Restore yesterday's state into staging and time it."),
    ("plat-13", "Epic", "Memory layer on the graph store", ["Memory Layer"], "in_progress", "kofi", "High", None,
     "Notebook links move from Postgres join tables to the graph store."),
    ("plat-14", "Story", "Migrate notebook links from Postgres join tables", ["Memory Layer"], "in_progress", "kofi", "High", "plat-13",
     "Copy every link, verify counts, then switch reads."),
    ("plat-15", "Bug", "Linked-note lookups time out past five hops", ["Memory Layer"], "done", "kofi", "High", "plat-13",
     "Five-hop lookups took more than eight seconds on Postgres."),
    ("plat-16", "Task", "Graph store backups every six hours", ["Graph Store"], "todo", "sara", "Medium", "plat-13",
     "Snapshots every six hours, kept for two weeks."),
    ("plat-17", "Story", "Search results include linked notes", ["Search Service"], "todo", "kofi", "Medium", None,
     "A search hit shows the notes it links to."),
    ("plat-18", "Bug", "Search index lags notebook edits by minutes", ["Search Service"], "in_progress", None, "Medium", None,
     "Edits take up to five minutes to show in search."),
]

# (blocker, blocked): "blocker blocks blocked".
BLOCKS = [
    ("pay-04", "pay-02"),
    ("plat-10", "pay-04"),
    ("pay-14", "plat-11"),
    ("plat-16", "plat-14"),
    ("plat-14", "plat-17"),
    ("plat-02", "plat-03"),
]

# Status category a seeded status lands in.
CATEGORY = {"todo": "new", "in_progress": "indeterminate", "done": "done"}


def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _call(fn, *args, **kwargs):
    """Run one Jira call, waiting out a long rate limit a few times (JsonClient raises RetryLater for it)."""
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except RetryLater as wait:
            if attempt == 3:
                raise
            print(f"  rate limited; waiting {int(wait.seconds)}s")
            time.sleep(wait.seconds)


def _humans(users: list[dict]) -> list[dict]:
    return sorted((user for user in users if user.get("accountType") == "atlassian" and user.get("active")), key=lambda user: user.get("displayName") or "")


class Seeder:
    def __init__(self, client: JiraClient, apply: bool):
        self.client = client
        self.apply = apply
        self.http = client.http
        self.keys: dict[str, str] = {}  # seed id -> issue key
        self.meta: dict[str, tuple] = {}  # project key -> (issue type ids, fields per type)
        self.created: Counter = Counter()

    # --- reads ------------------------------------------------------------------------

    def ours(self, key: str) -> bool | None:
        """True: the project exists and this script made it. False: it exists and is someone else's. None: no project."""
        if _call(self.client.project, key) is None:
            return None
        try:
            _call(self.http.get, f"/rest/api/3/project/{key}/properties/{MARKER_PROPERTY}")
            return True
        except HttpError as error:
            if error.status == 404:
                return False
            raise

    def seeded_issues(self, key: str) -> dict[str, dict]:
        found = {}
        for issue in _call(lambda: list(self.client.search(f'project = "{key}" AND labels = "{LABEL}" ORDER BY created ASC', limit=500))):
            for label in (issue.get("fields") or {}).get("labels") or []:
                if label.startswith(f"{LABEL}-"):
                    found[label.removeprefix(f"{LABEL}-")] = issue
        return found

    # --- the run ----------------------------------------------------------------------

    def run(self) -> int:
        me = _call(self.client.myself)
        print(f"Jira site {site_host(self.client.base_url)} as {me.get('displayName')}")
        state = {project["key"]: self.ours(project["key"]) for project in PROJECTS}
        foreign = [key for key, ours in state.items() if ours is False]
        if foreign:
            print(f"Stopping: project {', '.join(foreign)} already exists and was not created by this script. Nothing was written.")
            return 1
        others = [project["key"] for project in _call(self.http.get, "/rest/api/3/project/search", {"maxResults": 50}).get("values") or [] if project["key"] not in state]
        if others:
            print(f"Other projects on the site, left alone: {', '.join(sorted(others))}")

        people = _humans(_call(self.http.get, "/rest/api/3/users/search", {"maxResults": 100}) or [])
        if not people:
            print("Stopping: the site has no active people to assign issues to. Nothing was written.")
            return 1
        account = {persona: people[index % len(people)] for index, persona in enumerate(PERSONAS)}
        load = Counter(account[row[5]]["displayName"] for row in ISSUES if row[5])
        print(f"Assignees, round-robin over {len(people)} active people: " + ", ".join(f"{name} {count}" for name, count in sorted(load.items())))

        existing: dict[str, dict] = {}
        for project in PROJECTS:
            self.project(project, state[project["key"]], me)
            if state[project["key"]]:
                existing.update(self.seeded_issues(project["key"]))
        self.keys = {seed: issue["key"] for seed, issue in existing.items()}

        # Parents first, so a child can name its epic.
        for row in sorted(ISSUES, key=lambda row: row[7] is not None):
            self.issue(row, existing.get(row[0]), account)
        self.links()
        self.summary()
        return 0

    def project(self, project: dict, ours: bool | None, me: dict) -> None:
        key = project["key"]
        if ours is None:
            print(f"{'Creating' if self.apply else 'Would create'} project {key} “{project['name']}” (company-managed kanban, lead {me.get('displayName')})")
            self.created["projects"] += 1
            if self.apply:
                _call(
                    self.http.post,
                    "/rest/api/3/project",
                    {
                        "key": key, "name": project["name"], "projectTypeKey": "software", "projectTemplateKey": TEMPLATE,
                        "leadAccountId": me["accountId"], "assigneeType": "UNASSIGNED", "description": NOTE,
                    },
                )
                _call(self.http.request, "PUT", f"/rest/api/3/project/{key}/properties/{MARKER_PROPERTY}", body={"marker": LABEL, "by": "scripts/seed_jira.py"})
        else:
            print(f"Project {key} exists and was created by this script")
        have = {component["name"] for component in _call(self.client.components, key)} if (ours or self.apply) else set()
        missing = [name for name in project["components"] if name not in have]
        if missing:
            print(f"  {'Creating' if self.apply else 'Would create'} components: {', '.join(missing)}")
            self.created["components"] += len(missing)
        for name in missing if self.apply else []:
            _call(self.http.post, "/rest/api/3/component", {"project": key, "name": name, "description": f"Quillfern's {name}. {NOTE}"})

    def _fields(self, key: str) -> tuple[dict[str, str], dict[str, set[str]], dict[str, set[str]]]:
        """The project's issue type ids by name, the fields each type's create screen takes, and the
        required text fields with no default (an older site's "Epic Name"), which get the title."""
        if key not in self.meta:
            types = _call(self.http.get, f"/rest/api/3/issue/createmeta/{key}/issuetypes", {"maxResults": 50}) or {}
            ids = {item["name"]: item["id"] for item in types.get("issueTypes") or [] if not item.get("subtask")}
            fields, required = {}, {}
            for name, type_id in ids.items():
                meta = (_call(self.http.get, f"/rest/api/3/issue/createmeta/{key}/issuetypes/{type_id}", {"maxResults": 100}) or {}).get("fields") or []
                fields[name] = {item.get("fieldId") for item in meta}
                required[name] = {
                    item.get("fieldId") for item in meta
                    if item.get("required") and not item.get("hasDefaultValue") and (item.get("schema") or {}).get("type") == "string"
                    and item.get("fieldId") not in ("summary", "description")
                }
            self.meta[key] = (ids, fields, required)
        return self.meta[key]

    def _create(self, body: dict) -> dict:
        """POST the issue. A 400 that names an optional field (parent, priority, components) is retried once without it."""
        try:
            return _call(self.http.post, "/rest/api/3/issue", {"fields": body})
        except HttpError as error:
            dropped = [name for name in ("parent", "priority", "components") if name in body and f'"{name}"' in str(error)]
            if error.status != 400 or not dropped:
                raise
            print(f"  Jira refused {', '.join(dropped)} for “{body['summary']}”; creating it without")
            return _call(self.http.post, "/rest/api/3/issue", {"fields": {k: v for k, v in body.items() if k not in dropped}})

    def issue(self, row: tuple, existing: dict | None, account: dict) -> None:
        seed, kind, title, components, status, persona, priority, parent, description = row
        project = seed.split("-")[0].upper()
        want = CATEGORY[status]
        if existing is not None:
            current = (((existing.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key")
            if current != want:
                self.created["status moves"] += 1
                if self.apply:
                    self.transition(existing["key"], want)
            return
        self.created["issues"] += 1
        self.created[f"type {kind}"] += 1
        self.created[f"status {status}"] += 1
        self.created["unassigned"] += persona is None
        who = account[persona]["displayName"] if persona else "unassigned"
        if not self.apply:
            print(f"  would create {project} {kind.lower()} “{title}” ({', '.join(components) or 'no component'}; {status.replace('_', ' ')}; {who})")
            return
        ids, fields, required = self._fields(project)
        kind = kind if kind in ids else "Task"
        allowed = fields.get(kind, set())
        body: dict = {
            "project": {"key": project}, "summary": title, "issuetype": {"id": ids[kind]},
            "description": _adf(f"{description} {NOTE}"), "labels": [LABEL, f"{LABEL}-{seed}"],
        }
        body.update({field_id: title for field_id in required.get(kind, set())})
        if components and "components" in allowed:
            body["components"] = [{"name": name} for name in components]
        if "priority" in allowed:
            body["priority"] = {"name": priority}
        if parent and parent in self.keys:
            # Not always listed on the create screen even where Jira takes it; _create drops it on a 400.
            body["parent"] = {"key": self.keys[parent]}
        created = self._create(body)
        self.keys[seed] = created["key"]
        print(f"  created {created['key']} “{title}” ({who})")
        if want != "new":
            self.transition(created["key"], want)
        if persona:
            # Assigned last and quietly, so the site's people get no email about synthetic work.
            _call(
                self.http.request, "PUT", f"/rest/api/3/issue/{created['key']}",
                params={"notifyUsers": "false"}, body={"fields": {"assignee": {"accountId": account[persona]["accountId"]}}},
            )

    def transition(self, key: str, category: str) -> None:
        options = (_call(self.http.get, f"/rest/api/3/issue/{key}/transitions") or {}).get("transitions") or []
        match = next((option for option in options if ((option.get("to") or {}).get("statusCategory") or {}).get("key") == category), None)
        if match is None:
            print(f"  {key}: no transition into the {category} category; left as it is")
            return
        _call(self.http.post, f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        print(f"  {key} -> {match['to'].get('name')}")

    def _blocks(self, key: str) -> dict[str, dict]:
        """``key``'s Blocks links by the other issue's key, as Jira reads them back from ``key``:
        ``("blocks", link)`` when ``key`` blocks it, ``("blocked by", link)`` when it blocks ``key``."""
        found = {}
        for link in _call(self.client.issue_links, key):
            if (link.get("type") or {}).get("name") != "Blocks":
                continue
            if link.get("outwardIssue"):
                found[link["outwardIssue"]["key"]] = ("blocks", link)
            elif link.get("inwardIssue"):
                found[link["inwardIssue"]["key"]] = ("blocked by", link)
        return found

    def links(self) -> None:
        for blocker, blocked in BLOCKS:
            if blocker in self.keys and blocked in self.keys and self._blocks(self.keys[blocker]).get(self.keys[blocked], ("",))[0] == "blocks":
                continue
            self.created["links"] += 1
            self.created["cross-project links"] += blocker.split("-")[0] != blocked.split("-")[0]
            if self.apply:
                self._link(self.keys[blocker], self.keys[blocked])

    def _link(self, blocker: str, blocked: str) -> None:
        """``blocker`` blocks ``blocked``, checked by reading it back from ``blocker``. Jira's create body names
        the two sides confusingly, so if the first link comes back reversed, that link (made just now, by this
        script) is removed and every link from then on is written the other way round."""
        sides = ("outwardIssue", "inwardIssue") if getattr(self, "_swapped", False) else ("inwardIssue", "outwardIssue")
        _call(self.http.post, "/rest/api/3/issueLink", {"type": {"name": "Blocks"}, sides[0]: {"key": blocker}, sides[1]: {"key": blocked}})
        how, link = self._blocks(blocker).get(blocked, ("", {}))
        if how == "blocked by" and not getattr(self, "_swapped", False):
            _call(self.http.request, "DELETE", f"/rest/api/3/issueLink/{link['id']}")
            self._swapped = True
            return self._link(blocker, blocked)
        if how != "blocks":
            raise SystemExit(f"Could not confirm that {blocker} blocks {blocked}; stopping.")
        print(f"  {blocker} blocks {blocked}")

    def summary(self) -> None:
        created = self.created
        verb = "Created" if self.apply else "Would create"
        plural = {"Epic": "epics", "Story": "stories", "Task": "tasks", "Bug": "bugs"}
        types = ", ".join(f"{created[f'type {kind}']} {plural[kind]}" for kind in plural if created[f"type {kind}"])
        states = ", ".join(f"{created[f'status {status}']} {status.replace('_', ' ')}" for status in ("todo", "in_progress", "done") if created[f"status {status}"])
        print(
            f"{verb}: {created['projects']} projects, {created['components']} components, {created['issues']} issues"
            + (f" ({types}; {states}; {created['unassigned']} unassigned)" if created["issues"] else "")
            + f", {created['links']} Blocks links ({created['cross-project links']} across projects)"
            + (f", {created['status moves']} status moves on issues it made" if created["status moves"] else "")
            + "."
        )
        if not self.apply:
            print("Dry run: nothing was written. Re-run with --apply to create it.")
        else:
            print("Projects: " + ", ".join(f"{self.client.base_url}/browse/{project['key']}" for project in PROJECTS))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="create what is missing (default: print the plan only)")
    parser.add_argument("--env-file", help="read CONNECTOR_JIRA_* from this KEY=value file; the environment wins")
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(os.path.expanduser(args.env_file), override=False)
    missing = [name for name in ("CONNECTOR_JIRA_BASE_URL", "CONNECTOR_JIRA_EMAIL", "CONNECTOR_JIRA_API_TOKEN") if not os.environ.get(name, "").strip()]
    if missing:
        print(f"Set {', '.join(missing)} (or pass --env-file).")
        return 2
    try:
        client = JiraClient(os.environ["CONNECTOR_JIRA_BASE_URL"].strip(), os.environ["CONNECTOR_JIRA_EMAIL"].strip(), os.environ["CONNECTOR_JIRA_API_TOKEN"].strip())
        return Seeder(client, args.apply).run()
    except Exception as error:
        # One line, no traceback: messages here carry paths and Jira's error text, never a setting's value.
        print(f"Stopped: {type(error).__name__}: {error}. Re-running creates only what is still missing.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
