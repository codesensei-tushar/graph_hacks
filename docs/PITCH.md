# Pitch deck

The slide text of the Company Brain pitch deck, kept here so changes show up in review. The deck itself is a 16:9 slide artifact built from this text; its link goes in the submission notes. Fourteen slides, about three minutes spoken. Every example is a real reply from the synthetic company, Quillfern, with no model key, unless it is marked pending.

Typefaces: Space Grotesk for headings, IBM Plex Sans for text, JetBrains Mono for edges and code. Colours: ink `#14181F`, paper `#F6F4EF`, accent orange `#E8622F` (services), blue `#2A78D6` (people), green `#1BAF7A` (documents, tickets, decisions), the same families the ask page draws.

## 1. Cover

**Company Brain.** One graph of how the company is connected. Answers that show the path.

Graph Hacks 2026 · Track 03, Company Brain. `Arjun Mehta -[OWNS]-> Billing Service`. brain.noobsurd.space

> Notes: Company Brain is a Slack agent over a single FalkorDB graph. People, teams, services, documents, tickets, decisions and chat are nodes and edges, and Slack and GitHub feed it live. Ask it something and it answers by walking the graph, then prints the edges it used.

## 2. A billing bug lands on Friday. Who should look at it?

| Source | Fact |
| --- | --- |
| Org chart | Who owns the billing service |
| Pull requests | Who changed the retry code last month |
| Reviews | Who approved that change |
| Docs | Who wrote what a retry should do |

Four facts in four systems, joined by relationships nobody wrote down. A search box returns documents. **A graph returns people, and the reason each one is on the list.**

## 3. One FalkorDB graph. A Slack agent that walks it.

Feeds (Slack messages, GitHub pull requests and reviews, the company's docs, tickets and decisions) → one graph (people, teams, services, documents, tickets, decisions, chat and memories, with who may read what) → fixed tools (hand-written, parameterised Cypher: who owns, who knows, why, what breaks, how connected) → the answer (a short answer, the edges it walked, and the tool that ran).

Ask in a channel or a DM. New messages, pull requests and tickets land in the graph as they happen, so the next answer already uses them.

## 4. One graph, four groups of nodes

| Group | Nodes | Edges |
| --- | --- | --- |
| The company | Team, Person, Service (with a kind), Document, Ticket, Decision, Chat | `OWNS` · `DEPENDS_ON` · `SUPERSEDES` |
| Slack | SlackWorkspace, SlackUser, Channel, and who is in which private channel | `IS` · `MEMBER_OF` |
| Memory | Dated memories in the scope of the room they were said in, tagged into a topic tree | `SAID` · `MENTIONS` |
| GitHub | Repo, GitHubUser, PullRequest, linked to the services whose code changed | `AUTHORED` · `TOUCHES` |

Every node written from Slack or GitHub carries the scope of the room or repo it came from, and every query ends with the same filter: `coalesce(n.scope, 'org_shared') IN $scopes`.

## 5. The agent never writes Cypher

1. **Resolve names.** The things the question names are matched against the graph first.
2. **Pick a tool.** One Claude Haiku call fills a schema. No key? A keyword router does it.
3. **Run its query.** Hand-written, parameterised, read-only Cypher, filtered by scope.
4. **Return the path.** The answer carries the exact edges walked, and who picked the tool.
5. **Writes wait.** A write stops at an Approve card that names its resolved target.

No model-generated string ever reaches a query, so there is nothing to inject. **Twenty-four read-only tools, three that write only after a human approves.**

> Notes: read tools are who_owns, who_knows, decision_trace, decision_pressure, working_groups, blast_radius, who_should_know, if_person_leaves, onboarding_brief, person_profile, reading_list, what_changed, connectors, path_between, load_bearing, risk_map, team_dependencies, decision_health, related_memories, search, explore, find, neighbors and recall; the writers are ingest_ticket, record_decision and onboard_team.

## 6. The answer is the path

Question: `who owns billing, and who else has worked on it`

> Billing Service is owned by Arjun Mehta. Ask first: Arjun Mehta (owns it, wrote the Billing runbook), then Lena Fischer (worked on it and TKT-102), Tomas Reyes (worked on it) and Sara Lindqvist (knows it). Open tickets: TKT-102.
>
> *Tool who_knows, picked by keywords. Read from: public channels*

```
Path (who_knows, 8 edges)
Arjun Mehta -[OWNS]-> Billing Service
Lena Fischer -[WORKED_ON]-> Billing Service
Sara Lindqvist -[KNOWS]-> Billing Service
Tomas Reyes -[WORKED_ON]-> Billing Service
TKT-102 -[ABOUT]-> Billing Service
Lena Fischer -[WORKED_ON]-> TKT-102
Arjun Mehta -[AUTHORED]-> Billing runbook
Billing runbook -[DESCRIBES]-> Billing Service
```

People are ranked by their edges to the service: owns 5, wrote a pull request 3, worked on, reviewed or assigned 2, knows or opened 1. The web page draws the same edges as a graph.

## 7. Decisions stay current, and keep their history

**Stands, 15 Sep 2026:** Move the memory layer off Postgres. Proposed by Kofi Mensah, approved by Tomas Reyes. `SUPERSEDES` → **Replaced, 1 Jun 2026:** Keep notebook links in Postgres join tables. Proposed by Sara Lindqvist.

`BASED_ON`: RFC: memory layer on a graph store · TKT-101: Memory queries time out on large workspaces.

A plan changes in a channel: `decision: move billing retries to the invoice worker`. The Approve card names what it will replace. Ask about the old one and the first line says it is no longer current, then the one that stands, with both dates. Nothing is deleted.

## 8. Rankings and routes are a CALL, not code

| Question | Call | Reply |
| --- | --- | --- |
| What breaks if auth goes down | `DEPENDS_ON*1..6` | API Gateway, Billing, Notification and Search at one hop, Invoice Worker at two. Billing and Notification are the main connectors, a tie, counted from the routes walked. |
| Which services are load-bearing | `algo.pageRank` | Postgres carries the most weight (the highest PageRank): 6 services depend on it directly or through others. |
| How is Maya connected to the invoice worker | `algo.SPpaths` | Maya Ortiz → Auth Service → Billing Service → Invoice Worker, each hop cited in the direction the edge points. |
| Who actually works together | `algo.labelPropagation` | Arjun, Lena and Tomas share Billing Service, though the org chart puts Tomas on Platform. |

## 9. It never guesses, and reads only what you may see

**Refuse to guess.** `who knows the payments gateway`

> Nothing in the graph matches “payments gateway” as one thing. Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”).

Names resolve against the graph before any tool runs.

**Permissions are a graph query.** A public channel reads public memory. A private channel adds its own. A DM adds your private channels and the private repos your linked GitHub account can read. Private repo data shows only to people who can read the repo.

## 10. Proof that the graph does the work

**Take FalkorDB out, and 5 of 6 capabilities collapse.**

The same questions, run again with every graph read returning nothing. The sixth is a refusal and stays one: it never depended on the graph being up. Live on the page, and as a table at `/api/proof`.

## 11. For every team, not only engineering

A Service node has a **kind**: `service`, `system`, `process`, `product`, `account`, `policy`, `playbook`. A renewals process, a CRM, a pricing playbook and an account sit in the same graph as the billing service, and every tool answers for them in the right words: a process stalls, a policy no longer applies.

Each team gets a page: its people, what it runs grouped by kind, the decisions that stand and what was said. A new team joins from one TOML file that people who do not write code can fill in. The import only merges, and people Slack already knows are matched by email:

```
POST /ops/import  sales.toml   →   13 nodes, 32 edges created.  Again: 0.
```

Or one line in Slack: `onboard team: Sales | members: @priya @dev | owns: Renewals (process), HubSpot (system), Pricing playbook (playbook) | decision: annual plans only, decided 2026-09-01`. The Approve card counts what it will create before anything is written, and the team goes in through the same importer.

**Sales, onboarded on the synthetic company.** Final: these are the replies the replay and import tests pin.

| Question | Reply |
| --- | --- |
| `who owns renewals, and who else has worked on it` | "The Renewals process is run by Priya Shah. Ask first: Priya Shah (owns it), then Dev Patel (worked on it) and Noor Haddad (worked on it)." |
| `what breaks if hubspot goes down` | "If the HubSpot system goes down, the Renewals process stalls (1 hop) and the Pricing playbook no longer applies (2 hops)." |
| `why did we change pricing` | "Sell annual plans only at renewal. … Decided 1 Sep 2026. … This replaced *Discount monthly plans at renewal* (decided 2 Mar 2026)." |
| `what if priya shah leaves` | "If Priya Shah leaves, 2 things rest on them alone … Pricing playbook: Priya Shah is its only owner; next in line: Noor Haddad, then Tomas Reyes." Tomas works on Platform: the next in line comes from another team. |

## 12. One build, three tracks

| Track | What it shows |
| --- | --- |
| 03 · Company Brain (primary) | People, services, docs, tickets, decisions and chat in one graph. Who owns and who knows, decisions traced and kept current, every answer with its sources. |
| 01 · Agents that act on connected data | Multi-hop tools with a chosen depth, graph algorithms as tools, the same catalogue over MCP, tickets filed after an Approve click, and the path as the explanation. |
| 02 · Agent memory | Dated memories per room, history that persists in the graph, and separate memory per person, private channel and private repo. |

> Notes: Track 02 is a single agent in Slack plus any MCP client, sharing one graph and one permission model; we do not claim a multi-agent crew.

## 13. Built on one database

**FalkorDB is the only store.** The graph, and in the same instance the short-lived state: the scheduler, approvals, running turns and event dedupe. The app talks to one process.

- **Surfaces.** A Slack app for every workspace, a public ask page with team pages, and the read-only catalogue over MCP.
- **Feeds.** Slack events and a GitHub App: pull requests, reviews and issues, linked to services by the paths they touch.
- **Model, optional.** One structured Claude Haiku call picks the tool. Without a key, a keyword router answers every path.

## 14. Close

**The graph is the product.**

Try it, no Slack needed: brain.noobsurd.space · Source and data model: github.com/codesensei-tushar/graph_hacks · Demo video: [video link]
