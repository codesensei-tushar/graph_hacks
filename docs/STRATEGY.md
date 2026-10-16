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
4. `what breaks if auth goes down` at depth one, then `and two hops out`. The deeper run surfaces a service the first missed, with the path.
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
| 1:45 to 2:10 | Step 4, blast radius at depth one, then four | "At depth one, only Auth. At depth four the graph reaches Invoice Worker through Billing. A search box could not." |
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

## Sources

- [Graph Hacks overview](https://www.wemakedevs.org/hackathons/falkordb), [rules](https://www.wemakedevs.org/hackathons/falkordb/rules), [schedule](https://www.wemakedevs.org/hackathons/falkordb/schedule), [resources](https://www.wemakedevs.org/hackathons/falkordb/resources)
- [How to build an agent that reasons over a graph](https://www.wemakedevs.org/blogs/how-to-build-an-agent-that-reasons-over-a-graph), the organizers' reference build
- FalkorDB docs: [algorithms](https://docs.falkordb.com/algorithms), [PageRank](https://docs.falkordb.com/algorithms/pagerank), [vector indexing](https://docs.falkordb.com/cypher/indexing/vector-index), [MCP server](https://docs.falkordb.com/genai-tools/mcpserver), [Graphiti](https://docs.falkordb.com/agentic-memory/graphiti)
- [GraphRAG, AI agents and memory in 2026](https://www.falkordb.com/?p=19048), FalkorDB's own positioning
- [Glean knowledge graph](https://docs.glean.com/security/knowledge-graph), [enterprise brain glossary](https://www.unifyapps.com/resources/glossary/enterprise-brain)
- Competitors: [MERIDIAN](https://github.com/aniruddhaadak80/meridian-graphhacks), [Axon](https://github.com/volfiros/axon-brain), [Minutes](https://github.com/Hydra-Of-Malice/FalkorDB), [dealership-brain](https://github.com/rossycoder/dealership-brain)
- Judging in general: [How to win hackathons](https://getpushtoprod.substack.com/p/how-to-win-hackathons), [Lessons from winning five hackathons](https://sustained.substack.com/p/lessons-from-winning-five-hackathons)
