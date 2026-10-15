# Company Brain — problem statement

Track 03 of [Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb). Deadline: **18 Oct 2026, 11:59 PM IST**.

## What we are building

An agent that understands how one company works. People, teams, projects, services, documents, tickets, chats, and decisions live in **one FalkorDB graph**. The agent answers questions by walking those connections, and every answer shows the path it used.

The graph is the product. It decides what the agent can know and how it explains itself.

## What it must do

1. **One graph for company knowledge.** Load docs, tickets, and chats into the same graph as people, teams, and projects.
2. **Who owns what, and who knows what.** Answer ownership and expertise from edges, not from a search box over flat text.
3. **Trace a decision.** Follow a decision back to the person, the doc, and the ticket that produced it.
4. **Stay current.** A new ticket or chat message updates the graph, and the next answer uses that update.
5. **Show sources.** Each answer returns the nodes and relationships it walked.

## Demo questions

These are the questions the demo has to answer against the sample company:

| Question | What the graph must return |
| --- | --- |
| Who owns billing, and who else has worked on it? | Owner, plus people linked through past work |
| Why did we move the memory layer off Postgres? | Decision, author, source doc, and source ticket |
| What breaks if the auth service goes down? | Services that depend on auth |
| A new ticket just landed about a billing bug. Who should look at it? | Owner and people who know that system, after the ticket is written into the graph |

## How it works

```
docs / tickets / chats
        |
        v
   ingest  -->  FalkorDB graph  <--  Cypher tools
                      ^                    |
                      |                    v
                 new events           agent answer
                                      + path (sources)
```

1. Seed a fictional company into FalkorDB (people, teams, services, docs, tickets, one decision).
2. The agent does not guess relationships. It calls tools that run Cypher: who owns a thing, who has worked on it, decision trace, blast radius, and ingest.
3. The UI shows the answer and the path (nodes and edges) behind it.
4. Ingest writes a new ticket or message into the graph so a later question sees it.

## Out of scope for the first cut

- Real Slack, Jira, or Google Drive connectors
- Login and multi-tenant graphs
- A general chatbot that answers without a graph path

## Done when

- FalkorDB is the only store for company knowledge.
- The four demo questions work on the seeded graph.
- Ingesting one new ticket changes a later answer.
- Every answer cites the path it used.
