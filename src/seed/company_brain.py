"""Our own company: the Company Brain project in this repo.

Each service lists the repo ``paths`` it owns, so GitHub pull requests that change
those files link to it.

Services are the real components under src/ plus the outside systems they call.
Dependencies follow the imports between those packages. Documents, tickets, and
decisions are the real ones from docs/ and from building the Slack layer.
"""

COMPANY = {"id": "company-brain", "name": "Company Brain"}

PEOPLE = [
    {"id": "tushar", "name": "Tushar", "role": "Founder and engineer", "github": "codesensei-tushar"},
]

TEAMS = [
    {"id": "core", "name": "Core", "members": ["tushar"]},
]

SERVICES = [
    {
        "id": "slack-events",
        "paths": ["src/api/v1/slack.py", "src/api/v1/slack_install.py", "slack_app"],
        "name": "Slack events API",
        "team": "core",
        "aliases": "slack events, events endpoint, interactions, slack routes, request url, signature check",
        "summary": "FastAPI routes in src/api/v1 that receive signed Slack events and button clicks.",
    },
    {
        "id": "slack-workspace",
        "paths": ["src/domain/slack"],
        "name": "Slack workspace",
        "team": "core",
        "aliases": "slack, slack app, bot, workspace, install, channels, membership",
        "summary": "src/domain/slack: installs, channel membership, rollout, greetings, and routing a message to a turn.",
    },
    {
        "id": "turn-engine",
        "paths": ["src/domain/slack/turn"],
        "name": "Turn engine",
        "team": "core",
        "aliases": "turn, turn loop, triage, chime, steering, reactions",
        "summary": "src/domain/slack/turn: decides whether to answer, runs the turn, and posts the reply.",
    },
    {
        "id": "slack-client",
        "paths": ["src/integrations/slack"],
        "name": "Slack client",
        "team": "core",
        "aliases": "slack client, block kit, slack sdk, message formatting",
        "summary": "src/integrations/slack: Slack Web API calls, event envelopes, and Block Kit rendering.",
    },
    {
        "id": "access-control",
        "paths": ["src/domain/access"],
        "name": "Access control",
        "team": "core",
        "aliases": "permissions, access, scopes, roles, approvals, admin, owner",
        "summary": "src/domain/access: memory scopes, owner/admin/member roles, and Approve/Deny cards.",
    },
    {
        "id": "memory-layer",
        "paths": ["src/domain/memory"],
        "name": "Memory layer",
        "team": "core",
        "aliases": "memory, memories, writeback, remember, search brain",
        "summary": "src/domain/memory: writes Slack messages to the narrowest scope and searches what the asker may read.",
    },
    {
        "id": "knowledge-tools",
        "paths": ["src/domain/knowledge"],
        "name": "Knowledge tools",
        "team": "core",
        "aliases": "knowledge tools, tool catalogue, cypher tools, graph tools, ask service, resolver",
        "summary": "src/domain/knowledge: fixed Cypher tools that answer from the graph and return the path they walked.",
    },
    {
        "id": "llm-gateway",
        "paths": ["src/integrations/llm"],
        "name": "LLM gateway",
        "team": "core",
        "aliases": "llm, model, claude, haiku, anthropic sdk",
        "summary": "src/integrations/llm/model.py: the one place model calls go through.",
    },
    {
        "id": "scheduler",
        "paths": ["src/core/scheduler.py"],
        "name": "Scheduler",
        "team": "core",
        "aliases": "scheduler, jobs, background jobs, expiry",
        "summary": "src/core/scheduler.py: background jobs such as approval expiry and channel rollout.",
    },
    {
        "id": "github-ingest",
        "paths": ["src/api/v1/github.py", "src/domain/github", "src/integrations/github"],
        "name": "GitHub ingest",
        "team": "core",
        "aliases": "github, github app, github integration, webhooks, pull requests, prs, issues, link github",
        "summary": "Writes pull requests, reviews, and issues from the GitHub App into the graph, scoped per repo.",
    },
    {
        "id": "falkordb",
        "paths": ["src/db", "src/seed", "scripts/seed_graph.py", "docker-compose.yml"],
        "name": "FalkorDB",
        "team": "core",
        "aliases": "falkordb, graph, graph database, database, redis",
        "summary": "The only store: the company graph, memories, membership, and short-lived keys.",
    },
    {
        "id": "slack-platform",
        "name": "Slack platform",
        "team": "core",
        "aliases": "slack api, slack servers",
        "summary": "Slack itself. Sends events to us and receives our replies.",
    },
    {
        "id": "github-platform",
        "name": "GitHub",
        "team": "core",
        "aliases": "github api, github.com",
        "summary": "GitHub itself. Sends webhooks to the GitHub App and answers its API reads.",
    },
    {
        "id": "claude-api",
        "name": "Claude API",
        "team": "core",
        "aliases": "anthropic, anthropic api, claude api",
        "summary": "Anthropic's Messages API, called by the LLM gateway.",
    },
]

# child depends on parent, from the imports between the packages
DEPENDS_ON = [
    ("slack-events", "slack-workspace"),
    ("slack-events", "turn-engine"),
    ("slack-events", "slack-client"),
    ("slack-events", "falkordb"),
    ("slack-workspace", "access-control"),
    ("slack-workspace", "knowledge-tools"),
    ("slack-workspace", "memory-layer"),
    ("slack-workspace", "turn-engine"),
    ("slack-workspace", "llm-gateway"),
    ("slack-workspace", "slack-client"),
    ("slack-workspace", "falkordb"),
    ("turn-engine", "access-control"),
    ("turn-engine", "llm-gateway"),
    ("turn-engine", "slack-client"),
    ("turn-engine", "falkordb"),
    ("memory-layer", "llm-gateway"),
    ("memory-layer", "slack-client"),
    ("memory-layer", "falkordb"),
    ("access-control", "falkordb"),
    ("knowledge-tools", "falkordb"),
    ("scheduler", "falkordb"),
    ("slack-client", "slack-platform"),
    ("slack-client", "falkordb"),
    ("llm-gateway", "claude-api"),
    ("github-ingest", "knowledge-tools"),
    ("github-ingest", "falkordb"),
    ("github-ingest", "github-platform"),
    ("memory-layer", "github-ingest"),
]

# (person, relation, service)
WORK = [
    ("tushar", "OWNS", "slack-events"),
    ("tushar", "OWNS", "slack-workspace"),
    ("tushar", "OWNS", "turn-engine"),
    ("tushar", "OWNS", "slack-client"),
    ("tushar", "OWNS", "access-control"),
    ("tushar", "OWNS", "memory-layer"),
    ("tushar", "OWNS", "knowledge-tools"),
    ("tushar", "OWNS", "llm-gateway"),
    ("tushar", "OWNS", "scheduler"),
    ("tushar", "OWNS", "github-ingest"),
    ("tushar", "OWNS", "falkordb"),
    ("tushar", "KNOWS", "slack-platform"),
    ("tushar", "KNOWS", "claude-api"),
    ("tushar", "KNOWS", "github-platform"),
]

DOCUMENTS = [
    {
        "id": "doc-problem",
        "name": "Problem statement",
        "service": "knowledge-tools",
        "author": "tushar",
        "summary": "docs/PROBLEM.md: one FalkorDB graph for company knowledge, answers that cite their path. Deadline 18 Oct 2026.",
    },
    {
        "id": "doc-setup",
        "name": "Setup guide",
        "service": "falkordb",
        "author": "tushar",
        "summary": "docs/SETUP.md: run FalkorDB in Docker, seed the graph, start the API.",
    },
    {
        "id": "doc-slack",
        "name": "Slack guide",
        "service": "slack-workspace",
        "author": "tushar",
        "summary": "docs/SLACK.md: creating the Slack app, tokens, the permissions graph, and what was ported from company-brain.",
    },
    {
        "id": "doc-manifest-dev",
        "name": "Slack dev manifest",
        "service": "slack-events",
        "author": "tushar",
        "summary": "slack_app/manifest.yaml: scopes, events, and request URLs for the dev app behind an ngrok tunnel.",
    },
    {
        "id": "doc-strategy",
        "name": "docs/STRATEGY.md",
        "service": "knowledge-tools",
        "author": "tushar",
        "summary": "What the judges reward, where we stand, which FalkorDB features are confirmed, and the build order with its cut line.",
    },
    {
        "id": "doc-manifest-prod",
        "name": "Slack production manifest",
        "service": "slack-events",
        "author": "tushar",
        "summary": "slack_app/manifest.prod.yaml: the same app pointed at https://brain.noobsurd.space.",
    },
]

TICKETS = [
    {
        "id": "TKT-101",
        "name": "TKT-101",
        "title": "Build the LLM agent turn",
        "service": "turn-engine",
        "owner": "tushar",
        "summary": "Answers still come from fixed Cypher tools. The model should pick the graph tool and write the reply.",
    },
    {
        "id": "TKT-102",
        "name": "TKT-102",
        "title": "Deploy to brain.noobsurd.space",
        "service": "slack-events",
        "owner": "tushar",
        "summary": "The domain has no DNS record yet. Needs DNS, HTTPS, and the production Slack app's tokens.",
    },
    {
        "id": "TKT-103",
        "name": "TKT-103",
        "title": "Read a FalkorDB password from config",
        "service": "falkordb",
        "owner": "tushar",
        "summary": "The client connects with no password, so production must keep port 6379 private until this lands.",
    },
    {
        "id": "TKT-104",
        "name": "TKT-104",
        "title": "Access leasing between teammates",
        "service": "access-control",
        "owner": "tushar",
        "summary": "company-brain lets a teammate lend a tool connection for one request. Not ported yet.",
    },
    {
        "id": "TKT-105",
        "name": "TKT-105",
        "title": "MCP and tool connectors",
        "aliases": "tool integrations, linear connector, jira connector, mcp connectors",
        "service": "turn-engine",
        "owner": "tushar",
        "summary": "GitHub, Linear, and other connectors with personal and org-shared scopes. Not ported yet.",
    },
]

DECISIONS = [
    {
        "id": "decision-falkordb-memory",
        "name": "Store memory in FalkorDB instead of a hosted memory API",
        "summary": "company-brain keeps memories in supermemory. We port its Slack layer but keep every memory, scope, and membership edge in the FalkorDB graph, so the graph stays the only store.",
        "aliases": "why falkordb, why not supermemory, memory store, hosted memory api",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "memory-layer",
        "based_on": ["doc-problem", "doc-slack"],
        "decided_at": 1_792_033_200_000,  # 15 Oct 2026 03:00 UTC
    },
    {
        "id": "decision-narrowest-scope",
        "name": "Write each memory to the narrowest room",
        "summary": "Public channels write to org_shared, private channels to their own scope, DMs to the person. A DM reads the person's scope, org_shared, and every private channel they belong to. Unknown channel privacy counts as private.",
        "aliases": "permissions graph, scopes, why scopes, private channel memory, fail closed",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "access-control",
        "based_on": ["doc-slack"],
        "decided_at": 1_792_044_000_000,  # 15 Oct 2026 06:00 UTC
    },
    {
        "id": "decision-http-events",
        "name": "Receive Slack events over HTTPS, not Socket Mode",
        "summary": "Signed HTTPS events are what Slack recommends for production, and the same code runs locally behind ngrok and in production at brain.noobsurd.space. The app token is not used.",
        "aliases": "socket mode, http events, why https, app token, request url",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "slack-events",
        "based_on": ["doc-slack", "doc-manifest-prod", "TKT-102"],
        "decided_at": 1_792_054_800_000,  # 15 Oct 2026 09:00 UTC
    },
    {
        "id": "decision-haiku",
        "name": "Use Claude Haiku 5.5 for every model profile",
        "summary": "All profiles (triage, gate, fast, memory, main) call claude-haiku-5-5; the profile only sets effort. Any model failure falls back to the no-model path.",
        "aliases": "which model, why haiku, model choice, llm model",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "llm-gateway",
        "based_on": ["TKT-101"],
        "decided_at": 1_792_144_800_000,  # 16 Oct 2026 10:00 UTC
    },
    {
        "id": "decision-vector-recall",
        "name": "Hybrid recall with a vector index on Memory.embedding",
        "summary": "Add a vector index next to the full-text index and union both result sets in search, with a local sentence-transformers encoder so no key is needed.",
        "aliases": "vector recall, hybrid recall, vector index, embeddings, semantic search",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "memory-layer",
        "based_on": ["doc-strategy"],
        "decided_at": 1_792_207_800_000,  # 17 Oct 2026 03:30 UTC (09:00 IST)
    },
    {
        "id": "decision-mentions-recall",
        "name": "Recall follows MENTIONS edges, vector search cut",
        "summary": "Memories link to the services, tickets and people they mention, and recall walks those edges from the graph instead of ranking embeddings. The vector index is cut from the plan.",
        "aliases": "mentions recall, mentions edges, vector search cut, why no vector search, no embeddings",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "memory-layer",
        "based_on": ["doc-strategy"],
        "supersedes": "decision-vector-recall",
        "decided_at": 1_792_225_800_000,  # 17 Oct 2026 08:30 UTC (14:00 IST)
    },
]

CHATS = [
    {
        "id": "chat-port-slack-layer",
        "name": "Design chat on porting company-brain's Slack layer",
        "summary": "Read company-brain's Slack and permissions docs, kept the scopes and the event handling, and replaced supermemory with FalkorDB.",
        "author": "tushar",
        "about": "decision-falkordb-memory",
    },
    {
        "id": "chat-slack-setup",
        "name": "Slack app setup chat",
        "summary": "Only the bot token and signing secret are required. The app token is for Socket Mode, which we don't use.",
        "author": "tushar",
        "about": "decision-http-events",
    },
]
