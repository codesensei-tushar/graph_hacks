# Onboarding a team

Company Brain started with engineering, but nothing in the graph is about code. Any team
can write down who is on it, what it runs and what it decided, and the same tools answer
for it: who owns the renewals process, what breaks if the CRM goes down, why pricing
changed. Every answer cites the path it walked.

A small team can onboard from Slack with one line. A larger one writes itself down as one
TOML file: start from the blank template,
[`docs/templates/team.example.toml`](templates/team.example.toml); the worked example,
[`examples/teams/quillfern-sales.toml`](../examples/teams/quillfern-sales.toml), is the
Sales team of the made-up demo company. Both go through the same importer and write the
same nodes and edges.

## From Slack

Mention the bot in a channel:

```
onboard team: Sales | members: @priya @dev | owns: Renewals (process), HubSpot (system, @dev), Pricing playbook (playbook) | decision: annual plans only, decided 2026-09-01
```

- After the team's name, each part is `members:`, `owns:` (or `runs:`), `decision:` (one
  per decision) or `summary:`, separated by `|`.
- Members are @mentions, so Slack says exactly who they are.
- Each thing is `Name (kind)`. Add `@someone` inside the brackets to name its owner;
  otherwise the first member owns it. Someone named as an owner joins the team.
- Other names people use for a thing follow `aka`, so questions find it by them:
  `Billing Service (service) aka billing, payments`. The names after `aka` run up to the next
  thing with a `(kind)`, so put a thing without one before them, or write the names inside the
  brackets: `Billing Service (service, @dev, aka billing, payments)`.
- A decision is about everything the line lists. `decided YYYY-MM-DD` dates it; without
  that, it is dated the day of the message, and whoever sent the line proposed it.

Nothing is written yet. The bot answers with an Approve card that lists everything it will
create:

> Onboard *Sales*: 2 people (Priya Shah and Dev Patel), 1 system (HubSpot), 1 process
> (Renewals), 1 playbook (Pricing playbook), the decision *Annual plans only*, 1 Sep 2026.
> Owners: Priya Shah (Renewals and Pricing playbook); Dev Patel (HubSpot). Visible to
> public channels.

After Approve, the reply counts what was written and cites every edge. In a public channel
the team is visible to everyone; in a private channel the team, what it runs and its
decisions stay in that channel (people are never scoped: they belong to the whole company).
A line that cannot be onboarded is answered with what to fix, and nothing is written. The
command does not work in a DM.

## Import a file

From a checkout, against the graph in `.env`:

```bash
python scripts/import_team.py my-team.toml --check   # check it; writes nothing
python scripts/import_team.py my-team.toml           # import it
```

Against a running instance, with the admin token:

```bash
curl -X POST "$BRAIN_URL/ops/import?check=true" -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" --data-binary @my-team.toml
curl -X POST "$BRAIN_URL/ops/import"            -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" --data-binary @my-team.toml
```

The answer counts what the file holds and what the import changed:

```json
{"team": "sales", "contents": "Sales: 3 people, 1 system, 1 process, 1 account, 1 playbook, 2 decisions",
 "nodes_created": 13, "edges_created": 32, "properties_set": 48, ...}
```

Importing the same file again is safe. Every write merges by id and nothing is ever
deleted, so a second import of an unchanged file creates nothing and sets nothing; an edited
file updates what changed. The other side of that: taking a block out of the file does not
take it out of Company Brain. The team joins its org's company: an org made in the console
gets a Company node named after it when it is created (orgs made earlier get one at the next
startup), and the legacy org's is the seeded one (`SEED_COMPANY`). `/ops/import` writes to the
legacy org only.

## The file

Four sections. Only the fields below are read; any other field is refused by name, so a
typo is an error you see, not a field that quietly disappears.

### `[team]`

| Field | | |
| --- | --- | --- |
| `name` | required | The team's name. |
| `summary` | | One sentence; it shows at the top of the team's page. |
| `id` | | Leave it out and one is made from the name (`"Sales"` becomes `sales`). |

### `[[members]]`, one block per person

| Field | | |
| --- | --- | --- |
| `name` | required | |
| `role` | | |
| `email` | | Give it. Once the bot is installed, everyone in the Slack workspace is already in Company Brain, and a member whose email is already there *is* that person: the import fills them in (name, role) instead of adding a second one. |
| `slack_id` | | Lets the bot know who is asking. In Slack: the person's profile, ⋮, *Copy member ID*. Only for someone new to Company Brain, or the Slack account already linked to them (see below). |
| `id` | | Made from the name if left out. To add someone already in Company Brain without an email, use their id; their name and role are updated to what the file says. |

### `[[runs]]`, everything the team runs

| Field | | |
| --- | --- | --- |
| `name` | required | |
| `kind` | | What sort of thing it is; see below. Defaults to `service`, so set it. |
| `owner` | | The one member who owns it. |
| `worked_on_by` | | Other members who have worked on it, suggested after the owner. |
| `aliases` | | Other names people use for it, so questions find it. |
| `summary` | | What it is. For a policy or a playbook the bot quotes it, so say what the rule is. |
| `depends_on` | | What stops this from working if it fails: other things in the file, or things already in Company Brain. |
| `id` | | Made from the name if left out. |

`kind` changes how the bot talks about the thing:

| `kind` | Is | When it fails |
| --- | --- | --- |
| `process` | work done again and again: renewals, ticket triage | stalls |
| `system` | a tool you rely on that can go down: a CRM, a help desk | goes down |
| `product` | something you sell or ship | is affected |
| `account` | a customer | is affected |
| `policy` | a rule: a refund policy | no longer applies |
| `playbook` | a how-to: a pricing playbook, an escalation guide | no longer applies |
| `service` | an engineering service (the default) | goes down |

An account is a customer, not a dependency: it never takes `depends_on`, and nothing can
depend on it.

### `[[decisions]]`, one block per decision

| Field | | |
| --- | --- | --- |
| `name` | required | The decision in one line. |
| `about` | required | What it is about, from the team's `[[runs]]` or from Company Brain. The team page finds a team's decisions this way. |
| `summary` | | Why, in a sentence. |
| `proposer`, `approver` | | Members. |
| `based_on` | | What it rests on: a playbook or policy in the file, another decision, or a document, ticket or pull request already in Company Brain. |
| `replaces` | | The earlier decision it replaces, from the file or Company Brain. The bot then answers with the current one first and says "This replaced ...", with both dates. |
| `decided_on` | | The day, as `"YYYY-MM-DD"`. Optional, but without it the decision has no date, so answers and the team page cannot say when it was decided. |
| `aliases`, `id` | | As for `[[runs]]`. |

### Pointing from one entry to another

`owner`, `worked_on_by`, `depends_on`, `about`, `based_on`, `replaces`, `proposer` and
`approver` name another entry, by its name exactly as written in the file or by its id.
Something already in Company Brain can be named the same way, by its name or id there.
A name that matches nothing, or matches two things, is refused; nothing is ever linked to
a guess.

## What gets refused

The whole file is checked before anything is written. If anything is wrong, nothing is
written and the answer lists every problem, one per line:

```
- unknown field 'ownr' in [[runs]] 'Renewals'; did you mean 'owner'?
- kind in [[runs]] 'Renewals' is 'department'; use one of service, system, process, product, account, policy, playbook
- [[runs]] 'Acme' is an account, and an account depends on nothing; leave depends_on off it
- owner in [[runs]] 'HubSpot': no member named 'Nobody Here' in this file or in Company Brain
- [[decisions]] 'Annual only' needs about: the things in [[runs]] it is about
- decided_on in [[decisions]] 'Annual only' should be a date like "2026-09-01"
```

It also refuses a `[[runs]]` entry whose id is already run by another team (point at it
with `depends_on` instead, or give the entry its own id), two entries with the same id, an
email two people share, anything already in Company Brain for other readers (a team
onboarded in a private channel cannot be re-onboarded from a public one), and a file over
256 KB.

An import links a Slack account only to a person it creates. A DM reads the private repos
of the person its Slack user is linked to, so a file or a command that paired a Slack
account with someone already here would hand that account their access. For someone
already in Company Brain, `slack_id` must be the Slack account already linked to them, or
left out (they link their own with `link account` in a DM to the bot). In the Slack
command, each @mention resolves through the person its Slack account is linked to, and a
mention with no person yet becomes a new one; a name is never matched to someone here.

## What it writes

Each section becomes the nodes and edges every tool already reads; there are no new
labels, and the file never supplies one:

```
(:Team)-[:PART_OF]->(:Company)
(:Person)-[:MEMBER_OF]->(:Team)                 members
(:SlackUser)-[:IS]->(:Person)                   slack_id
(:Team)-[:RUNS]->(:Service {kind})              runs
(:Person)-[:OWNS]->(:Service)                   owner
(:Person)-[:WORKED_ON]->(:Service)              worked_on_by
(:Service)-[:DEPENDS_ON]->(:Service)            depends_on
(:Decision)-[:ABOUT]->(:Service)                about
(:Person)-[:PROPOSED|APPROVED]->(:Decision)     proposer, approver
(:Decision)-[:BASED_ON]->(...)                  based_on
(:Decision)-[:SUPERSEDES]->(:Decision)          replaces
```

Everything a team runs is a `Service` node with a `kind`, so every traversal sees it.
Labels and relationship types are fixed in code, `kind` comes from a fixed list, ids are
reduced to lower-case letters, digits and dashes, and every value reaches the database as a
query parameter. Nothing in the file is ever written into a query's text.

## Sales, as the demo imports it

The demo company's seed imports `examples/teams/quillfern-sales.toml` through the same
importer, so the replay (`tests/integration/test_demo_replay.py`) proves it end to end.
The file names one person who is not on Sales: Tomas Reyes, the engineering manager
already in Company Brain, worked on the Pricing playbook. That one reference links the two
teams, so "who knows the pricing playbook" ends with him.

To import it live instead, as the video does, rehearse against a running instance and the
fake Slack API (the same path as in [SETUP.md](SETUP.md); both read `SLACK_SIGNING_SECRET`
from `.env`). The replay replaces the graph in `FALKORDB_GRAPH`, so point it at a scratch one.

```bash
python scripts/fake_slack.py --port 8899
SLACK_API_BASE_URL=http://127.0.0.1:8899/api/ SLACK_BOT_TOKEN= LLM_API_KEY= SEED_COMPANY=quillfern \
    BRAIN_ADMIN_TOKEN=local-demo-token uvicorn src.main:app --port 8010

# Quillfern without its Sales team, and the chat history
python scripts/replay_workspace.py --app-url http://127.0.0.1:8010 --team TQUILLFERN --bot UBRAIN \
    --fake-slack http://127.0.0.1:8899 --steps history --no-sales

# Onboard Sales from its file
curl -X POST http://127.0.0.1:8010/ops/import -H "Authorization: Bearer local-demo-token" \
    --data-binary @examples/teams/quillfern-sales.toml

# The seven demo steps, then the three Sales questions in #sales
python scripts/replay_workspace.py --app-url http://127.0.0.1:8010 --team TQUILLFERN --bot UBRAIN \
    --fake-slack http://127.0.0.1:8899 --steps demo --no-seed --no-github
```

After the import, in `#sales`:

- `who owns renewals, and who else has worked on it`: "The Renewals process is run by
  Priya Shah. Ask first: Priya Shah (owns it), then Dev Patel (worked on it) and Noor
  Haddad (worked on it)."
- `what breaks if hubspot goes down`: "If the HubSpot system goes down, the Renewals
  process stalls (1 hop) and the Pricing playbook no longer applies (2 hops)", with both
  `DEPENDS_ON` edges cited.
- `why did we change pricing`: "Sell annual plans only at renewal ... Decided 1 Sep 2026
  ... This replaced *Discount monthly plans at renewal* (decided 2 Mar 2026)".
