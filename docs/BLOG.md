# Blog post outline

The best blog post wins a side prize, and the organizers said what they want: "Write about what you built, how FalkorDB fits in, and what you learned along the way." This is the outline the post follows. Fill each section from the docs it points to; keep the voice plain and first person plural. Target 1,400 to 1,800 words and five images. Publish on dev.to or Medium before the submission deadline, then link it from the README and the submission form.

Working title: **The answer is the path: a company brain on FalkorDB**

Alternatives: "Agents that refuse to guess", "One graph, every answer cited".

## 1. Open with the question, not the stack

A new ticket lands: "annual renewals charge twice on retry". Who should look at it? The answer is not in any one record. It is the owner of the billing service, the person who touched the retry code last week, and the reviewer who approved that pull request. Three hops. Say that a search box returns documents, and a graph returns the people.

Image 1: the Slack reply to demo step 5, with the path lines visible.

## 2. What we built, in one paragraph

A Slack bot over one FalkorDB graph. People, teams, services, documents, tickets, decisions and chat are nodes and edges. Slack and GitHub feed it live. You ask in Slack, and every answer ends with the edges it walked, written as `A -[REL]-> B`. Link the repo and the live deployment. Name the three tracks entered.

## 3. The graph model

Pull the tables from the README section "Graph data model". Explain the one convention that matters: every node written from Slack or GitHub carries a `scope`, and every query filters on the asker's read set. Keep it short; link to the README for the full list.

Image 2: FalkorDB Browser showing a Service with its `DEPENDS_ON`, `OWNS`, `TOUCHES` edges and a Memory with `MENTIONS`.

## 4. The agent never writes Cypher

This is the design decision the post is about. The model picks a tool from a fixed catalogue and fills in arguments. Humans wrote every query. Names resolve against the graph before anything runs; a miss means "the graph doesn't know that" plus the nearest matches, never a guess. Show one `ToolSpec` and one `ToolResult` from `src/domain/knowledge/`. Explain why: no model string ever reaches a query, so there is nothing to inject, and the explanation is the traversal itself rather than a sentence written afterwards.

Image 3: demo step 6, the refusal with candidates.

## 5. Decisions that stay current

The Track 03 bullet nobody demos well. A message in a channel changes the plan. The bot writes a new `Decision` that `SUPERSEDES` the old one, with both dates and the message it came from. "Why did we" then answers with the current decision and the one it replaced. Nothing is deleted. Take the example from demo step 3.

Image 4: the two replies to the same question, before and after the message.

## 6. Permissions are a graph query too

Public channel reads `org_shared`. Private channel adds its own scope. A DM adds the person's scope, every private channel they are a member of, and every private repo their linked GitHub account can read. Show the two read-set queries from the README. Point out that the repo check is `SlackUser -[:IS]-> Person <-[:IS]- GitHubUser -[:CAN_READ]-> Repo`, one pattern, and that it fails closed.

## 7. Algorithms as tools, and the three things that bit us

PageRank over `DEPENDS_ON` for "what is load-bearing" and over authored and reviewed pull requests for "who knows". `algo.SPpaths` for "why is Maya connected to the invoice worker". Then the honest part, from docs/STRATEGY.md:

- A parameter is not accepted as a variable-length bound on FalkorDB 4.22. We clamp the depth in Python and build the literal.
- `algo.SPpaths` defaults to outgoing edges, so a person-to-service route returns nothing until `relDirection: 'both'`.
- `algo.CDLP` from the docs is not registered on that build; `algo.labelPropagation` is.

Image 5: blast radius at depth one and depth four, side by side.

## 8. What we cut and why

We did not add vector search. An embedding model would have added a gigabyte to the image a day before the deadline, and similarity search is not graph work. Recall follows edges instead: from the entity a question resolved to, through `MENTIONS`, to the memories that talk about it and its neighbours. Say plainly that semantic recall is the first thing to add afterwards, and that FalkorDB has the vector index for it.

## 9. One store

FalkorDB holds the graph and, as Redis keys in the same instance, the scheduler queue, approvals, running turns and dedupe. The app talks to one process. Mention the production setup in one sentence and link docs/DEPLOY.md.

## 10. How we used AI assistants

Required by the rules, and worth a real paragraph. Claude Code wrote a large share of the code and docs. The graph model, the tool catalogue, the scope rules and every query were decided and read by the team, and the FalkorDB behaviours above were checked by running them. Say what the team would do differently.

## 11. What's next

MCP over the same catalogue, semantic recall, more sources. Two sentences. End on the line the whole post is about: the answer is the path.

## Checklist before publishing

- Every claim about FalkorDB behaviour matches the feature table in docs/STRATEGY.md.
- No private data in any screenshot; the demo workspace is the synthetic company.
- Repo, deployment and video links work in a private window.
- The AI assistance paragraph is in.
