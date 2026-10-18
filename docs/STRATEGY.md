# Hackathon strategy

Research for the final stretch of [Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb), Track 03 Company Brain. Submissions close **18 Oct 2026, 11:59 PM IST**. This page records what the organizers reward, where this project stands against the field, which FalkorDB features are confirmed to work on our image, and what to build in what order. The problem statement itself is in [PROBLEM.md](PROBLEM.md).

Short version: enter all three tracks with one build, keep Track 03 primary, and spend the remaining time on the agent loop over a fixed Cypher tool catalogue that returns the graph path with every answer.

## Submission checklist

From the [rules](https://www.wemakedevs.org/hackathons/falkordb/rules) and [overview](https://www.wemakedevs.org/hackathons/falkordb). There is no published scoring rubric.

| Required | Status |
| --- | --- |
| Public source repository | **Not yet.** The repository is private (`gh repo view` on 18 Oct, 14:20 IST). The user makes it public before submitting; step 6 of "18 Oct, in order" |
| README with setup instructions **and the graph data model** | Done. README, "Graph data model". Vector recall was cut (round 2), so no vector index is documented |
| **The Cypher queries or graph algorithms the product depends on**, in the README | Done. README, "Cypher queries the product depends on", with the injection audit |
| Demo video of the working project | Not yet. Record to "Recording script, Quillfern on production (primary)", with the Sales beat from round 6 on the local replay |
| Live deployment, or complete local setup | Both. `brain.noobsurd.space` serves Quillfern (DEPLOY.md), and `scripts/replay_workspace.py` is the local setup. Every merge to `main` redeploys and drops connections for a few seconds; keep it up through judging |
| Tracks named at submission | Enter 01, 02 and 03. Each has its own prize. |
| FalkorDB is the primary database and "supports a central part of the product" | Yes. It is the only store, including the Redis keys. The ask page's proof shows 5 of 6 capabilities collapsing without it |
| Only public, synthetic or your own data | Done. Production and the replay serve Quillfern, a synthetic company. Never load a real customer workspace |
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

Status on 18 Oct: all six are closed or cut on purpose.
- In Slack the model picks from a fixed catalogue of read tools when `LLM_API_KEY` is set, and keywords pick otherwise; the public page always uses keywords.
- Decisions supersede each other with dates.
- `load_bearing`, `path_between` and `working_groups` call `algo.pageRank`, `algo.SPpaths` and `algo.labelPropagation`.
- Every reply prints its path, and the page draws it.
- The proof runs on the ask page.
- Semantic recall was cut in favour of `related_memories` over `MENTIONS` edges (round 2).

What is still open is listed in "18 Oct, in order".

Visible Track 03 competitors on GitHub as of 17 Oct. Re-checked 18 Oct, 14:30 IST:
- MERIDIAN pushed a ReAct agent loop, a multi-agent debate and centrality algorithms on 17 Oct.
- The Axon and Minutes repositories now answer 404.
- Atlas has had no commit since 12 Oct.

| Entry | What it does | Threat |
| --- | --- | --- |
| [MERIDIAN](https://github.com/aniruddhaadak80/meridian-graphhacks) | Four agents on one graph, graph per tenant, MCP with eight tools, an ablation endpoint that shows which capabilities collapse without the graph, live on Vercel, enters all three tracks | High |
| [Cairn](https://dev.to/bikash_kh_/i-built-a-company-brain-that-remembers-why-decisions-were-made-on-falkordb-c7) | Decision, premise and quoted source as a graph with a "still true?" check, web app deployed, blog published (see Round 4) | High for Track 03 and the blog prize |
| [Axon](https://github.com/volfiros/axon-brain) | "Remembers why your team decided things, and flags when new work goes against it" | Medium. Best decision-tracing pitch; still in design when indexed |
| [Minutes](https://github.com/Hydra-Of-Malice/FalkorDB) | Time-aware shared memory from meetings and email, Tracks 02 and 03 | Medium |
| [dealership-brain](https://github.com/rossycoder/dealership-brain) | Multi-agent brain for one vertical | Low |

Business framing judges will recognise: the "people, content, activity" knowledge graph that [Glean](https://docs.glean.com/security/knowledge-graph) sells for expertise search, plus the shared "enterprise brain" memory layer vendors now pitch under agents. At least one hosted Slack knowledge assistant shut down in September 2026 and open-sourced its harness, so a self-hosted brain where the graph is both the memory and the permission model is a live gap.

## FalkorDB features confirmed on our image

Checked on 17 Oct against the local `falkordb/falkordb:v4.22.1` container by running each call on a throwaway graph. The production compose runs the same version.

| Feature | Call that worked | Notes |
| --- | --- | --- |
| PageRank | `CALL algo.pageRank('Label', 'REL') YIELD node, score` | Positional arguments; `null` means all |
| Betweenness | `CALL algo.betweenness({nodeLabels: [...], relationshipTypes: [...]}) YIELD node, score` | Config map form. Positional form returned nothing. **Sampled**: the scores and the top node change with `samplingSeed`, and the default seed changes across server restarts. On both seeded graphs the default names a different top node than exact Brandes over the same edges (Quillfern exact: Auth 2.5, Billing 2.0; FalkorDB: Billing 5.0, Auth 2.5). Used in no answer since round 6 |
| Shortest paths | `CALL algo.SPpaths({sourceNode: a, targetNode: c, relTypes: ['REL'], maxLen: 5}) YIELD path, pathWeight` | Bind the nodes with `MATCH` first |
| Components | `CALL algo.WCC({nodeLabels: [...]}) YIELD node, componentId` | |
| Communities | `CALL algo.labelPropagation({nodeLabels: [...]}) YIELD node, communityId` | `algo.CDLP` from the docs is **not registered** on this build. Deterministic: `working_groups` gave the same clusters in separate client processes, on a fresh server and after a restart |
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
| Connectors | Counted from the routes `blast_radius` walked; `connectors` is an exact Cypher bridge query | Decided in round 6, not `algo.betweenness`: the sampled algorithm named the wrong connector (row above), and a counted route can be checked against the printed path |
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

Superseded by round 6's "Storyboard, consolidated" and, for production, "Recording script, Quillfern on production (primary)".

Run against the synthetic company, in Slack, in this order:

1. `who owns billing, and who else has worked on it` in a public channel. Owner, then people linked by past pull requests, with the path.
2. `why did we move the memory layer off Postgres`. Decision, author, source doc, source ticket.
3. Post `we're moving billing retries to the invoice worker, decided in today's sync` in the channel. Ask question 2's equivalent for billing. The answer shows the new decision, says what it superseded, and when.
4. `what breaks if auth goes down, one hop`, then `and two hops out`. The first answer says how many more break further out; the second surfaces the invoice worker with its route. Without a depth word the answer shows everything grouped by hop.
5. `ticket: billing exports time out`. Approve. `who should look at it` returns the owner and the top-ranked people who know that system.
6. `who knows the payments gateway`, a system not in the graph. The agent says the graph doesn't know it, and names the nearest match.
7. Same question from a DM by someone without access to a private repo, then by a linked collaborator. Only the second sees the repo's pull requests.
8. `who actually works together`. Label propagation. Arjun, Lena and Tomas share Billing even though Tomas is on Platform; Kofi and Maya share the memory layer across Data and Platform. Sara knows Billing and stays outside that cluster.
9. `is the billing retries decision still the last word`, after the scripted pull requests are in. It cites the runbook and TKT-102, and names the pull requests that touched Billing and were not cited.

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
| `algo.betweenness` on a five-service chain scores Auth 4.0, Billing 2.0, the rest 0 | A "connector" sentence in `blast_radius` answers is one query away (superseded in round 6: the algorithm is sampled, see the feature table) |

### Competitors

Searched again: no new Track 03 entries beyond the four above. MERIDIAN is still the bar (live deployment, MCP tools, an ablation endpoint, the path drawn on a canvas). Nothing has a chat surface. Our edge holds if the agent loop and the superseded-decision move land and the video shows them.

### Item 4 decision: cut vector recall

- `sentence-transformers` pulls torch: on the order of a gigabyte on top of a `python-slim` image, plus a model download at first start, a day before the deadline. An ONNX route (`fastembed`) is lighter, roughly a 100 to 150 MB model plus a runtime, but still a new dependency and a download step.
- Vector similarity is not graph work. The organizers told judges to look for work the graph does that the agent could not do without it. Embedding search is what every retrieval demo has.
- The graph already offers a recall path a vector store cannot: `(Memory)-[:MENTIONS]->(Service|Document|Ticket|Decision)`. Expanding from the resolved entity, then one hop out, scoped by the read set, is cheap and on-message.

Decision: cut item 4 from the must-ship list, build `related_memories` instead, and state the trade-off in the blog as a choice, not a gap.

### Video storyboard

Superseded by the consolidated storyboard in round 6. Target three minutes, hard cap three and a half. No length limit is published. Record on 18 Oct morning IST so there is an afternoon to redo it.

| Time | Shot | Say |
| --- | --- | --- |
| 0:00 to 0:15 | Title over FalkorDB Browser showing the Quillfern graph | "Agents see pieces. Company Brain puts people, services, docs, tickets, decisions and chat in one FalkorDB graph and answers by walking it." |
| 0:15 to 0:30 | Browser: one Service node, its `DEPENDS_ON`, `OWNS`, `TOUCHES` edges, a Memory with `MENTIONS` | "Slack and GitHub feed it live. Every memory carries the scope of the room it was said in." |
| 0:30 to 0:55 | Slack, public channel, demo step 1 | Read two path lines aloud. "The answer is the path." |
| 0:55 to 1:15 | Step 2, the decision trace | "Decision, who proposed and approved it, the RFC and the ticket it cites." |
| 1:15 to 1:45 | Step 3, post the message that changes the plan, ask again | "A new message changed the plan. The graph holds a new Decision that supersedes the old one, with both dates, and it quotes the message it was recorded from. Knowledge stays current without deleting history." |
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

**Description** (about 150 words; rewritten in round 6 for every team):

> Company Brain is a Slack bot over a single FalkorDB graph. People, teams, what they run, documents, tickets, decisions and chat are nodes and edges. Slack and GitHub feed it live, and any team, not only engineering, joins from one TOML file. Ask who owns billing, why a decision was made, what breaks if auth goes down, what rests on one person alone if they leave, or where the company is one person away from an outage. It answers by walking the graph, then prints the edges it used as `A -[REL]-> B` lines. The agent never writes Cypher: it picks from a fixed catalogue of hand-written queries, resolves names against the graph first, and refuses to guess. Decisions are superseded, not overwritten, so "why did we" stays current. Every answer is filtered by the rooms and repos the asker can read. Every read-only tool is also served over MCP and cites its path. FalkorDB is the only store.

**How FalkorDB is central** (one paragraph, in case the form asks):

> Every answer is a traversal. Ownership, expertise, decision lineage, blast radius and what rests on one person are edges, not search results, and the reply cites the exact path. PageRank over `DEPENDS_ON` ranks the services the company leans on, label propagation finds who actually works together, and `algo.SPpaths` explains why two things are connected. People are ranked by their own edges (owning, writing, reviewing), so the ranking is the path. A team's processes, tools and playbooks are the same `Service` nodes with a `kind`, so every tool answers for every team. Memories, tags, channel membership and repo access live in the same graph, and the scheduler, approvals and dedupe keys live as Redis keys in the same instance. Remove FalkorDB and nothing answers.

**Tracks.** 03 Company Brain (primary), 01 Agents That Act on Connected Data, 02 Agent Memory and Coordination.

**Links.** Repository: https://github.com/codesensei-tushar/graph_hacks . Live: https://brain.noobsurd.space (health at `/healthz`). Demo video: unlisted YouTube link, added on 18 Oct. Blog: added when published.

**AI assistance disclosure** (if there is a field; otherwise it is in the README): parts of the project were written with Claude Code (Anthropic). The graph model, the tool catalogue, the scope rules and every query were decided and reviewed by the team, and every FalkorDB behaviour claimed was checked by running it.

### 18 Oct, in order

Rewritten 18 Oct, 14:45 IST. **Code freeze 20:00 IST; submit by 22:00** (the form closes 23:59). Every merge to `main` redeploys production and drops connections for a few seconds, so the lead holds merges during a take. Checked on production at 14:20 IST, before #115 and #116: `/api/proof` and `/mcp` answered 503, the graph holds 1 decision (the refresh adds the rest), and the repository is private.

| # | When (IST) | What | Owner |
| --- | --- | --- | --- |
| 1 | Now | **Tokens.** `/api/proof` and `POST /ops/seed` answer 503 because `BRAIN_ADMIN_TOKEN` is not in Secret Manager. Create it with `read -rs V && printf '%s' "$V" \| gcloud secrets create BRAIN_ADMIN_TOKEN --data-file=- --configuration=graphhacks; unset V` (value from `openssl rand -hex 32`). `MCP_TOKEN` is optional since #115: `/mcp` is public and rate-limited, and a token only lifts the limit. Keep every token out of the repo | user |
| 2 | Right after 1 | **Redeploy**: `gh workflow run deploy.yml`, or let the next merge do it. Verify `curl -s -o /dev/null -w '%{http_code}' https://brain.noobsurd.space/api/proof` prints 401, not 503 | user |
| 3 | Before 4 | **SEED_COMPANY: done in #116.** `POST /ops/seed` merges whatever `SEED_COMPANY` names, and it defaults to `company_brain`. `deploy.sh` writes only the listed secrets into the VM's `.env`, so #116 sets `SEED_COMPANY: quillfern` in `deploy/docker-compose.yml`. Step 4's response proves it | done |
| 4 | After 3 | **Seed refresh, then the CITES cleanup.** `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/seed`. The response must say `"company": "quillfern"`; stop if it says anything else. Then in the Browser at https://gdb.noobsurd.space: `MATCH (d:Decision)-[c:CITES]->(s) WHERE (d)-[:BASED_ON]->(s) DELETE c`. Details in step 3 of "Recording script, Quillfern on production (primary)" | user |
| 5 | A minute after 4 | **`python scripts/check_live.py`: 16 passed, 0 failed.** Before the refresh it reads 15 of 15, because the page hides the billing-retries chip until that decision exists | user or critic |
| 6 | Before recording | **Make the repository public** (rule 10): `gh repo edit codesensei-tushar/graph_hacks --visibility public --accept-visibility-change-consequences`. Secrets live only in Secret Manager (DEPLOY.md) | user |
| 7 | After 5, ideally after the 20:00 freeze | **Record** to "Recording script, Quillfern on production (primary)", plus the Sales beat on the local replay (round 6). 1080p. Redo once if a step stumbles | user, team |
| 8 | After 7 | **Blog**: publish from docs/BLOG.md, then link it and the video from the README (the only merges allowed after the freeze) | user, coordinator |
| 9 | After 7 | **Deck**: put the video link in place of `[video link]` on the deck's last slide and in docs/PITCH.md | user |
| 10 | By 22:00 | **Submit** with the copy in "Form fields": repo, live link, video, tracks 03, 01 and 02, blog. `/mcp` needs no token since #115 | user |

## Production demo steps, self-seed (superseded)

**Superseded.** Written for the self-seed. Production serves Quillfern, so record to "Recording script, Quillfern on production (primary)" instead.

Production keeps the Company Brain self-seed plus the real workspace and GitHub data; nobody reseeds it. The final check runs these seven moves against production after the last merge. They are the same seven moves as the Quillfern script, on this project's own graph.

| Step | Type in the workspace connected to production | Expect |
| --- | --- | --- |
| 1 | `who owns the memory layer, and who else has worked on it` | `Tushar -[OWNS]-> Memory layer`, then the pull requests that touched `src/domain/memory`: `Tushar -[AUTHORED]-> graph_hacks#N -[TOUCHES]-> Memory layer`, reviewers as `-[REVIEWED]->` |
| 2 | `why do we store memory in FalkorDB instead of a hosted memory API` | The decision, "Decided 15 Oct 2026. Proposed by Tushar.", `-[BASED_ON]->` the problem statement and the Slack guide, plus the design chat |
| 3 | `why did we cut vector recall` | "Recall follows MENTIONS edges, vector search cut. Decided 17 Oct 2026." then "This replaced *Hybrid recall with a vector index on Memory.embedding* (decided 17 Oct 2026)", with `-[SUPERSEDES]->` and `-[BASED_ON]-> docs/STRATEGY.md` in the path. Seeded; the production refresh carries it |
| 4 | `what breaks if the claude api goes down, one hop`, then in the thread `and three hops out` | One hop: only `LLM gateway -[DEPENDS_ON]-> Claude API`, and "4 more break further out (... Slack events API at depth 3)". Three hops: Slack workspace, Turn engine, Memory layer, then Slack events API; "Depth 3 reached 5 services; nothing new past depth 3." |
| 5 | `ticket: scheduler jobs run twice after a restart`, Approve, then in the thread `who should look at it` | The card reads "File a ticket titled *scheduler jobs run twice after a restart* in the company graph, linked to *Scheduler*". After Approve: `TKT-106 -[ABOUT]-> Scheduler`, `Tushar -[OWNS]-> Scheduler`. The follow-up answers "TKT-106 is about Scheduler. Ask first: Tushar (owns Scheduler)." |
| 6 | `who owns the linear integration` | A refusal: nothing in the graph matches, nearest is the MCP and tool connectors ticket or GitHub ingest. No guess |
| 7 | From a DM by a teammate whose GitHub account is not linked, then by one who is a collaborator on a private test repo installed on the production GitHub App | Only the second answer includes that repo's pull requests |

Step 4 depends on `blast_radius` taking a depth; until then ask once and read the full set. Step 5 depends on the approval card showing the resolved service. Step 7 needs the private test repo and one `link github` done the day before.

## Recording script, self-seed (superseded)

**Superseded.** Written for the self-seed. Production serves Quillfern, so record to "Recording script, Quillfern on production (primary)" instead.

Type these exactly, in this order, in the dev Slack workspace connected to production. Each step says the tool that should answer and the edges to read aloud. Hold every reply on screen for three seconds. If a step answers differently, stop and fix before recording on; do not improvise.

**Before the first take**

- `/healthz` on production reports the git SHA of the final merge.
- `BRAIN_ADMIN_TOKEN` and `MCP_TOKEN` exist in Secret Manager and the redeploy has run: `/api/proof` and `/mcp` answer 401 without a token, not 503.
- The GitHub App is installed on production for this repository, so pull requests that touched `src/domain/memory` are in the graph. Check in the FalkorDB Browser with `MATCH (p:PullRequest) RETURN count(p)`; if it is zero, step 1 shows only the `OWNS` edge and the private-repo step cannot be shown.
- One private test repository is installed on the same App with one pull request by the recorder's GitHub account. The recorder has done `link github` in a DM; a second teammate has not.
- The production seed refresh runs after the alias fix from #39 is on main (it is), so `who knows github ingest` resolves instead of refusing as ambiguous.
- The seeded decision pair (hybrid vector recall, superseded by recall over edges) has been merged into production with `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/seed` (never `scripts/seed_graph.py` without `--merge`, which deletes the graph and the real workspace data with it). The response shows `nodes_created` 3 the first time and 0 after.
- `LLM_API_KEY` is set on production, so the footer says "picked by the model". Without it the footer says "picked by keywords"; both are honest, say which one is on screen.
- The seeded chat and document summaries name the hosted product this layer was modelled on. Decide before recording whether that is acceptable on screen; renaming them in `src/seed/company_brain.py` is a one-line seed change per entry.

| Step | Type | Tool | Read aloud |
| --- | --- | --- | --- |
| 1 | In the channel: `@Company Brain who owns the memory layer, and who else has worked on it` | `who_knows` | `Tushar -[OWNS]-> Memory layer`, then one pull request chain: `Tushar -[AUTHORED]-> graph_hacks#N`, `graph_hacks#N -[TOUCHES]-> Memory layer`, and a reviewer's `-[REVIEWED]->` line. "The answer is the path." |
| 2 | `@Company Brain why do we store memory in FalkorDB instead of a hosted memory API` | `decision_trace` | "Decided 15 Oct 2026. Proposed by Tushar.", then `Tushar -[PROPOSED]-> Store memory in FalkorDB instead of a hosted memory API`, `-[BASED_ON]-> Problem statement`, `-[BASED_ON]-> Slack guide`, and the design chat `-[ABOUT]->` the decision. "Decision, author, sources, and the conversation it came from." |
| 3 | `@Company Brain why did we cut vector recall` | `decision_trace` | "Recall follows MENTIONS edges, vector search cut. Decided 17 Oct 2026." and "This replaced *Hybrid recall with a vector index on Memory.embedding* (decided 17 Oct 2026)"; path lines `Recall follows MENTIONS edges, vector search cut -[SUPERSEDES]-> Hybrid recall with a vector index on Memory.embedding` and `-[BASED_ON]-> docs/STRATEGY.md`. "Knowledge stays current without deleting history." Optional live write: `@Company Brain decision: every model profile stays on Haiku through the demo`, Approve on the card that names what it replaces, then `why haiku` to show the chain grow. |
| 4 | `@Company Brain what breaks if the claude api goes down, one hop` | `blast_radius`, depth 1 | Exactly one path line, `LLM gateway -[DEPENDS_ON]-> Claude API`, and the reply, whose opening the production demo test pins word for word: "If Claude API goes down, these stop with it: LLM gateway (1 hop). 4 more break further out (Memory layer at depth 2, Slack workspace at depth 2, Turn engine at depth 2 and Slack events API at depth 3); ask with more hops. Among them, LLM gateway is the main connector: 4 of the 5 reach Claude API through it." Then in the same thread: `and three hops out` |
| 4b | (thread) `and three hops out` | `blast_radius`, depth 3, subject from the thread | `Slack workspace -[DEPENDS_ON]-> LLM gateway`, `Turn engine -[DEPENDS_ON]-> LLM gateway`, `Memory layer -[DEPENDS_ON]-> LLM gateway`, `Slack events API -[DEPENDS_ON]-> Slack workspace`, and "Depth 3 reached 5 services; nothing new past depth 3. Among them, LLM gateway is the main connector: 4 of the 5 reach Claude API through it." "Three hops. A search box could not." |
| 5 | `@Company Brain ticket: scheduler jobs run twice after a restart` | approval card | Read the card: "File a ticket titled *scheduler jobs run twice after a restart* in the company graph, linked to *Scheduler*". Click Approve. |
| 5b | (reply after Approve) | `ingest_ticket` | `TKT-106 -[ABOUT]-> Scheduler`, `Tushar -[OWNS]-> Scheduler`. "Writes wait for a human. The card named the target before the write." |
| 5c | (thread) `who should look at it` | `who_knows`, subject from the thread | "TKT-106 is about Scheduler. Ask first: Tushar (owns Scheduler)." with `TKT-106 -[ABOUT]-> Scheduler` and `Tushar -[OWNS]-> Scheduler`. |
| 6 | `@Company Brain who owns the linear integration` | refusal | "Nothing in the graph matches “linear integration”", plus whatever nearest names it offers. "It says what it does not know, and never guesses." |
| 7 | DM from the unlinked teammate: `who knows github ingest` | `who_knows` | No private repository edges; footer reads "public channels, your DMs". |
| 7b | DM from the recorder (linked collaborator): `who knows github ingest` | `who_knows` | `<private repo>#N -[TOUCHES]-> GitHub ingest`, `codesensei-tushar -[AUTHORED]-> <private repo>#N`; footer ends with "the <private repo> repo". "Private repo data shows only to people who can read the repo. The permission check is a graph query too." |
| 8 | Terminal: `curl -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/api/proof?format=text` | proof route | The table: every question collapses without the graph except the refusal. "Take FalkorDB out and nothing answers." (Skip if item 6 has not landed.) |
| 9 | README in the browser: the Mermaid diagram, then the deploy URL and the three tracks | | Close. |

Ticket numbers (`TKT-106`, `graph_hacks#N`) depend on the day's graph; read whatever the reply shows. The ticket in step 5 is new each take; delete it between takes with `MATCH (t:Ticket {title: 'scheduler jobs run twice after a restart'}) DETACH DELETE t` in the Browser, or use a new title.

## Recording script, Quillfern on production (primary)

**This is the script to record.** Production serves Quillfern, not the self-seed: `/api/ask/tools` reports company `quillfern`, and the teams are Data, Payments and Platform (rechecked 18 Oct, 14:20 IST). The two self-seed scripts above are superseded. It follows the Quillfern replay, whose replies `tests/integration/test_demo_replay.py` pins. Steps 1 and 6 and the bus-factor reply were checked on production read-only that morning; step 2 answers there too, but without dates or the decision it replaced until the refresh. Steps 1 to 8 were rechecked word for word on 18 Oct at `1ecb932`, on a fresh Quillfern seed (production after the refresh) through the replay's fake Slack, with and without the Sales import: every quoted line reads as written, and the import changes none of them.

**Before the first take, in this order**

1. `BRAIN_ADMIN_TOKEN` in Secret Manager, then redeploy (steps 1 and 2 of "18 Oct, in order"); the refresh, the import and step 8 need it. Without a token `/api/proof` answers 503 while the secret is unset (still so on 18 Oct at `7ede9c6`) and 401 once it is set. Since #115 `/mcp` needs no token: it is public and rate-limited, and `MCP_TOKEN` only lifts the limit.
2. **`SEED_COMPANY` is `quillfern` on production since #116** (`deploy/docker-compose.yml`), so `POST /ops/seed`, `/ops/import` and `onboard team:` all join Quillfern. Before #116 it was unset and defaulted to `company_brain`, and a refresh would have merged the self-seed into Quillfern. Step 3's response still confirms it.
3. Refresh the seed: `curl -X POST -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" https://brain.noobsurd.space/ops/seed`. The response must say `"company": "quillfern"`. Production's Quillfern predates the current seed: its one decision cites through `CITES`, with no date and no `SUPERSEDES`, and the billing retries decision that step 3 replaces is missing. The refresh adds both missing decisions, the dates, `BASED_ON` and `SUPERSEDES`, and the seed's six dated public-channel memories: 13 nodes (the memories, their six Slack speakers and the `org_shared` scope) and 27 edges (`IN_SCOPE`, `SAID` and 15 `MENTIONS`), measured on a trimmed graph at `994c817`. They fill each team page's "What was said", answer the chip `what have we said about billing`, and add a *From conversations* list to some Slack replies (step 2 below). It also leaves the old `CITES` edges, so step 2 then lists the RFC and TKT-101 twice, in "Based on:" and in the path. Remove them in the Browser: `MATCH (d:Decision)-[c:CITES]->(s) WHERE (d)-[:BASED_ON]->(s) DELETE c`. All of this was checked on a scratch graph trimmed to look like production's (before the seed had memories, the refresh reported 2 nodes and 12 edges created). The ask page caches each answer for a minute, so wait a minute before checking there.
4. Optional, so the whole video stays on production: import Sales with `curl -X POST https://brain.noobsurd.space/ops/import -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" --data-binary @examples/teams/quillfern-sales.toml`, as the recording's Sales beat (13 nodes, 32 edges on a fresh Quillfern), then `/team/sales`, S2 and `what if priya shah leaves` as in round 6. Otherwise the Sales beat stays on the local replay.
5. A minute after the refresh, and after any redeploy, `python scripts/check_live.py` passes. It sends only public GETs and checks every page chip, the README's four questions, `/team/payments` and the `/?q=` deep link. Since #114 the page lists a chip only when it answers with a path on the live graph: before the refresh production lists 8, without `is the billing retries decision still the last word` (that decision is missing; checked 18 Oct at `7ede9c6`) and without `what have we said about billing` (no memories yet), and a minute after the refresh it lists 10. Before #114 the billing retries chip was `check_live`'s one failure. With the three `/mcp` checks the run reads 18 of 18 before the refresh and 20 of 20 after it (both run locally at `994c817`, on a fresh seed and on one trimmed to look like production's).

| Step | Type in the dev Slack workspace | Tool | Read aloud |
| --- | --- | --- | --- |
| 1 | `@Company Brain who owns billing, and who else has worked on it` | `who_knows` | "Billing Service is owned by Arjun Mehta." Then the ranked people and their path lines. "The answer is the path." |
| 2 | `@Company Brain why did we move the memory layer off Postgres` | `decision_trace` | "Proposed by Kofi Mensah", "Approved by Tomas Reyes", the RFC and TKT-101 it rests on, and after the refresh "This replaced *Keep notebook links in Postgres join tables* (decided 1 Jun 2026)" and, under *From conversations*, Tomas's "signed off on moving the memory layer to the graph store, Kofi owns the migration" (learned 2026-09-15) and Sara's "I'll run the graph store for the memory layer, same box as search" (learned 2026-09-11) |
| 3 | Post `we're moving billing retries to the invoice worker, decided in today's sync`, then `@Company Brain decision: move billing retries to the invoice worker`. Approve, then ask `why did we move billing retries to the invoice worker` | `record_decision`, then `decision_trace` | The card says what it replaces: *Retry renewal webhooks inside the billing service* (decided 20 Sep 2026). The trace quotes your message: "Decided in #<channel> on <date> by <you>: “we're moving billing retries ...”" |
| 4 | `@Company Brain what breaks if auth goes down, one hop`, then in the thread `and two hops out` | `blast_radius` | One hop: "... API Gateway, Billing Service, Notification Service and Search Service (1 hop). 1 more breaks further out (Invoice Worker at depth 2) ...". Two hops: "... Invoice Worker (2 hops). Depth 2 reached 5 services; nothing new past depth 2. Among them, Billing Service and Notification Service are the main connectors: 1 of the 5 reaches Auth Service through each." |
| 5 | `@Company Brain ticket: billing exports time out`, Approve, then in the thread `who should look at it` | `ingest_ticket`, then `who_knows` | The card names *Billing Service*; then "Filed *TKT-N* about Billing Service" and the ranked people |
| 6 | `@Company Brain who knows the payments gateway` | refusal | "Nothing in the graph matches “payments gateway” as one thing. Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”)." |
| 7 | Not live: real Slack users are not Quillfern's people, and its private repo is scripted. Show the local replay's two DM replies in the terminal instead | `who_knows` | Kofi's DM has no `billing-internal` line (footer "public channels, your DMs, #engineering"); Arjun's adds `billing-internal#7` and its footer ends "the quillfern/billing-internal repo". "The permission check is a graph query too." |
| 8 | Terminal: `curl -H "Authorization: Bearer $BRAIN_ADMIN_TOKEN" "https://brain.noobsurd.space/api/proof?format=text"` | proof | "5 of 6 capabilities collapse without FalkorDB", and the refusal row "refused both ways". `tests/integration/test_knowledge_proof.py` pins this on the current seed. Production's old seed reads the same through the public `GET /api/ask/proof` (JSON, no token; on 18 Oct at `7ede9c6`, 5 of 6 collapsed and the refusal refused both ways), so the text route needs only `BRAIN_ADMIN_TOKEN` set |

Between takes, delete the two writes in the Browser: `MATCH (d:Decision {name: 'Move billing retries to the invoice worker'}) DETACH DELETE d` and `MATCH (t:Ticket {title: 'billing exports time out'}) DETACH DELETE t`. With the new decision gone, the old one stands again.

## Round 4: 17 Oct, late morning

### Rules and schedule

Re-read both pages: unchanged. Eighteen rules, no scoring rubric, no video length, submissions close 18 Oct 23:59 IST. Rule 10 still requires a public repository; ours is private until the user flips it.

### Competitors, three new

| Entry | What it is | What it means for us |
| --- | --- | --- |
| [Cairn](https://dev.to/bikash_kh_/i-built-a-company-brain-that-remembers-why-decisions-were-made-on-falkordb-c7) | A "why was this decided" brain: Decision `BECAUSE` Premise `EVIDENCED_BY` Source, every reason tied to a quote checked word for word, a "still true?" check that links later postmortems back to decisions, betweenness and label propagation, a graph per company, a deployed web app, a published blog with a self-built 40-question benchmark against vector and keyword RAG | The strongest Track 03 story so far and the blog to beat for the side prize. Web app only, data from exports, no live Slack or Jira, no approval flow, no per-room permissions. Our edge: live Slack and GitHub, the permission graph, writes behind approval, a decision that supersedes instead of a flag. Our blog must carry the proof numbers and the honest refusal, which their post has no equivalent of |
| [Atlas](https://github.com/ikrishg/atlas) | A Slack company brain that shows the path for every answer ("kill test": multi-hop routes with labelled edges). Slack app, ingestion and deployment not built; hand-written seed | Same pitch sentence as ours, far behind on delivery. Watch for a last-day Slack app |
| [TeamTrail](https://github.com/kollisathwikkumar/TeamTrail) | Track 03 onboarding brain built on top of an upstream extraction project, with notes saying work began before the event | Likely conflicts with rule 8; not a reference point |

MERIDIAN, re-checked: video still a placeholder, live console on Vercel, eight MCP tools, an `/api/proof` ablation that reports 8 of 8 capabilities collapsing, MIT licence. Two things to say plainly in our README and blog, without naming anyone: our proof reports 5 of 6 because the sixth question is a refusal that stays a refusal without the graph, and that is the behaviour judges were told to look for; an ablation where every question collapses includes the questions the system should have refused. And we have no LICENSE file where the others do; the user decides.

### Carry into the copy

- Blog: one sentence under "What we cut" with the proof numbers ("five of six questions collapse without FalkorDB; the sixth is a refusal either way, and that is the point").
- README: already states the refusal behaviour; add the proof sentence next to the curl.
- Submission description: unchanged; the "how FalkorDB is central" paragraph already names the proof route.

## Round 5: every team, not only engineering (17 Oct, afternoon)

### Does it help or dilute?

It helps, with one condition. The Track 03 brief says "connect people, teams, projects, documents, and decisions" and "how a company works"; an engineering-only graph answers half of that. Showing one non-engineering team with the same seven moves proves the graph model is the company's, not the codebase's. The condition: it is the same tools, the same path citations, the same refusal and the same approval card, on new nodes. A second product would dilute the demo; a second team strengthens it.

What the design must not do, sent to the coders directly:

- No new store, no new label family. `Service` gains `kind` (service, system, process, product, account, policy, playbook); every tool keeps working and the data model changes by one property.
- Accounts are not dependencies. `account` nodes carry ownership and work edges only and are excluded from blast radius and load-bearing, or the bot will say "Acme stops working".
- Wording follows `kind`: a service or system goes down, a process stalls, a product or account is affected, a policy or playbook no longer applies.
- The importer and `onboard team:` are merge-only, parameter-only (labels, kinds and relationship types from an allowlist in code), behind the admin token or an Approve card that previews what will be created. The README's injection audit must stay true.
- The seven engineering replies are pinned by the replay and production demo tests and by the recording script; the Sales seed is additive.

### The Sales beat

Superseded by round 6's "The Sales beat, literal".

Added after step 4 in both scripts; the recording runs it on the synthetic company, since production holds this project's own graph. Expected replies are targets until coder B's tests pin them; the recording script follows the tests, not the other way round.

| Step | Type | Tool | Read aloud |
| --- | --- | --- | --- |
| S1 | `who owns renewals, and who else has worked on it` | `who_knows` | The renewals process's owner first, then the people linked by work, and the "Ask first" sentence. "The same question, for Sales." |
| S2 | `what breaks if hubspot goes down` | `blast_radius` | The renewals process and the pricing playbook grouped by hop, with "stalls" and "no longer applies" in the sentence. "A CRM outage, traced through the process it feeds." |
| S3 | `why did we change pricing` | `decision_trace` | The current pricing decision with its date, then "This replaced *...* (decided ...)". "Decisions stay current for every team." |

Storyboard: one 25-second beat between step 4 and step 5 ("Not only engineering. The same graph, Sales onboarded with one command."), showing `onboard team: sales` and its card, then S2. Trim step 2's hold to stay under three and a half minutes.

### Copy for every team

README, under the pitch table: "Any team can be added the same way: people, the processes and systems they run, their playbooks and decisions become nodes, and the same tools answer for them. `docs/ONBOARDING-TEAMS.md` shows Sales, onboarded with one command."

Landing page, a section titled "For every team": "Engineering was first. Sales, support and operations fit the same graph: a renewals process depends on the CRM, a pricing decision supersedes the last one, and the bot answers who owns what and why, with the path, for them too."

Blog, one paragraph in "What we built": "We started with engineering because the data was ours. The model is not about code: a `Service` node has a `kind`, so a renewals process, a CRM, a pricing playbook and an account sit in the same graph as the billing service. Onboarding Sales took one command and one Approve; the tools did not change."

Submission description, one added sentence: "Any team onboards the same way; the demo adds Sales with one command."

## Round 6: the video, end to end (17 Oct, evening)

Short version: record steps 1 to 8 in Slack on production as scripted, then cut to a local replay of the synthetic company for the Sales beat in the browser, which ends on one new move, `if_person_leaves`. One storyboard below replaces the round 2 table and the round 5 beat. Replies marked "target" become exact when the test that pins them lands; the script follows the tests.

### Since round 5

| PR | What it changes for the video or the copy |
| --- | --- |
| #50 | A decision recorded from Slack is `RECORDED_FROM` the asker's message and quotes it. Step 3's voice-over and the blog say so |
| #53 | The public ask routes have a one-minute cache, a question cap and a per-address rate limit, so the ask page is safe to show and link |
| #54 | The model's tool choice works for the first time: before it, every model call failed and every turn fell back to keywords. 14 of 14 demo questions now route as scripted. The footer reads "picked by the model" only if `LLM_API_KEY` is set on production |
| #56, #64 | `kind` on `Service`. Replies are worded by kind ("stalls", "no longer applies", "is affected"); accounts never appear in a blast radius. Services and systems keep their text byte for byte |
| #57, #61 | A public page per team at `/team/<id>`, grouped by kind. Its suggested questions are only ones that answer with a path |
| #62 | The README injection audit counts 44 Cypher literals and names the kind clause and the team page's row limits |

### Where the Sales beat runs

The dev Slack workspace is wired to production, and production holds this project's own graph. Onboarding a synthetic Sales team there would write invented people into the real graph, and re-pointing the Slack app's event URL mid-recording is fragile. So the Sales beat runs in the terminal and the browser against a local replay of the synthetic company, the same setup the blog images were taken with. Production stays clean.

18 Oct: production now serves Quillfern, so the reason above no longer holds. Importing Sales there is optional step 4 of "Recording script, Quillfern on production (primary)", which works since #116 set `SEED_COMPANY`. Before #116 the import failed with "the graph has no company 'company-brain'".

The `onboard team:` Slack command stays on coder B's list straight after the importer and the Sales seed. Onboarding more teams is a product feature; the importer alone needs a terminal and an admin token. The video just does not depend on it.

### The one new move: `if_person_leaves`

Coder A is adding three any-team tools. One goes on screen:

- **`if_person_leaves`, on screen.** "What if the person who runs renewals leaves" is a question every team asks. The answer is short, made of edges the path prints (`OWNS`, `WORKED_ON`, `AUTHORED` and `REVIEWED` to `TOUCHES`), and the successors are ranked by `who_knows` itself without the leaver, so "who takes over" agrees with "who knows". Accounts count, because an account losing its owner is exactly the Sales story. The reply is capped at the lead and three items, then "And N more", so it reads in ten seconds.
- **`onboarding_brief`, not on screen.** It covers what the team page already shows, and a long reply cannot be read in the time.
- **`connectors`, not on screen.** It overlaps step 8 of the Quillfern script and the connector sentence in blast radius.

All three are read-only catalogue tools, so MCP serves them with no extra work. The README tool list names them; the video does not.

Router phrasings: "what if X leaves", "what happens if X leaves", "who covers for X", "who takes over from X". A name not in the graph gets `resolve.describe_miss` verbatim, quoting only the name.

### Storyboard, consolidated

Hard cap three and a half minutes. This table replaces the round 2 storyboard and the round 5 storyboard line.

| Time | Shot | Say |
| --- | --- | --- |
| 0:00 to 0:12 | Title over the FalkorDB Browser showing production's graph | "Agents see pieces. Company Brain puts people, services, docs, tickets, decisions and chat in one FalkorDB graph and answers by walking it." |
| 0:12 to 0:22 | Browser on the local replay graph, not production: production has no GitHub data, so no `TOUCHES` edges, and after the refresh only the six seeded memories. Use the graph set up for the Sales beat (`--steps history --no-sales`, under "Before recording" below). There, checked on 18 Oct, Memory Layer has `DEPENDS_ON` (to Graph Store, from Search Service), `OWNS` from Kofi Mensah, `TOUCHES` from notebook#41, and two memories with `MENTIONS` | "Slack and GitHub feed it live. Every memory carries the scope of the room it was said in." |
| 0:22 to 0:45 | Slack, production, recording step 1 | "The answer is the path." |
| 0:45 to 1:00 | Step 2 | "Decision, author, sources, and the conversation it came from." |
| 1:00 to 1:20 | Step 3 | "Knowledge stays current without deleting history." |
| 1:20 to 1:45 | Steps 4 and 4b | "Three hops. A search box could not." |
| 1:45 to 2:05 | Steps 5, 5b and 5c | "Writes wait for a human. The card named the target before the write." |
| 2:05 to 2:15 | Step 6 | "It says what it does not know, and never guesses." |
| 2:15 to 2:25 | Steps 7 and 7b. Cut if the private test repo and `link github` are not done | "The permission check is a graph query too." |
| 2:25 to 2:35 | Terminal, step 8, the proof route on production | "Take FalkorDB out and nothing answers." |
| 2:35 to 2:43 | Terminal, local replay: B1, the import | "Not only engineering. Sales is one file and one command." |
| 2:43 to 2:52 | Browser: B2, `/team/sales` | "The same graph: a process is run by someone, a CRM can go down, an account has an owner." |
| 2:52 to 3:04 | Ask page: B3 | "A CRM outage, traced through the process it feeds." |
| 3:04 to 3:16 | Ask page: B4 | "And if the person who runs renewals leaves, the graph says what rests on them alone and who is next in line, even from another team." |
| 3:16 to 3:26 | README: the Mermaid diagram, the deploy URL, the three tracks | Close. |

3:26 with every step in; 3:16 without step 7.

The step numbers above are the self-seed script's. On Quillfern production, use "Recording script, Quillfern on production (primary)": step 4 is one hop, then two (say "Two hops. A search box could not."), step 5 is the card, the write and `who should look at it` in one, and step 7 is the replay's two DM replies in the terminal.

### The Sales beat, literal

On the local replay. Hold every reply three seconds. Rechecked word for word on 18 Oct at `1ecb932`, set up as in "Before recording" below: B1 created 13 nodes and 32 edges (0 on the second run), and B2 to B4, S1 and S3 read as written.

| Step | Type | Answers with | Read aloud |
| --- | --- | --- | --- |
| B1 | `curl -X POST http://127.0.0.1:8010/ops/import -H "Authorization: Bearer local-demo-token" --data-binary @examples/teams/quillfern-sales.toml` | `POST /ops/import` | The merge report: 13 nodes and 32 edges created (a second run creates 0). "Not only engineering. Sales is one file and one command." |
| B2 | Open `http://127.0.0.1:8010/team/sales` | team page | The groups by kind and "Run by" on the renewals process |
| B3 | Ask page: `what breaks if hubspot goes down` | `blast_radius` | "If the HubSpot system goes down, the Renewals process stalls (1 hop) and the Pricing playbook no longer applies (2 hops). Depth 2 reached 2 things that depend on it; nothing new past depth 2. Among them, the Renewals process is the main connector: 1 of the 2 reaches the HubSpot system through it." Wording from #64 and #66, names from the Sales seed (#67) |
| B4 | Ask page: `what if priya shah leaves` | `if_person_leaves` | "If Priya Shah leaves, 2 things rest on them alone, ranked by how much depends on each. Renewals process: Priya Shah is the only one running it; next in line: Dev Patel, then Noor Haddad; 1 other depends on it. Pricing playbook: Priya Shah is its only owner; next in line: Noor Haddad, then Tomas Reyes." Point at the last name: Tomas is on Platform, so the next in line for the pricing playbook comes from another team, which the org chart would not show. Pinned by #68 |

Run S1 and S3 on the same instance before the take as checks; they are not on screen. S1, `who owns renewals, and who else has worked on it`: "The Renewals process is run by Priya Shah. Ask first: Priya Shah (owns it), then Dev Patel (worked on it) and Noor Haddad (worked on it)." S3, `why did we change pricing`: starts "Sell annual plans only at renewal." and includes "Decided 1 Sep 2026." and "This replaced *Discount monthly plans at renewal* (decided 2 Mar 2026)".

### Optional: onboard this project's own launch team, on production

A fallback if the local segment cannot be recorded, or a bonus in step 7's slot if the private repo is not ready. It shows the command live on the real graph, with real data and nothing invented. It depends on coder B's `onboard team:` command; check the exact syntax against that PR before recording.

| Step | Type | Answers with | Read aloud |
| --- | --- | --- | --- |
| L1 | `@Company Brain onboard team: launch \| members: @Tushar \| owns: Hackathon submission (process), Demo video (product), Blog post (product), Recording script (playbook)`. Pick `@Tushar` from Slack's autocomplete, so it arrives as a real mention; typed as plain text, it is refused | approval card | "Onboard *Launch*: 1 person (<name>), 1 process (Hackathon submission), 2 products (Demo video and Blog post), 1 playbook (Recording script). <name> owns all of them. Visible to public channels." Click Approve. "Onboarding is a write, so it waits for a human too." |
| L2 | `@Company Brain who runs the hackathon submission` | `who_owns` | "The Hackathon submission process is run by <name> for the Launch team." |

`<name>` is the Person name Slack provisioning gave the recorder on production. The card text comes from coder B's tests (#81). This beat writes real nodes into whichever graph production holds; if production stays Quillfern, it adds this project's launch team to the demo company, so skip it unless the user wants that.

Add only the teammates who actually work on the launch. The kinds are honest: the recording script in this file is literally a playbook. The nodes stay in production's graph after the video, which is fine because they are true.

### Before recording, added to the round 3 list

- Local replay for the Sales beat: follow "Sales, as the demo imports it" in `docs/ONBOARDING-TEAMS.md` up to the import: the fake Slack API, the app on port 8010 with `SEED_COMPANY=quillfern` and `BRAIN_ADMIN_TOKEN=local-demo-token`, then `replay_workspace.py ... --steps history --no-sales`, so Sales is not in the graph yet. B1 is the guide's import command. The replay replaces the graph named in `FALKORDB_GRAPH`, so set it to a scratch graph and never to the shared or the production one. Reset between takes by running the history step again.
- `LLM_API_KEY` on production is now worth setting (user): since #54 the model really picks, and "picked by the model" on screen is the Track 01 tool-selection signal. Run `scripts/check_model_routing.py` once with the key first. Without a key, record anyway and say "picked by keywords" is on screen.
- Browser at 1080p, light theme, zoomed so the path lines and the drawing read at video size.

### Copy to settle when coder B's PRs land

- The landing page (`src/static/index.html`) and `docs/PITCH.md` already show an `onboard team:` example. It must match the command's final syntax.
- The blog's "Onboarding Sales took one command and one Approve" is true only once `onboard team:` ships. The video shows the importer, so "one file and one command" is the safe wording until then.

## Specs for items 6 and 7

Written for coder A to build straight after the D slice. Domain logic stays in `src/domain/`, routes stay thin, nothing here needs a model key.

### Item 6: proof that the graph does the work

What it shows: the same catalogue, the same questions, run twice, once with FalkorDB and once with every graph read returning nothing. The report lists, per question, the tool, the answer with the graph, the answer without, and whether the capability collapsed. The video shows the text form for ten seconds.

- **Mechanism.** A context variable in `src/db/falkordb_client.py`, `graph_reads_disabled`, with a `dark_graph()` context manager that sets it. While set, `connect()` returns the real client plus a proxy graph whose `query` and `ro_query` return an object with an empty `result_set` without touching FalkorDB. Nothing else changes: the resolver sees no rows and returns `found=False`, every tool returns an empty path, memory search returns nothing. The proof only runs read tools, so the proxy never has to refuse a write; still, log a warning if `query` is called with `CREATE`, `MERGE`, `SET` or `DELETE` in it.
- **Domain.** `src/domain/knowledge/proof.py`. `QUESTIONS: dict[str, list[str]]` keyed by the seeded `Company.id` (`quillfern`, `company-brain`), six read questions each: ownership, who knows, a decision, blast radius, a path or depth question, and a question about something not in the graph. `run_proof(scopes=("org_shared",)) -> ProofReport`. Dataclasses: `ProofOutcome(answer, found, steps)`, `ProofRow(question, tool, with_graph, without_graph, collapsed)`, `ProofReport(company, rows, collapsed, total)`. Each question goes through `ask_service.route` and the catalogue, never the model, so the result is deterministic. `collapsed = with_graph.found and with_graph.steps > 0 and not (without_graph.found and without_graph.steps > 0)`. The not-in-graph question is expected to refuse both ways; its row says so, which is the point: the refusal does not depend on the graph being up.
- **Route.** `GET /api/proof` in `src/api/v1/proof.py`, bearer `BRAIN_ADMIN_TOKEN` with its own check: 503 until set, 401 with no token, 403 on a wrong one. (The shared admin check behind `/slack/status` and `POST /ops/seed` answers 403 for a missing token too; it stays as is until after the deadline.) Returns the report as JSON; `?format=text` returns a fixed-width table for the terminal shot. One function call in the route, nothing else.
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
| Graph queries and algorithms as agent tools | The catalogue, PageRank for load-bearing services, `algo.SPpaths` for routes, label propagation for working groups, and `risk_map`, which ranks every owned thing by what depends on it times its gaps (one person, no document, owner on another team) with PageRank beside it | "Ranking and routing are a `CALL`, not code." |
| Tool selection through MCP | Live: every read-only tool at `/mcp`, public and rate-limited; a token lifts the limit (fifteen on 18 Oct with `risk_map`; `who_should_know` and `if_person_leaves` were listed by production's `/api/ask/tools` at `45f6b00`, and a local `/mcp` serves all fifteen), same schemas, same cited results | "The same catalogue over MCP, read-only, `org_shared`." |
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
