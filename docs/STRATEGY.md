# Hackathon strategy

Research for the final stretch of [Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb), Track 03 Company Brain. Submissions close **18 Oct 2026, 11:59 PM IST**. This page records what the organizers reward, where this project stands against the field, which FalkorDB features are confirmed to work on our image, and what to build in what order. The problem statement itself is in [PROBLEM.md](PROBLEM.md).

Short version: enter all three tracks with one build, keep Track 03 primary, and spend the remaining time on the agent loop over a fixed Cypher tool catalogue that returns the graph path with every answer.

## Submission checklist

From the [rules](https://www.wemakedevs.org/hackathons/falkordb/rules) and [overview](https://www.wemakedevs.org/hackathons/falkordb). There is no published scoring rubric.

| Required | Status |
| --- | --- |
| Public source repository | Done |
| README with setup instructions **and the graph data model** | Done. README, "Graph data model". Update it as the Decision edges and the vector index land. |
| **The Cypher queries or graph algorithms the product depends on**, in the README | Done. README, "Cypher queries the product depends on". Update it as the tool catalogue PRs land. |
| Demo video of the working project | Not yet. Script below. |
| Live deployment, or complete local setup | Deployed at `brain.noobsurd.space` (DEPLOY.md). Keep it up through judging. |
| Tracks named at submission | Enter 01, 02 and 03. Each has its own prize. |
| FalkorDB is the primary database and "supports a central part of the product" | Yes. It is the only store, including the Redis keys. |
| Only public, synthetic or your own data | The demo workspace must be a synthetic company. Never load a real customer workspace. |
| AI assistance disclosed, and every query explainable by the team | README has the disclosure. Each of us must be able to walk through the tool queries. |
| Main FalkorDB work new during the event | Yes. First commit is 15 Oct. |

Rules also state: "If the product still works without FalkorDB, the graph is decoration and doesn't qualify."

## What the organizers reward

The clearest signal is the organizers' own reference build, [How to build an agent that reasons over a graph](https://www.wemakedevs.org/blogs/how-to-build-an-agent-that-reasons-over-a-graph). It says judging "comes back to whether the graph is doing work the agent could not do without it", and lays out the design to copy:

- **The agent never writes Cypher.** Humans write parameterised queries in a fixed tool catalogue. The model picks a tool and fills in arguments. This also closes injection, because no model-generated string reaches a query.
- **Return the path with every answer.** Explanation is the route the traversal took, not a sentence the model writes afterwards. "Reads like reasoning and is reconstruction."
- **Refuse to guess.** Resolve names against the graph before running anything. An empty traversal means "the graph does not know", and the agent should say exactly that.
- **Algorithms for rankings, routes and clusters.** Shortest path for "why is A connected to B", PageRank for "what is load-bearing", instead of hand-rolled code.
- **Memory as nodes with dates.** Write findings back, connect them, and return when they were learned.
- **One graph per tenant** with `select_graph`.
- **Two demo moves:** run the same question at depth one and depth four so the deeper run surfaces a distant dependency with its path, and ask about something not in the graph so the agent says so.

The track brief adds: knowledge from docs, tickets and chats in one graph; who owns what and who knows what; tracing decisions back to their source; keeping knowledge current as things change; answers with clear sources.

## Where we stand

What exists and is ahead of the visible field: live Slack ingestion with a permissions graph (scopes per public channel, private channel and person), GitHub App ingestion of pull requests, reviews and issues, proactive triage and chime-in, approvals for graph writes, scheduled jobs on the same database, multi-workspace installs, and a production deploy with CI. None of the Track 03 entries found so far has a real chat surface.

What is missing, and what judges will notice first:

- `compute_turn` still falls back to graph lookups. There is no agent loop, so the demo cannot show tool selection.
- Decisions are static seed nodes. Nothing supersedes them when a new message changes the plan.
- Memory search is keyword only (full-text). No semantic recall.
- No algorithm runs in any answer.
- Nothing renders or cites the path as a path.
- No proof that the product collapses without the graph.

Visible Track 03 competitors on GitHub as of 17 Oct:

| Entry | What it does | Threat |
| --- | --- | --- |
| [MERIDIAN](https://github.com/aniruddhaadak80/meridian-graphhacks) | Four agents on one graph, graph per tenant, MCP with eight tools, an ablation endpoint that shows which capabilities collapse without the graph, live on Vercel, enters all three tracks | High |
| [Axon](https://github.com/volfiros/axon-brain) | "Remembers why your team decided things, and flags when new work goes against it" | Medium. Best decision-tracing pitch; still in design when indexed |
| [Minutes](https://github.com/Hydra-Of-Malice/FalkorDB) | Time-aware shared memory from meetings and email, Tracks 02 and 03 | Medium |
| [dealership-brain](https://github.com/rossycoder/dealership-brain) | Multi-agent brain for one vertical | Low |

Business framing judges will recognise: the "people, content, activity" knowledge graph that [Glean](https://docs.glean.com/security/knowledge-graph) sells for expertise search, plus the shared "enterprise brain" memory layer vendors now pitch under agents. At least one hosted Slack knowledge assistant shut down in September 2026 and open-sourced its harness, so a self-hosted brain where the graph is both the memory and the permission model is a live gap.

## FalkorDB features confirmed on our image

Checked on 17 Oct against the local `falkordb/falkordb:v4.22.1` container by running each call on a throwaway graph. The production compose runs the same version.

| Feature | Call that worked | Notes |
| --- | --- | --- |
| PageRank | `CALL algo.pageRank('Label', 'REL') YIELD node, score` | Positional arguments; `null` means all |
| Betweenness | `CALL algo.betweenness({nodeLabels: [...], relationshipTypes: [...]}) YIELD node, score` | Config map form. Positional form returned nothing |
| Shortest paths | `CALL algo.SPpaths({sourceNode: a, targetNode: c, relTypes: ['REL'], maxLen: 5}) YIELD path, pathWeight` | Bind the nodes with `MATCH` first |
| Components | `CALL algo.WCC({nodeLabels: [...]}) YIELD node, componentId` | |
| Communities | `CALL algo.labelPropagation({nodeLabels: [...]}) YIELD node, communityId` | `algo.CDLP` from the docs is **not registered** on this build |
| Vector index | `CREATE VECTOR INDEX FOR (m:Memory) ON (m.embedding) OPTIONS {dimension: N, similarityFunction: 'cosine'}` | Store with `vecf32([...])`; a plain list is not indexed |
| Vector query | `CALL db.idx.vector.queryNodes('Memory', 'embedding', k, vecf32([...])) YIELD node, score` | The index does not filter. Apply the scope filter in Cypher after `YIELD` |
| Full-text | `db.idx.fulltext.createNodeIndex` / `queryNodes` | Already used by `search_brain.py` |
| Variable-length paths | `MATCH p=(s:Service {id: $id})<-[:DEPENDS_ON*1..4]-(d:Service) RETURN [n IN nodes(p) \| n.name]` | A **parameter is not accepted as a bound** (`*1..$depth` fails with "unhandled type in inlined properties"). Clamp depth to an int in Python and build the literal |
| Undirected shortest path | `CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: [...], relDirection: 'both', maxLen: 6})` | The default `relDirection` is `outgoing`, which returns nothing for person -> service -> service routes. `pathCount: 0` returns every shortest path |
| Read-only queries | `graph.ro_query(...)` on the Python client | Rejects writes ("graph.RO_QUERY is to be executed only on read-only queries"). Every read tool uses it |
| Ranking people | A `UNION` of `OWNS\|WORKED_ON\|KNOWS` rows and `AUTHORED\|REVIEWED -> TOUCHES` rows, weighted and summed in Python | PageRank over people is flat (every Person 0.011 to 0.015), list arguments to `algo.pageRank` silently yield nothing, and grouping by a collected list of maps fails. Billing on Quillfern: Arjun 10, Lena 7, Tomas 4, Sara 1 |
| Load-bearing services | `CALL algo.pageRank('Service', 'DEPENDS_ON')` | Quillfern: Postgres 0.113, Auth 0.086, Graph Store 0.049, the same order as transitive dependents |
| Edge direction in a path | `UNWIND range(0, length(path)-1) AS i ... startNode(relationships(path)[i]) = nodes(path)[i]` | With `relDirection: 'both'` an edge can be walked backwards; cite the true arrow |
| Memory expansion | `(:Memory)-[:MENTIONS]->(e)` then `(e)-[]-(n)` and `(:Memory)-[:MENTIONS]->(n)`, as two `UNION` branches with the scope filter in each, `ORDER BY` after the union | Verified with three hand-made memories: private-channel memory hidden from `org_shared`, visible with that channel in the read set; one-hop rows via the neighbour's name |
| Connectors | `CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON']})` | Quillfern: Auth 3.5, Billing 3.0, Notification 0.5 |
| Depth truth | `*1..N` literal, `RETURN DISTINCT d.name, min(length(p))` | Quillfern: Auth at depth one reaches API Gateway, Billing, Notification, Search; depth two adds Invoice Worker; nothing new after. The contrast is one versus two, not one versus four |
| Also registered | `algo.AStar`, `algo.BFS`, `algo.HarmonicCentrality`, `algo.SSpaths`, `algo.MSF`, `algo.maxFlow`, `db.idx.vector.queryRelationships` | |

Other FalkorDB tooling the resources page lists: the [MCP server](https://docs.falkordb.com/genai-tools/mcpserver), [GraphRAG-SDK](https://github.com/FalkorDB/GraphRAG-SDK), [Graphiti](https://docs.falkordb.com/agentic-memory/graphiti) for bi-temporal episodes, and [Text-to-Cypher](https://www.falkordb.com/blog/text-to-cypher-natural-language-graph-queries/). We don't need them: the tool catalogue is hand-written Cypher by design, and our memory model already exists. Mention them in the blog as alternatives considered.

## Build plan, in order

Each item names the layer it lands in. Business logic stays in `src/domain/`; provider code in `src/integrations/`; routes stay thin.

1. **Agent loop over a fixed tool catalogue.** `src/domain/knowledge/`: one function per tool, each a parameterised Cypher query that returns rows plus the nodes and edges it walked. Tools: `who_owns`, `who_knows`, `decision_trace`, `blast_radius`, `path_between`, `load_bearing`, `recall`, `ingest_ticket`. Entity names resolve against the graph before any query. The loop itself registers through `register_turn_agent` in `compute.py`; model calls stay in `src/integrations/llm/model.py`. The reply names the tool that ran and cites the chain `node -[edge]-> node`.
2. **Decisions that stay current.** Add `(:Decision)-[:SUPERSEDES]->(:Decision)`, `[:DECIDED_IN]->(:Channel|:Document)`, `[:BASED_ON]->(:Document|:Ticket|:PullRequest)` and a `decided_at` property. A channel message that changes a plan writes a new Decision superseding the old one. `decision_trace` returns the current one, the one it replaced, and the dates. This answers "tracing decisions" and "keeping knowledge current" in one demo move.
3. **Algorithms as tools.** `load_bearing` runs PageRank over `DEPENDS_ON`. `who_knows` ranks people by PageRank over `AUTHORED`, `REVIEWED` and `TOUCHES` instead of counting. `path_between` runs `algo.SPpaths`. Betweenness surfaces connector people; label propagation surfaces de facto teams.
4. **Recall follows edges, not embeddings.** Vector recall is cut (see Round 2 below). Instead a small `related_memories` tool expands from the entities the question resolved to: memories that `MENTIONS` the same node, then memories that mention its neighbours one hop out, each filtered by the asker's read set, with the existing full-text index as the first pass. This is recall the graph does and a vector store cannot. If time remains after the video is recorded, an ONNX embedding model behind a flag, default off.
5. **Synthetic company and a replayable history.** A fake Slack Web API plus a signed-event driver in `scripts/` doubles as a local end-to-end test and the demo loader: replay a scripted workspace history and a public GitHub repo into the graph. Satisfies the data rule and makes the demo repeatable.
6. **Proof the graph does the work.** An ops route that runs each demo question with the graph disabled and reports which capabilities collapse, like MERIDIAN's ablation. Cheap, and strong in the video.
7. **MCP over the same tools.** `/mcp` already exists and answers 501. Serving the catalogue there makes the Track 01 entry credible.
8. **README data model and query list, video, blog.** The rules require the first two. The blog is a side prize.

Cut line: if time runs out, ship items 1, 2 and 5 plus the video. Those are the difference between "graph as a store" and "graph as the product".

## Demo script

Run against the synthetic company, in Slack, in this order:

1. `who owns billing, and who else has worked on it` in a public channel. Owner, then people linked by past pull requests, with the path.
2. `why did we move the memory layer off Postgres`. Decision, author, source doc, source ticket.
3. Post `we're moving billing retries to the invoice worker, decided in today's sync` in the channel. Ask question 2's equivalent for billing. The answer shows the new decision, says what it superseded, and when.
4. `what breaks if auth goes down, one hop`, then `and two hops out`. The first answer says how many more break further out; the second surfaces the invoice worker with its route. Without a depth word the answer shows everything grouped by hop.
5. `ticket: billing exports time out`. Approve. `who should look at it` returns the owner and the top-ranked people who know that system.
6. `who knows the payments gateway`, a system not in the graph. The agent says the graph doesn't know it, and names the nearest match.
7. Same question from a DM by someone without access to a private repo, then by a linked collaborator. Only the second sees the repo's pull requests.

Keep each answer on screen long enough to read the cited path.

## Round 2: 17 Oct evening

### Verified since round 1

Probed on the local `v4.22.1` container; each line changed a tool's design.

| Finding | Consequence |
| --- | --- |
| `*1..$depth` with a parameter fails; literal bounds work | `blast_radius` clamps depth to an int in Python and builds the literal. Only a validated integer is ever interpolated, never a name, so the "agent never writes Cypher" property holds |
| `algo.SPpaths` defaults to `relDirection: 'outgoing'`; mixed routes return nothing | `path_between` passes `relDirection: 'both'` |
| `graph.ro_query` rejects writes | Every read tool runs through it; say so in the README and the video |
| The module runs with `TIMEOUT 1000` ms and `RESULTSET_SIZE 10000` | Fine at demo size. Keep algorithm calls filtered by label and type |
| `algo.betweenness` on a five-service chain scores Auth 4.0, Billing 2.0, the rest 0 | A "connector" sentence in `blast_radius` answers is one query away |

### Competitors

Searched again: no new Track 03 entries beyond the four above. MERIDIAN is still the bar (live deployment, MCP tools, an ablation endpoint, the path drawn on a canvas). Nothing has a chat surface. Our edge holds if the agent loop and the superseded-decision move land and the video shows them.

### Item 4 decision: cut vector recall

- `sentence-transformers` pulls torch: on the order of a gigabyte on top of a `python-slim` image, plus a model download at first start, a day before the deadline. An ONNX route (`fastembed`) is lighter, roughly a 100 to 150 MB model plus a runtime, but still a new dependency and a download step.
- Vector similarity is not graph work. The organizers told judges to look for work the graph does that the agent could not do without it. Embedding search is what every retrieval demo has.
- The graph already offers a recall path a vector store cannot: `(Memory)-[:MENTIONS]->(Service|Document|Ticket|Decision)`. Expanding from the resolved entity, then one hop out, scoped by the read set, is cheap and on-message.

Decision: cut item 4 from the must-ship list, build `related_memories` instead, and state the trade-off in the blog as a choice, not a gap.

### Video storyboard

Target three minutes, hard cap three and a half. No length limit is published. Record on 18 Oct morning IST so there is an afternoon to redo it.

| Time | Shot | Say |
| --- | --- | --- |
| 0:00 to 0:15 | Title over FalkorDB Browser showing the Quillfern graph | "Agents see pieces. Company Brain puts people, services, docs, tickets, decisions and chat in one FalkorDB graph and answers by walking it." |
| 0:15 to 0:30 | Browser: one Service node, its `DEPENDS_ON`, `OWNS`, `TOUCHES` edges, a Memory with `MENTIONS` | "Slack and GitHub feed it live. Every memory carries the scope of the room it was said in." |
| 0:30 to 0:55 | Slack, public channel, demo step 1 | Read two path lines aloud. "The answer is the path." |
| 0:55 to 1:15 | Step 2, the decision trace | "Decision, who proposed and approved it, the RFC and the ticket it cites." |
| 1:15 to 1:45 | Step 3, post the message that changes the plan, ask again | "A new message changed the plan. The graph holds a new Decision that supersedes the old one, with both dates. Knowledge stays current without deleting history." |
| 1:45 to 2:10 | Step 4, `what breaks if auth goes down, one hop`, then `and two hops out` | "At depth one, four services. Two hops out the graph reaches the invoice worker through Billing and Notifications, and shows the route. A search box could not." |
| 2:10 to 2:30 | Step 5, `ticket:` then Approve, then "who should look at it" | "Writes wait for a human. After approval the ticket is in the graph, and the people are ranked by PageRank over who authored and reviewed what." |
| 2:30 to 2:45 | Step 6, a system not in the graph | "It says the graph doesn't know it and names the nearest match. It never guesses." |
| 2:45 to 2:55 | Step 7, DM from a non-collaborator, then from a linked one | "Private repo data shows only to people who can read the repo. The permission check is a graph query too." |
| 2:55 to 3:05 | Terminal, the ablation route with the graph disabled | "Without FalkorDB every answer collapses. The graph is the product." |
| 3:05 to 3:15 | README data model, deploy URL, the three tracks | Close. |

Before recording:

- Seed Quillfern and replay the scripted history on the instance the video uses. `scripts/seed_graph.py` needs a company flag (coder B).
- Record against the dev Slack workspace with the team typing the synthetic messages, not the fake Slack: real Block Kit rendering is more convincing, and invented people in a private dev workspace satisfy the data rule.
- Step 7 needs one private test repo installed on the dev GitHub App with one pull request, and one teammate linked through `link github`.
- Every reply shows the tool name and the path lines. Hold each reply on screen for three seconds.
- 1080p, microphone on, no music. Export MP4, upload unlisted, link it in the submission.
- Deploy main once after the last merge, run the seven steps against production once, then submit. The deployment is otherwise untouched until then.

### Next tasks once items 1, 2 and 5 land

- Coder A: `related_memories` (MENTIONS expansion), a connector sentence from betweenness in `blast_radius`, the ablation route, then MCP over the catalogue if time remains.
- Coder B: the company flag on `scripts/seed_graph.py`, the replay driver pointed at the dev workspace, and the blog draft from the docs (AI disclosure, the no-vector trade-off, the graph recall).
- Research: refresh the README query list against the merged catalogue, keep this checklist current, review every PR against the judge signals.

## Round 3: submission copy and the last day

### Form fields

The submit page renders only in a browser, so the exact fields are unknown until someone opens it. Every hackathon form of this kind asks for the items below; the text is ready to paste and can be trimmed to any limit.

**Title.** Company Brain

**Tagline.** A Slack agent that answers from one FalkorDB graph and shows the path behind every answer.

**Description** (about 120 words):

> Company Brain is a Slack bot over a single FalkorDB graph. People, teams, services, documents, tickets, decisions and chat are nodes and edges; Slack and GitHub feed it live. Ask who owns billing, why a decision was made, or what breaks if auth goes down, and it answers by walking the graph, then prints the edges it used as `A -[REL]-> B` lines. The agent never writes Cypher: it picks from a fixed catalogue of hand-written queries, resolves names against the graph first, and refuses to guess. Decisions that change are superseded, not overwritten, so "why did we" stays current. Permissions are a graph query too: every answer is filtered by the rooms and repos the asker can read. FalkorDB is the only store.

**How FalkorDB is central** (one paragraph, in case the form asks):

> Every answer is a traversal. Ownership, expertise, decision lineage and blast radius are edges, not search results, and the reply cites the exact path. PageRank over `DEPENDS_ON` and over authored and reviewed pull requests ranks services and people; `algo.SPpaths` explains why two things are connected. Memories, tags, channel membership and repo access live in the same graph, and the scheduler, approvals and dedupe keys live as Redis keys in the same instance. Remove FalkorDB and nothing answers.

**Tracks.** 03 Company Brain (primary), 01 Agents That Act on Connected Data, 02 Agent Memory and Coordination.

**Links.** Repository: https://github.com/codesensei-tushar/graph_hacks . Live: https://brain.noobsurd.space (health at `/healthz`). Demo video: unlisted YouTube link, added on 18 Oct. Blog: added when published.

**AI assistance disclosure** (if there is a field; otherwise it is in the README): parts of the project were written with Claude Code (Anthropic). The graph model, the tool catalogue, the scope rules and every query were decided and reviewed by the team, and every FalkorDB behaviour claimed was checked by running it.

### 18 Oct, in order

| When (IST) | What | Owner |
| --- | --- | --- |
| Morning | Last code PRs merge: agent loop, decisions, replay. Seed Quillfern and replay the history on the dev instance | coders, coordinator |
| Morning, before recording | Production's graph predates the new decisions and the app only seeds an empty graph. Refresh the seed in place, never the reset: `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/seed`. Expect `{"company": "company_brain", "mode": "merge", ...}` with `nodes_created` 3 (the two decisions and the strategy document) and a handful of `edges_created`; a second call returns 0/0/0. Then confirm in Slack that `why did we cut vector recall` answers with both dates | user |
| Late morning | Record the video to the storyboard above. Redo once if a step stumbles | team |
| Early afternoon | Publish the blog from docs/BLOG.md; link it from the README | coder B |
| Afternoon | Final merge. CI deploys main. Run the seven steps against production once | coordinator |
| Before 22:00 | Submit: repo, README, video link, live link, tracks, description above. Leave two hours for the form | user |

Nothing merges after the production run except the README link to the video and the blog.

## Production demo steps

Production keeps the Company Brain self-seed plus the real workspace and GitHub data; nobody reseeds it. The final check runs these seven moves against production after the last merge. They are the same seven moves as the Quillfern script, on this project's own graph.

| Step | Type in the workspace connected to production | Expect |
| --- | --- | --- |
| 1 | `who owns the memory layer, and who else has worked on it` | `Tushar -[OWNS]-> Memory layer`, then the real pull requests that touched `src/domain/memory` with `AUTHORED` and `REVIEWED` edges through the linked GitHub account |
| 2 | `why do we store memory in FalkorDB instead of a hosted memory API` | The decision, proposed and approved by Tushar, citing the problem statement and the Slack guide, plus the design chat |
| 3 | `why did we cut vector recall` | The current decision (recall over `MENTIONS` edges, 17 Oct, based on the strategy doc) and the one it superseded (hybrid vector recall), both dated. Coder B seeds this pair. If the live write landed, also post `decision: every model profile stays on Haiku through the demo` and ask `why haiku` again to see the chain grow |
| 4 | `what breaks if the claude api goes down`, then `three hops out` | Depth one: the LLM gateway only. Depth three: Slack workspace, turn engine, memory layer, then Slack events. The route is shown |
| 5 | `ticket: scheduler jobs run twice after a restart`, Approve, then `who should look at it` | The card names the service it will link to (Scheduler) before Approve. After Approve the ticket is in the graph and the answer is Tushar, with the edges |
| 6 | `who owns the linear integration` | A refusal: nothing in the graph matches, nearest is the MCP and tool connectors ticket or GitHub ingest. No guess |
| 7 | From a DM by a teammate whose GitHub account is not linked, then by one who is a collaborator on a private test repo installed on the production GitHub App | Only the second answer includes that repo's pull requests |

Step 4 depends on `blast_radius` taking a depth; until then ask once and read the full set. Step 5 depends on the approval card showing the resolved service. Step 7 needs the private test repo and one `link github` done the day before.

## Recording script, production

Type these exactly, in this order, in the dev Slack workspace connected to production. Each step says the tool that should answer and the edges to read aloud. Hold every reply on screen for three seconds. If a step answers differently, stop and fix before recording on; do not improvise.

**Before the first take**

- `/healthz` on production reports the git SHA of the final merge.
- The GitHub App is installed on production for this repository, so pull requests that touched `src/domain/memory` are in the graph. Check in the FalkorDB Browser with `MATCH (p:PullRequest) RETURN count(p)`; if it is zero, step 1 shows only the `OWNS` edge and the private-repo step cannot be shown.
- One private test repository is installed on the same App with one pull request by the recorder's GitHub account. The recorder has done `link github` in a DM; a second teammate has not.
- The seeded decision pair (hybrid vector recall, superseded by recall over edges) has been merged into production with `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/seed` (never `scripts/seed_graph.py` without `--merge`, which deletes the graph and the real workspace data with it). The response shows `nodes_created` 3 the first time and 0 after.
- `LLM_API_KEY` is set on production, so the footer says "picked by the model". Without it the footer says "picked by keywords"; both are honest, say which one is on screen.
- The seeded chat and document summaries name the hosted product this layer was modelled on. Decide before recording whether that is acceptable on screen; renaming them in `src/seed/company_brain.py` is a one-line seed change per entry.

| Step | Type | Tool | Read aloud |
| --- | --- | --- | --- |
| 1 | In the channel: `@Company Brain who owns the memory layer, and who else has worked on it` | `who_knows` | `Tushar -[OWNS]-> Memory layer`, then one pull request chain: `codesensei-tushar -[AUTHORED]-> graph_hacks#N -[TOUCHES]-> Memory layer`. "The answer is the path." |
| 2 | `@Company Brain why do we store memory in FalkorDB instead of a hosted memory API` | `decision_trace` | `Tushar -[PROPOSED]-> Store memory in FalkorDB instead of a hosted memory API`, `-[CITES]-> Problem statement`, `-[CITES]-> Slack guide`, and the design chat `-[ABOUT]->` the decision. "Decision, author, sources, and the conversation it came from." |
| 3 | `@Company Brain why did we cut vector recall` | `decision_trace` | The current decision with its date, then "This replaced *hybrid recall with a vector index* (decided 17 Oct)". "Knowledge stays current without deleting history." Optional live write: `@Company Brain decision: every model profile stays on Haiku through the demo`, Approve on the card that names what it replaces, then `why haiku` to show the chain grow. |
| 4 | `@Company Brain what breaks if the claude api goes down, one hop` | `blast_radius`, depth 1 | `LLM gateway -[DEPENDS_ON]-> Claude API`, and the sentence "3 more break further out; ask with more hops". Then in the same thread: `and three hops out` |
| 4b | (thread) `and three hops out` | `blast_radius`, depth 3 | `Slack workspace -[DEPENDS_ON]-> LLM gateway`, `Turn engine -[DEPENDS_ON]-> LLM gateway`, `Memory layer -[DEPENDS_ON]-> LLM gateway`, `Slack events API -[DEPENDS_ON]-> Slack workspace`. "Three hops. A search box could not." |
| 5 | `@Company Brain ticket: scheduler jobs run twice after a restart` | approval card | Read the card: "File a ticket titled *scheduler jobs run twice after a restart*, linked to *Scheduler*". Click Approve. |
| 5b | (reply after Approve) | `ingest_ticket` | `TKT-106 -[ABOUT]-> Scheduler`, `Tushar -[OWNS]-> Scheduler`. "Writes wait for a human. The card named the target before the write." |
| 5c | (thread) `who should look at it` | `who_knows` on the thread's ticket | The Ask-first sentence and `Tushar -[OWNS]-> Scheduler`. |
| 6 | `@Company Brain who owns the linear integration` | refusal | "Nothing in the graph matches “linear integration”", plus whatever nearest names it offers. "It says what it does not know, and never guesses." |
| 7 | DM from the unlinked teammate: `who knows github ingest` | `who_knows` | No private repository edges; footer reads "public channels, your DMs". |
| 7b | DM from the recorder (linked collaborator): `who knows github ingest` | `who_knows` | `<private repo>#N -[TOUCHES]-> GitHub ingest`, `codesensei-tushar -[AUTHORED]-> <private repo>#N`; footer ends with "the <private repo> repo". "Private repo data shows only to people who can read the repo. The permission check is a graph query too." |
| 8 | Terminal: `curl -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/api/proof?format=text` | proof route | The table: every question collapses without the graph except the refusal. "Take FalkorDB out and nothing answers." (Skip if item 6 has not landed.) |
| 9 | README in the browser: the Mermaid diagram, then the deploy URL and the three tracks | | Close. |

Ticket numbers (`TKT-106`, `graph_hacks#N`) depend on the day's graph; read whatever the reply shows. The ticket in step 5 is new each take; delete it between takes with `MATCH (t:Ticket {title: 'scheduler jobs run twice after a restart'}) DETACH DELETE t` in the Browser, or use a new title.

## Specs for items 6 and 7

Written for coder A to build straight after the D slice. Domain logic stays in `src/domain/`, routes stay thin, nothing here needs a model key.

### Item 6: proof that the graph does the work

What it shows: the same catalogue, the same questions, run twice, once with FalkorDB and once with every graph read returning nothing. The report lists, per question, the tool, the answer with the graph, the answer without, and whether the capability collapsed. The video shows the text form for ten seconds.

- **Mechanism.** A context variable in `src/db/falkordb_client.py`, `graph_reads_disabled`, with a `dark_graph()` context manager that sets it. While set, `connect()` returns the real client plus a proxy graph whose `query` and `ro_query` return an object with an empty `result_set` without touching FalkorDB. Nothing else changes: the resolver sees no rows and returns `found=False`, every tool returns an empty path, memory search returns nothing. The proof only runs read tools, so the proxy never has to refuse a write; still, log a warning if `query` is called with `CREATE`, `MERGE`, `SET` or `DELETE` in it.
- **Domain.** `src/domain/knowledge/proof.py`. `QUESTIONS: dict[str, list[str]]` keyed by the seeded `Company.id` (`quillfern`, `company-brain`), six read questions each: ownership, who knows, a decision, blast radius, a path or depth question, and a question about something not in the graph. `run_proof(scopes=("org_shared",)) -> ProofReport`. Dataclasses: `ProofOutcome(answer, found, steps)`, `ProofRow(question, tool, with_graph, without_graph, collapsed)`, `ProofReport(company, rows, collapsed, total)`. Each question goes through `ask_service.route` and the catalogue, never the model, so the result is deterministic. `collapsed = with_graph.found and with_graph.steps > 0 and not (without_graph.found and without_graph.steps > 0)`. The not-in-graph question is expected to refuse both ways; its row says so, which is the point: the refusal does not depend on the graph being up.
- **Route.** `GET /api/proof` in `src/api/v1/proof.py`, bearer `BRAIN_ADMIN_TOKEN` exactly like `/slack/status` (503 until set, 401 with no token, 403 on a wrong one). Returns the report as JSON; `?format=text` returns a fixed-width table for the terminal shot. One function call in the route, nothing else.
- **Tests.** Unit: the proxy returns empty result sets and never opens a connection. Integration on Quillfern: `collapsed == total - 1`, every `with_graph` row has at least one step, the `without_graph` answers are refusals, and the route answers 401 and 200.
- **README.** One line under the query section: the curl and what the report means.

### Item 7: the same tools over MCP

Verified on this machine in a scratch venv, not the shared one. Add the dependency to `requirements.txt`, install it in the shared venv, and tell the others.

- **Package.** `mcp==2.2.0`, the official Python SDK, released 7 Sep 2026 (2.3.0 of 2 Oct is also past two weeks). Wheel 357 KB; with its dependencies 57 MB of site-packages in a venv that already holds fastapi and uvicorn, so roughly 20 to 25 MB net on the image. New transitive packages: `httpx2`, `jsonschema`, `mcp-types`, `opentelemetry-api`, `python-multipart`, `sse-starlette`, `typing-inspection`. Python 3.10 or newer; the image is 3.12.
- **2.x API.** `FastMCP` is renamed: `from mcp.server.mcpserver import MCPServer`. `server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)` returns a Starlette app. Mount it with `app.mount("/mcp", sub)` and run `async with server.session_manager.run(): yield` inside `main.py`'s lifespan. Verified: `initialize`, `tools/list`, `tools/call` and session `DELETE` all answered 200, and a tool returning a dict comes back as JSON text plus structured content. Two traps: the sub-app's own path defaults to `/mcp`, so without `streamable_http_path="/"` the endpoint is `/mcp/mcp`, which nginx does not proxy; and the client's tool objects use `input_schema`, not `inputSchema`. Host checks: calls through `127.0.0.1` and `localhost` succeeded with the defaults and with `host="brain.noobsurd.space"`; `TransportSecuritySettings(enable_dns_rebinding_protection, allowed_hosts, allowed_origins)` exists if we want to pin the public name. Remove the 501 placeholder routes from `src/api/ops.py`.
- **Mapping.** One MCP tool per `ToolSpec` with `needs_approval=False`: same name, same description, `args_schema` passed as the input schema verbatim (use the SDK path that accepts an explicit schema rather than deriving one from a Python signature; the low-level `Server` with `list_tools` and `call_tool` handlers if `MCPServer` cannot take a schema). The handler runs `spec.run(args, scopes)` and returns `ToolResult.to_dict()`, so an MCP caller gets the same answer, path, `found` and `candidates` as Slack. Writing tools are not exposed: the server is read-only, like FalkorDB's own MCP server in read-only mode.
- **Scope and auth.** MCP callers read `org_shared` only, stated in the README: there is no Slack identity to derive a wider read set from. Auth: a new `MCP_TOKEN` setting (not the admin token, which must not be shared), checked as a bearer header by a small Starlette middleware around the mounted app, 503 when unset. The token goes to the judges in the private submission notes, never in the repo.
- **Tests.** Unit: the MCP tool list equals the read-only catalogue names and schemas. Integration: start the FastAPI app in a thread and use `mcp.client.streamable_http.streamable_http_client` plus `ClientSession` to list tools and call `who_owns` on Quillfern, asserting the path in the structured result (this is exactly the probe that passed here).
- **Why it matters.** Track 01 names tool selection through MCP as a focus, the organizers' reference build exposes its six tools over MCP, and MERIDIAN already ships eight. It also makes the Track 02 "handoff" claim true: the Slack agent and any MCP client walk one graph under one permission model.

### Tracks 01 and 02: what each needs from the submission

From the track briefs on the hackathon page. The same build enters all three; the copy must say what each track is looking for in its own words.

| Track 01 focus | What we show | Copy line |
| --- | --- | --- |
| Multi-hop reasoning | `blast_radius` at a chosen depth, `path_between` over `algo.SPpaths` | "Depth is a parameter; the route comes back with the answer." |
| Graph queries and algorithms as agent tools | The catalogue, PageRank for load-bearing services, betweenness for connectors | "Ranking and routing are a `CALL`, not code." |
| Tool selection through MCP | Item 7 | "The same catalogue over MCP, read-only, `org_shared`." |
| Actions based on what the graph shows | Tickets filed after approval, routed to the people the graph ranks | "It acts, after a human clicks Approve on a card that names the target." |
| Explainable results | Path lines, the picked-by footer, the proof route | "Explanation is the traversal." |

| Track 02 focus | What we show | Copy line |
| --- | --- | --- |
| Episodic, semantic, procedural memory | Dated Memory nodes per room; a tag topic tree and `MENTIONS` edges; a workspace prompt | Say "episodic and semantic"; do not claim procedural memory beyond the workspace prompt |
| Persistent conversation history | Memories, bot threads and a seven-day event ring, all in FalkorDB | "It remembers between sessions because the session is in the graph." |
| Shared state and handoffs between agents | One graph and one permission model shared by the Slack agent and MCP clients; approvals as the human handoff | Say "single agent in Slack, plus any MCP client"; do not claim a multi-agent crew |
| High-concurrency reads and writes | FalkorDB, background jobs and the scheduler on one instance | No numbers; we have no benchmark |
| Separate memory per user or tenant | Scopes per person, private channel and repo; one install per workspace | This is the strongest Track 02 line; lead with it |

Honesty rule for the copy: Track 03 is primary; Track 01 is credible once the D slice and MCP land; Track 02 rests on isolation and persistence, and the description says "single agent" plainly.

## Sources

- [Graph Hacks overview](https://www.wemakedevs.org/hackathons/falkordb), [rules](https://www.wemakedevs.org/hackathons/falkordb/rules), [schedule](https://www.wemakedevs.org/hackathons/falkordb/schedule), [resources](https://www.wemakedevs.org/hackathons/falkordb/resources)
- [How to build an agent that reasons over a graph](https://www.wemakedevs.org/blogs/how-to-build-an-agent-that-reasons-over-a-graph), the organizers' reference build
- FalkorDB docs: [algorithms](https://docs.falkordb.com/algorithms), [PageRank](https://docs.falkordb.com/algorithms/pagerank), [vector indexing](https://docs.falkordb.com/cypher/indexing/vector-index), [MCP server](https://docs.falkordb.com/genai-tools/mcpserver), [Graphiti](https://docs.falkordb.com/agentic-memory/graphiti)
- [GraphRAG, AI agents and memory in 2026](https://www.falkordb.com/?p=19048), FalkorDB's own positioning
- [Glean knowledge graph](https://docs.glean.com/security/knowledge-graph), [enterprise brain glossary](https://www.unifyapps.com/resources/glossary/enterprise-brain)
- Competitors: [MERIDIAN](https://github.com/aniruddhaadak80/meridian-graphhacks), [Axon](https://github.com/volfiros/axon-brain), [Minutes](https://github.com/Hydra-Of-Malice/FalkorDB), [dealership-brain](https://github.com/rossycoder/dealership-brain)
- Judging in general: [How to win hackathons](https://getpushtoprod.substack.com/p/how-to-win-hackathons), [Lessons from winning five hackathons](https://sustained.substack.com/p/lessons-from-winning-five-hackathons)
