# The answer is the path: a company brain on FalkorDB

*Draft for the Graph Hacks 2026 blog post. Placeholders are in double brackets. Sections marked "verify" describe behaviour that was still landing when this was written; check each against main before publishing. Target length 1,400 to 1,800 words, five images.*

*Built in four days for Graph Hacks 2026, Track 03 Company Brain. Repo: https://github.com/codesensei-tushar/graph_hacks . Live: https://brain.noobsurd.space . Video: [[link]].*

## The question that needs a path

A ticket lands in Slack on a Friday afternoon: "Annual plan renewals charge twice on retry." Who should look at it?

Nobody can answer that from one record. The answer is the person who owns the billing service, plus the engineer who changed the retry code last month, plus the reviewer who approved that pull request, plus whoever wrote the runbook that says what a retry is supposed to do. Four facts in four systems, joined by relationships nobody wrote down.

A search box returns documents. A graph returns people, and the reason each of them is on the list. That is the whole idea behind what we built.

## What we built

Company Brain is a Slack bot over one FalkorDB graph. People, teams, services, documents, tickets, decisions and chat are nodes and edges. Slack and GitHub feed it live: messages in channels the bot has joined become scoped memories, and pull requests and reviews become edges between people and the services whose code they touched. You ask in Slack, in a channel or a DM, and every answer ends with the edges it walked, written as `A -[REL]-> B`.

[[Image 1: the Slack reply to "a billing bug just landed, who should look at it", with the path lines visible.]]

We entered Track 03, and Tracks 01 and 02 with the same build, because the agent acts on connected data and the memory it keeps is the graph.

## The graph

Four groups of nodes share one graph.

- **The company.** Company, Team, Person, Service, Document, Ticket, Decision, Chat. Services point at each other with `DEPENDS_ON`. People point at services with `OWNS`, `WORKED_ON` and `KNOWS`. Decisions point at what they `CITE`.
- **Slack.** SlackWorkspace, SlackUser, Channel, and the membership edges that record who is in which private channel.
- **Memory.** Memory nodes in a Scope, tagged into a topic tree, with `MENTIONS` edges into the company nodes they talk about.
- **GitHub.** Repo, GitHubUser, PullRequest. A pull request `TOUCHES` the services whose paths it changed, and a GitHubUser `IS` a Person once the accounts are linked.

One convention does most of the work. Every node written from Slack or GitHub carries a `scope`: `org_shared` for public channels and public repos, `slack_channel_<id>` for a private channel, `user_<id>` for one person's DMs with the bot, `github_repo_<id>` for a private repo. Seeded company knowledge has no scope and counts as shared. Every query ends with the same filter, `coalesce(n.scope, 'org_shared') IN $scopes`, where `$scopes` is what the asker may read. The full model, label by label, is in the README.

[[Image 2: FalkorDB Browser, one Service with its DEPENDS_ON, OWNS and TOUCHES edges and a Memory with MENTIONS.]]

## The agent never writes Cypher

This is the design decision the rest of the post hangs on.

The model does not write queries. It picks a tool from a fixed catalogue and fills in arguments. Each tool is a `ToolSpec`: a name, a description the model reads, a JSON schema for the arguments, and a `run(args, scopes)` function with a hand-written, parameterised traversal behind it. Each tool returns a `ToolResult`: a short answer and a list of `PathStep`s, the exact edges it walked, which the reply prints as `Arjun Mehta -[OWNS]-> Billing Service`.

The loop is one structured call to Claude Haiku, on the cheapest profile we have. The reply has to match a schema: `tool` is an enum of the catalogue's names, `args` are the tools' own argument schemas. We check the choice against the registry, drop arguments the tool did not declare, clamp integers to their bounds and insist on the required ones. If there is no API key, the call fails, or the choice is invalid, a keyword router picks the tool instead. Every reply ends with the tool's name and whether the model or the keywords picked it, so the trace is on the screen, not in a log. Tools that write the graph never run from the loop. The turn suspends with an Approve/Deny card whose text is the tool's own preview of what it will write, with the target already resolved: "File a ticket titled *Billing exports time out*, linked to *Billing Service*". The write runs only after the asker approves, and the ticket command accepts `about <service>` when the title alone would be a guess.

Before any tool runs, the name in the question is resolved against the graph. A name resolves only when a node's name, alias or title appears in the question as a whole phrase, within the asker's scopes; the longest phrase wins. Two different nodes named side by side count as one unknown thing, not two answers. If nothing resolves, the tool returns `found = false` with up to three candidates, and the reply says exactly what the graph does know:

> Nothing in the graph matches “payments gateway” as one thing. Nearest: Billing Service (known as “payments”), API Gateway (known as “gateway”).

It never guesses.

Two things fall out of this. No model-generated string ever reaches a query, so there is nothing to inject and nothing to escape. And the explanation is not a sentence the model composes afterwards; it is the traversal itself. The organizers' guide warned that a post-hoc explanation "reads like reasoning and is reconstruction". That matched what we had seen, so we built the path into the return type.

Without a model key, a keyword router picks the tool instead, and every path still works. The test suite never calls a model.

[[Image 3: the refusal with candidates, demo step 6.]]

## Decisions that stay current

Most company-knowledge demos show a decision log. Few show what happens when a decision changes.

In our graph a Decision has a date, points at the Service it is about, and cites the documents and tickets behind it. When a message in a channel changes the plan ("we're moving billing retries to the invoice worker, decided in today's sync"), the bot writes a new Decision that `SUPERSEDES` the old one, `DECIDED_IN` that channel, `BASED_ON` whatever it cites. Ask "why did we" and you get the current decision, the one it replaced, both dates, and the message it came from. Nothing is deleted. The history is a chain of edges. [[Verify after the decision PR merges; adjust edge names if they changed.]]

[[Image 4: the same "why did we" question before and after the message.]]

## Permissions are a graph query too

A memory is written to exactly one scope, the narrowest room it was said in. What you can read depends on where you ask.

- A public channel reads `org_shared`.
- A private channel adds its own scope.
- A DM adds your own scope, every private channel you are a member of, and every private repo your linked GitHub account can read.

The last one is a single pattern: `(:SlackUser)-[:IS]->(:Person)<-[:IS]-(:GitHubUser)-[:CAN_READ]->(:Repo)`. Collaborator lists are re-synced every hour, so when someone loses access to a repo their DMs stop showing that repo's pull requests. If a channel's privacy cannot be confirmed, it is treated as private. If an account is not linked, the answer is no. Everything fails closed.

## Algorithms as tools, and three things that bit us

FalkorDB ships graph algorithms as procedures, which means ranking and routing are a `CALL` rather than code we had to write.

- `algo.pageRank('Service', 'DEPENDS_ON')` says which service the most others lean on, the one to worry about first, and the reply adds how many services depend on it.
- `algo.SPpaths` answers "why is Maya connected to the invoice worker": `Maya -[OWNS]-> Auth <-[DEPENDS_ON]- Billing <-[DEPENDS_ON]- Invoice Worker`, every shortest route considered, the one with the strongest edges shown.
- `algo.betweenness` names the connector among the services a failure hits, the one most shortest paths run through.
- Blast radius walks `DEPENDS_ON` to a depth you choose and groups what it finds by hop.

One thing we tried and dropped: PageRank for people. "Who knows billing" sounded like a ranking problem, but PageRank scores by incoming links and people have almost none; every person came out between 0.011 and 0.015. So people are ranked by a weighted sum of their edges to the service (owning it counts five, writing a pull request three, reviewing two), and the reply shows the reasons: "Ask first: Arjun Mehta (owns it, wrote notebook#52)". The ranking is explainable because it is made of the same edges the path prints.

Three things we learned by running them against FalkorDB 4.22, not by reading about them:

1. **A parameter is not accepted as a variable-length bound.** `MATCH p=(s)<-[:DEPENDS_ON*1..$depth]-(d)` fails. We clamp the depth to an integer in Python and build the literal. The "no model string reaches a query" rule still holds, because only a validated integer is interpolated.
2. **`algo.SPpaths` follows outgoing edges by default.** A route from a person to a service returned nothing until we passed `relDirection: 'both'`, because `OWNS` and `DEPENDS_ON` point different ways.
3. **The documented `algo.CDLP` is not registered on that build.** `algo.labelPropagation` is, and does the same job.

Blast radius is the one to watch in the video. At depth one, Auth going down takes out the gateway, billing, notifications and search. One hop further it reaches the invoice worker through Billing, and the reply shows the route. A search box cannot do that.

[[Image 5: blast radius at one hop and two hops out, side by side.]]

## What we cut, and why

We did not add vector search, and we want to be clear that this was a choice.

The honest reason is weight: an embedding model would have added about a gigabyte to a slim image a day before the deadline. The better reason is that similarity search is not graph work. The judges were asked to look for work the graph does that the agent could not do without it, and nearest-neighbour text retrieval is what every retrieval demo already has.

So recall follows edges. From the entity a question resolved to, through `MENTIONS`, to the memories that talk about it, then one hop out to the memories about its neighbours, all filtered by what you may read, with FalkorDB's full-text index as the first pass. A second tool, `recall`, runs the full-text index over what you may read for questions that name no node at all, and returns dated lines and no path. Semantic recall is the first thing we would add next. FalkorDB has the vector index for it, and the Memory node is already shaped for an `embedding` property.

## One store

FalkorDB holds the graph. It also holds, as Redis keys in the same instance, the scheduler queue, pending approvals, running turns, and the dedupe keys for Slack events and GitHub deliveries. The app talks to one process. Production runs the same compose file behind nginx, with a password on the database and its port closed to the internet, and the deploy pipeline ships by git SHA and rolls back by SHA. The details are in docs/DEPLOY.md.

Take FalkorDB out and nothing answers. Not "answers worse". Nothing.

## How we used AI assistants

The rules ask for this, and it deserves a real paragraph rather than a footnote.

Claude Code wrote a large share of the code and the docs, in several sessions that coordinated through pull requests. The team decided the graph model, the tool catalogue, the scope rules and the demo, read every query that went in, and checked every claim about FalkorDB behaviour in this post by running it against the database. The three gotchas above were found that way. [[Team: one or two sentences on what you would do differently.]]

## What's next

Expose the same catalogue over MCP, so other agents can walk the graph with the same permissions. Add semantic recall next to the full-text pass. Add more sources, each one as more edges rather than another store.

The line we kept coming back to: the answer is the path.

---

*Repo: https://github.com/codesensei-tushar/graph_hacks . Live: https://brain.noobsurd.space . Video: [[link]]. Graph model and queries: the README.*

## Before publishing

- Every claim about FalkorDB behaviour matches the feature table in docs/STRATEGY.md.
- Every "verify" note is resolved against main and removed.
- No private data in any screenshot; the demo workspace is the synthetic company.
- Repo, deployment and video links open in a private window.
- The AI assistance paragraph is in, with the team's own sentence.
- Publish on dev.to or Medium, then link it from the README and the submission form.
