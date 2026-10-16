# Company Brain

[![Deploy](https://github.com/codesensei-tushar/graph_hacks/actions/workflows/deploy.yml/badge.svg)](https://github.com/codesensei-tushar/graph_hacks/actions/workflows/deploy.yml)

Hackathon project for Graph Hacks track 03. FalkorDB holds how a company is connected. An agent answers from that graph and shows the path behind each answer.

- What to build: [docs/PROBLEM.md](docs/PROBLEM.md)
- Hackathon strategy (rules, judges, competitors, build order): [docs/STRATEGY.md](docs/STRATEGY.md)
- How to run the graph: [docs/SETUP.md](docs/SETUP.md)
- Slack app and permissions: [docs/SLACK.md](docs/SLACK.md)
- GitHub App ingestion: [docs/GITHUB.md](docs/GITHUB.md)
- What's left for the product (agent, vector search): [docs/SLACK.md#whats-left-for-the-product-the-agent-and-vector-search](docs/SLACK.md#whats-left-for-the-product-the-agent-and-vector-search)
- Production deploy, CI/CD and operations: [docs/DEPLOY.md](docs/DEPLOY.md)

AI assistance: parts of this project were written with Claude Code (Anthropic), including the production infrastructure, deploy pipeline and their docs.

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/seed_graph.py
uvicorn src.main:app --reload
```

The interface is Slack. Create the app from [`slack_app/manifest.yaml`](slack_app/manifest.yaml) and follow [docs/SLACK.md](docs/SLACK.md). Health check: [http://localhost:8000/api/health](http://localhost:8000/api/health). Graph browser: [http://localhost:3000](http://localhost:3000).

## Graph data model

One FalkorDB graph (`company`) holds everything: the seeded company, what Slack and GitHub add at runtime, every memory, and who may read what. Nodes carry `id` and `name`; the tool answers cite them. The Cypher that reads and writes each part is listed in the next section.

**Scopes.** A node written from Slack or GitHub carries `scope`: `org_shared` (public channels, public repos), `slack_channel_<id>` (one private channel or group DM), `user_<slack id>` (one person's DMs with the bot), or `github_repo_<id>` (one private repo). Seeded nodes have no `scope` and count as `org_shared`. Every read filters `coalesce(n.scope, 'org_shared') IN $scopes`, where `$scopes` is the asker's read set.

**Company knowledge** (`src/seed/`, extended by Slack commands and GitHub webhooks):

| Label | Properties | Edges out |
| --- | --- | --- |
| `Company` | `id`, `name` | |
| `Team` | `id`, `name` | `-[:PART_OF]->(Company)`, `-[:RUNS]->(Service)` |
| `Person` | `id`, `name`, `role`, `email`, `github`, `org_member`, `provisioned_by`, `created_at` | `-[:MEMBER_OF]->(Team)`, `-[:OWNS\|WORKED_ON\|KNOWS]->(Service)`, `-[:WORKED_ON]->(Ticket)`, `-[:AUTHORED]->(Document\|Chat)`, `-[:PROPOSED\|APPROVED]->(Decision)` |
| `Service` | `id`, `name`, `aliases`, `summary`, `paths` (repo prefixes a pull request can touch) | `-[:DEPENDS_ON]->(Service)` |
| `Document` | `id`, `name`, `summary` | `-[:DESCRIBES]->(Service)` |
| `Ticket` | `id`, `name`, `title`, `summary`, `scope`; from GitHub also `number`, `state`, `url`, `source = 'github'`, `updated_at` | `-[:ABOUT]->(Service)`, `-[:IN_REPO]->(Repo)` |
| `Decision` | `id`, `name`, `summary`, `aliases` | `-[:ABOUT]->(Service)`, `-[:CITES]->(Document\|Ticket)` |
| `Chat` | `id`, `name`, `summary` | `-[:ABOUT]->(Decision)` |

**Slack** (`src/domain/slack/`):

| Label | Properties | Edges out |
| --- | --- | --- |
| `SlackWorkspace` | `team_id`, `team_name`, `bot_user_id`, `bot_token_enc`, `installed_by`, `scopes`, `app_id`, `home_channel_id`, `source`, `created_at`, `updated_at` | |
| `SlackWorkspaceMember` | `id` (`<team>:<user>`), `team_id`, `slack_user_id`, `person_id`, `email`, `status`, `link_source`, `provisioned_person_id`, `updated_at` | `-[:LINKS]->(Person)` |
| `SlackUser` | `id`, `name`, `email`, `is_member` | `-[:IS]->(Person)`, `-[:MEMBER_OF {updated_at}]->(Channel)`, `-[:SAID]->(Memory)` |
| `Channel` | `id`, `name`, `is_private`, `backfilled_at` | |

**Memory** (`src/domain/memory/`):

| Label | Properties | Edges out |
| --- | --- | --- |
| `Scope` | `id` (the scope string), `name`, `entity_context`, `profile_buckets` | |
| `Memory` | `id`, `custom_id`, `scope`, `text`, `content`, `name`, `title`, `type`, `source_type`, `metadata`, `brain_tags`, `brain_tag_labels`, `buckets`, `channel_id`, `ts`, `forget_after`, `is_forgotten`, `source_count`, `created_at`, `updated_at` | `-[:IN_SCOPE]->(Scope)`, `-[:HAS_TAG]->(Tag)`, `-[:FILED_UNDER]->(Tag)`, `-[:MENTIONS]->(Service\|Document\|Ticket\|Decision)` |
| `Tag` | `id` (`<scope>::<key>`), `container`, `key` (a topic path such as `product/billing`), `label`, `kind`, `description`, `implicit`, `use_count`, `created_at`, `updated_at`, `last_used_at` | `-[:PARENT]->(Tag)` |

**GitHub** (`src/domain/github/`):

| Label | Properties | Edges out |
| --- | --- | --- |
| `Repo` | `id`, `name`, `full_name`, `private`, `html_url`, `installation_id`, `scope` | |
| `GitHubUser` | `id`, `login`, `name` | `-[:IS]->(Person)`, `-[:CAN_READ {user}]->(Repo)`, `-[:AUTHORED]->(PullRequest)`, `-[:REVIEWED {state}]->(PullRequest)`, `-[:OPENED\|ASSIGNED]->(Ticket)` |
| `PullRequest` | `id`, `name`, `number`, `title`, `summary`, `state`, `merged`, `url`, `scope`, `updated_at` | `-[:IN_REPO]->(Repo)`, `-[:TOUCHES]->(Service)`, `-[:CLOSES]->(Ticket)` |

**Landing this week** (docs/STRATEGY.md items 2 and 4): `Decision` gains `decided_at` and `rationale`, plus `-[:SUPERSEDES]->(Decision)`, `-[:DECIDED_IN]->(Channel|Document)`, `-[:BASED_ON]->(Document|Ticket|PullRequest)` and `-[:MADE_BY]->(Person)`, so a later message that changes a plan supersedes the old decision instead of overwriting it. `Memory` gains `embedding` with a vector index for semantic recall next to the full-text index.

**Indexes** (`ensure_indexes` in `src/domain/memory/memory_service.py`): range indexes on `Memory.scope`, `Memory.id`, `Memory.custom_id`, `Tag.id`, `Tag.container`, `Scope.id`, `SlackUser.id`, `Channel.id`; a full-text index on `Memory.text`; the vector index on `Memory.embedding` once item 4 lands.

**Short-lived state** lives as Redis keys in the same FalkorDB instance, so the app talks to one process: the scheduler's sorted set `brain:schedule`, approvals `brain:approval:*`, running turns `brain:thread-turn:*`, bot threads `brain:bot-thread:*`, the per-channel event ring `brain:slack-event:*`, channel-observe batches `brain:observe:*`, and the dedupe keys `slack:evt:*` and `github:delivery:*`.

## Cypher queries the product depends on

The agent never writes Cypher. Each tool in `src/domain/knowledge/` is a parameterised query written by hand; the model only picks the tool and its arguments. Every tool returns the nodes and edges it walked, and the Slack reply prints them as `A -[REL]-> B` lines. `$scopes` is always the asker's read set.

**Name resolution, before any tool runs.** The question is matched against names, aliases and titles the asker may see. No match means the tool answers that the graph doesn't know the thing, and names the nearest candidates. It never guesses.

```cypher
MATCH (n)
WHERE labels(n)[0] IN $labels AND n.name IS NOT NULL
  AND coalesce(n.scope, 'org_shared') IN $scopes
RETURN n.id, n.name, labels(n)[0], coalesce(n.aliases, ''), coalesce(n.title, '')
```

**who_knows** (owner, past work, tickets, pull requests and reviews for one service):

```cypher
MATCH (person:Person)-[rel:OWNS|WORKED_ON|AUTHORED|KNOWS]->(node {id: $id})
RETURN person.name, type(rel), node.name

MATCH (ticket:Ticket)-[:ABOUT]->(service:Service {id: $id})
WHERE coalesce(ticket.scope, 'org_shared') IN $scopes
OPTIONAL MATCH (person:Person)-[:WORKED_ON]->(ticket)
RETURN ticket.name, ticket.title, service.name, person.name

MATCH (gh:GitHubUser)-[rel:AUTHORED|REVIEWED]->(pr:PullRequest)-[:TOUCHES]->(service:Service {id: $id})
WHERE coalesce(pr.scope, 'org_shared') IN $scopes
OPTIONAL MATCH (gh)-[:IS]->(person:Person)
RETURN coalesce(person.name, '@' + gh.login), type(rel), pr.name, service.name
```

**decision_trace** (who proposed and approved, what it cites, the chat it came from):

```cypher
MATCH (person:Person)-[rel:PROPOSED|APPROVED]->(decision:Decision {id: $id})
RETURN person.name, type(rel), decision.name, decision.summary

MATCH (decision:Decision {id: $id})-[rel:CITES|ABOUT]->(source)
WHERE coalesce(source.scope, 'org_shared') IN $scopes
RETURN decision.name, type(rel), source.name, labels(source)[0]

MATCH (chat:Chat)-[:ABOUT]->(decision:Decision {id: $id})
WHERE coalesce(chat.scope, 'org_shared') IN $scopes
OPTIONAL MATCH (author:Person)-[:AUTHORED]->(chat)
RETURN author.name, chat.name, chat.summary
```

**blast_radius** (everything that transitively depends on a service). Today the edges are read once and walked breadth-first in Python; the catalogue version walks them in the graph with a capped depth, which is what the depth-one versus depth-four demo shows.

```cypher
MATCH (child:Service)-[:DEPENDS_ON]->(parent:Service)
RETURN child.id, child.name, parent.id, parent.name
```

**ingest_ticket** (the `ticket:` command, after Approve):

```cypher
CREATE (ticket:Ticket {id: $id, name: $id, title: $title, summary: $title, scope: $scope})
WITH ticket
MATCH (service:Service {id: $service})
MERGE (ticket)-[:ABOUT]->(service)
```

**Memory recall** (full-text, one call per readable scope, results scored and merged):

```cypher
CALL db.idx.fulltext.queryNodes('Memory', $q) YIELD node AS m, score
WHERE m.scope = $scope AND NOT coalesce(m.is_forgotten, false)
OPTIONAL MATCH (author:SlackUser)-[:SAID]->(m)
RETURN m.id, m.text, m.scope, m.channel_id, m.ts, m.title, m.brain_tags, m.updated_at, author.id, author.name, score
```

**Memory write** (every kept Slack message; `MENTIONS` ties chat to the company graph):

```cypher
MERGE (scope:Scope {id: $scope})
MERGE (m:Memory {custom_id: $cid})
ON CREATE SET m.id = $id, m.created_at = $now, m.source_count = 0
SET m.scope = $scope, m.text = $text, m.channel_id = $channel, m.ts = $ts, m.updated_at = $now
MERGE (m)-[:IN_SCOPE]->(scope)

MATCH (m:Memory {id: $id}) MERGE (u:SlackUser {id: $author}) MERGE (u)-[:SAID]->(m)
MATCH (m:Memory {id: $memory}), (n {id: $node}) MERGE (m)-[:MENTIONS]->(n)
```

**Read set** (what a conversation may see). A public channel reads `org_shared`. A private channel adds its own scope. A DM adds the person's scope, every private channel they are a member of, and every private repo they can read:

```cypher
MATCH (:SlackUser {id: $user})-[:MEMBER_OF]->(c:Channel {is_private: true}) RETURN c.id

MATCH (:SlackUser {id: $user})-[:IS]->(:Person)<-[:IS]-(:GitHubUser)-[:CAN_READ]->(r:Repo)
WHERE r.scope STARTS WITH 'github_repo_'
RETURN DISTINCT r.scope
```

**GitHub ingest** (webhooks and the install backfill). A pull request links to every service whose `paths` prefix covers a changed file; `closes #n` in the body becomes `CLOSES`; the collaborator list replaces the repo's `CAN_READ` edges on every sync:

```cypher
MERGE (p:PullRequest {id: $id})
SET p.name = $name, p.number = $number, p.title = $title, p.summary = $summary,
    p.state = $state, p.merged = $merged, p.url = $url, p.scope = $scope, p.updated_at = $updated
WITH p MATCH (r:Repo {id: $repo}) MERGE (p)-[:IN_REPO]->(r)

MATCH (p:PullRequest {id: $pr}), (s:Service {id: $service}) MERGE (p)-[:TOUCHES]->(s)
MATCH (u:GitHubUser {id: $user}), (p:PullRequest {id: $pr}) MERGE (u)-[r:REVIEWED]->(p) SET r.state = $state
MATCH (u:GitHubUser {id: $user}), (r:Repo {id: $repo}) MERGE (u)-[c:CAN_READ]->(r) SET c.user = $user
```

**Graph algorithms** (landing this week as tools; each call verified on the `falkordb/falkordb:v4.22.1` image this project runs):

```cypher
-- load_bearing: which service the most others lean on
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name, score ORDER BY score DESC LIMIT 5

-- path_between: why A is connected to B
MATCH (a {id: $a}), (b {id: $b})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['DEPENDS_ON', 'OWNS', 'WORKED_ON', 'TOUCHES', 'AUTHORED', 'IS'], maxLen: 6})
YIELD path, pathWeight
RETURN path

-- recall, semantic: nearest memories the asker may read
CALL db.idx.vector.queryNodes('Memory', 'embedding', $k, vecf32($query)) YIELD node, score
WHERE node.scope IN $scopes
RETURN node.text, node.scope, node.ts, score
```
