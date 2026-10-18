"""Our own company: the Company Brain project in this repo (codesensei-tushar/graph_hacks).

Each service lists the repo ``paths`` it owns, so GitHub pull requests that change those
files link to it (``TOUCHES``, longest prefix wins). A path is a directory or one file;
``src/api/v1/console`` would not match ``console_people.py``, so files are listed one by one.

Services are the real areas of the repo plus the outside systems they call. ``DEPENDS_ON``
follows the imports between those areas (child imports parent), keeping the edges with real
weight and leaving out the few back-references that would only make cycles. People are the
three contributors by GitHub login; who ``OWNS`` an area is whoever wrote most of its
commits (``git log --format=%an -- <path>``), and the others who committed there
``WORKED_ON`` it. Decisions are the real ones from docs/STRATEGY.md and this week's pull
requests, dated by when the pull request merged, proposed by its author and approved by
whoever merged it. ``pull_requests`` links a decision ``BASED_ON`` those pull requests once
GitHub ingest has written them (a later seed run links them; until then they are skipped).

This seed is merged next to Quillfern in the legacy graph (``POST /ops/seed?company=company_brain``),
so no id here may be one Quillfern uses, and no name or alias may take a Quillfern name
("memory layer", "graph", "gateway", "search", "auth", "hubspot", "crm"). ``tests/integration/test_demo_seed.py``
checks both.
"""

COMPANY = {"id": "company-brain", "name": "Company Brain"}

# The repo this seed describes: decisions cite its pull requests by number.
REPO = "codesensei-tushar/graph_hacks"

PEOPLE = [
    {"id": "tushar", "name": "Tushar Umbarkar", "role": "Founder and engineer: answers, Slack, the public site", "github": "codesensei-tushar"},
    {"id": "yash", "name": "Yash Patil", "role": "Engineer: connectors, console, tenancy, infra", "github": "noobsurd11"},
    {"id": "om", "name": "Om Sherikar", "role": "Engineer: GitHub alerts and who_should_know", "github": "omsherikar"},
]

TEAMS = [
    {
        "id": "cb-answers",
        "name": "Answers",
        "summary": "The question side of Company Brain: the knowledge tools, Slack, memory, MCP and the public site.",
        "members": ["tushar", "om"],
    },
    {
        "id": "cb-infra",
        "name": "Infra",
        "summary": "Everything under the answers: connectors, the console, tenancy, the scheduler, the graph client, deploy and backups.",
        "members": ["yash"],
    },
]

SERVICES = [
    # --- connectors -------------------------------------------------------------------------
    {
        "id": "cb-connectors",
        "paths": ["src/connectors", "src/api/v1/connectors.py", "docs/CONNECTORS.md"],
        "name": "Connector framework",
        "team": "cb-infra",
        "aliases": "connectors framework, connectors, connector sync, shared sync, content hash, connector writes",
        "summary": "src/connectors: the shared contract, sync loop, OAuth and batched graph writes every external tool (Jira, HubSpot) goes through.",
    },
    {
        "id": "cb-jira",
        "paths": ["src/connectors/jira", "scripts/seed_jira.py", "docs/JIRA.md"],
        "name": "Jira connector",
        "team": "cb-infra",
        "aliases": "jira, jira sync, jira cloud connector",
        "summary": "src/connectors/jira: tickets, assignees, BLOCKS links and pull request references from Jira Cloud.",
    },
    {
        "id": "cb-hubspot",
        "paths": ["src/connectors/hubspot", "scripts/seed_hubspot.py", "docs/HUBSPOT.md"],
        "name": "HubSpot connector",
        "team": "cb-infra",
        "aliases": "hubspot sync, hubspot integration",
        "summary": "src/connectors/hubspot: accounts, their owners and who worked their deals.",
    },
    {
        "id": "github-ingest",
        "paths": ["src/api/v1/github.py", "src/domain/github", "src/integrations/github", "docs/GITHUB.md"],
        "name": "GitHub ingest",
        "team": "cb-answers",
        "aliases": "github integration, github app, github webhooks, pull request ingest, link github",
        "summary": "Writes pull requests, reviews, and issues from the GitHub App into the graph, scoped per repo, and sends the new-pull-request heads-up.",
    },
    # --- console and tenancy ----------------------------------------------------------------
    {
        "id": "cb-console",
        "paths": [
            "src/api/v1/console_connectors.py",
            "src/api/v1/console_oauth.py",
            "src/api/v1/console_people.py",
            "src/api/v1/console_teams.py",
            "src/api/v1/auth.py",
            "src/api/console_web.py",
            "src/api/console_fake.py",
            "src/static/console",
            "src/static/console.css",
            "src/domain/console",
            "docs/CONSOLE.md",
        ],
        "name": "Console",
        "team": "cb-infra",
        "aliases": "admin console, console ui, sign in with slack, console api, connections page",
        "summary": "The admin console: Sign in with Slack, console sessions and CSRF, invites, the Connections, People and Teams pages, and their JSON API.",
    },
    {
        "id": "cb-tenancy",
        "paths": ["src/core/tenancy.py", "src/domain/orgs", "src/api/v1/org_ops.py"],
        "name": "Tenancy",
        "team": "cb-infra",
        "aliases": "orgs, org context, graph per org, multi tenancy, tenancy write strict",
        "summary": "src/core/tenancy.py and src/domain/orgs: every org gets its own graph, and every graph call runs in its org.",
    },
    {
        "id": "scheduler",
        "paths": ["src/core/scheduler.py"],
        "name": "Scheduler",
        "team": "cb-infra",
        "aliases": "background jobs, job queue, worker pool, leased claims",
        "summary": "src/core/scheduler.py: background jobs in Redis (approval expiry, channel rollout, connector syncs) run by a worker pool with leased claims and a fast lane.",
    },
    # --- answers ----------------------------------------------------------------------------
    {
        "id": "knowledge-tools",
        "paths": ["src/domain/knowledge", "src/api/v1/ask.py", "scripts/check_model_routing.py"],
        "name": "Knowledge tools",
        "team": "cb-answers",
        "aliases": "tool catalogue, cypher tools, graph tools, ask service, resolver, keyword router, agent",
        "summary": "src/domain/knowledge: the fixed Cypher tools, the resolver, the keyword router and the agent that picks a tool; every answer returns the path it walked.",
    },
    {
        "id": "cb-mcp",
        "paths": ["src/api/mcp.py", "src/domain/knowledge/mcp_tools.py"],
        "name": "MCP server",
        "team": "cb-answers",
        "aliases": "mcp, mcp endpoint, model context protocol",
        "summary": "/mcp: the read-only tool catalogue over MCP, no token needed, org_shared only, rate-limited like the ask page.",
    },
    {
        "id": "llm-gateway",
        "paths": ["src/integrations/llm"],
        "name": "LLM gateway",
        "team": "cb-answers",
        "aliases": "llm, model calls, haiku, anthropic sdk",
        "summary": "src/integrations/llm/model.py: the one place model calls go through.",
    },
    {
        "id": "cb-slack",
        "paths": ["src/domain/slack", "src/api/v1/slack.py", "src/api/v1/slack_install.py", "slack_app", "docs/SLACK.md"],
        "name": "Slack layer",
        "team": "cb-answers",
        "aliases": "slack app, slack bot, slack events, events endpoint, interactions, workspace install, channel rollout",
        "summary": "Signed Slack events and interactions, installs, channel membership and rollout, and routing a message to a turn.",
    },
    {
        "id": "turn-engine",
        "paths": ["src/domain/slack/turn"],
        "name": "Turn engine",
        "team": "cb-answers",
        "aliases": "turn, turn loop, triage, chime, steering, reactions",
        "summary": "src/domain/slack/turn: decides whether to answer, runs the turn, and posts the reply.",
    },
    {
        "id": "slack-client",
        "paths": ["src/integrations/slack"],
        "name": "Slack client",
        "team": "cb-answers",
        "aliases": "block kit, slack sdk, slack web api, message formatting",
        "summary": "src/integrations/slack: Slack Web API calls, event envelopes, and Block Kit rendering.",
    },
    {
        "id": "access-control",
        "paths": ["src/domain/access"],
        "name": "Access control",
        "team": "cb-answers",
        "aliases": "permissions, memory scopes, roles, approvals, approve card",
        "summary": "src/domain/access: memory scopes, owner/admin/member roles, and Approve/Deny cards.",
    },
    {
        "id": "cb-memory",
        "paths": ["src/domain/memory"],
        "name": "Slack memory layer",
        "team": "cb-answers",
        "aliases": "slack memory, brain memory, memory writeback, remember, search brain",
        "summary": "src/domain/memory: writes Slack messages as memories to the narrowest scope, links what they mention, and recalls what the asker may read.",
    },
    {
        "id": "cb-site",
        "paths": [
            "src/static",
            "src/api/web.py",
            "src/api/v1/explorer.py",
            "src/api/v1/graph.py",
            "src/api/v1/proof.py",
            "src/api/v1/teams.py",
            "docs/DESIGN.md",
        ],
        "name": "Public site",
        "team": "cb-answers",
        "aliases": "landing page, ask page, public ask page, website, explorer, team pages, tour",
        "summary": "brain.noobsurd.space: the landing page and public ask page, the graph explorer, team pages and the guided tour.",
    },
    {
        "id": "cb-onboarding",
        "paths": ["src/domain/onboarding", "scripts/import_team.py", "docs/ONBOARDING-TEAMS.md"],
        "name": "Team onboarding",
        "team": "cb-infra",
        "aliases": "team import, team file, onboard team, toml import",
        "summary": "src/domain/onboarding: a team joins from one TOML file, checked against the graph before anything is written.",
    },
    {
        "id": "cb-seeds",
        "paths": ["src/seed", "scripts/seed_graph.py", "scripts/replay_workspace.py", "scripts/replay"],
        "name": "Seed data",
        "team": "cb-answers",
        "aliases": "seeds, quillfern seed, self seed, replay",
        "summary": "src/seed: the Quillfern demo company and this self-seed, merged by id so a refresh keeps history.",
    },
    # --- infra ------------------------------------------------------------------------------
    {
        "id": "falkordb",
        "paths": ["src/db"],
        "name": "FalkorDB",
        "team": "cb-infra",
        "aliases": "falkordb client, falkor, graph client, redis graph",
        "summary": "The only store and src/db, its client: the org graphs, memories, membership, scheduler keys and short-lived keys.",
    },
    {
        "id": "cb-deploy",
        "paths": ["deploy", ".github", "Dockerfile", "docker-compose.yml", "docs/DEPLOY.md", "docs/SETUP.md"],
        "name": "Deploy and CI",
        "team": "cb-infra",
        "aliases": "deploy, deployment, ci, github actions, infra, nginx, vm",
        "summary": "deploy/ and .github/: CI, the deploy workflow, the VM's compose file, nginx and TLS.",
    },
    {
        "id": "cb-ops",
        "paths": ["deploy/backup", "deploy/systemd", "src/core/query_metrics.py", "src/api/ops.py", "scripts/check_live.py", "scripts/check_graph.py"],
        "name": "Backups and metrics",
        "team": "cb-infra",
        "aliases": "backups, falkordb backups, query metrics, slow query log, ops routes, healthz",
        "summary": "Nightly FalkorDB backups to GCS with a restore check, graph query metrics and GET /ops/metrics, healthz and the seed route.",
    },
    # --- outside systems --------------------------------------------------------------------
    {
        "id": "slack-platform",
        "name": "Slack platform",
        "team": "cb-answers",
        "aliases": "slack api, slack servers",
        "summary": "Slack itself. Sends events to us, receives our replies, and signs people in to the console.",
    },
    {
        "id": "github-platform",
        "name": "GitHub",
        "team": "cb-answers",
        "aliases": "github api, github.com",
        "summary": "GitHub itself. Sends webhooks to the GitHub App and answers its API reads.",
    },
    {
        "id": "claude-api",
        "name": "Claude API",
        "team": "cb-answers",
        "aliases": "anthropic, anthropic api",
        "summary": "Anthropic's Messages API, called by the LLM gateway.",
    },
]

# child depends on parent, from the imports between the areas (see the module docstring)
DEPENDS_ON = [
    ("cb-jira", "cb-connectors"),
    ("cb-hubspot", "cb-connectors"),
    ("cb-connectors", "falkordb"),
    ("cb-connectors", "cb-tenancy"),
    ("cb-connectors", "scheduler"),
    ("cb-console", "cb-connectors"),
    ("cb-console", "cb-jira"),
    ("cb-console", "cb-hubspot"),
    ("cb-console", "cb-tenancy"),
    ("cb-console", "cb-slack"),
    ("cb-console", "cb-onboarding"),
    ("cb-console", "access-control"),
    ("cb-console", "falkordb"),
    ("cb-console", "slack-platform"),
    ("cb-tenancy", "falkordb"),
    ("scheduler", "falkordb"),
    ("scheduler", "cb-tenancy"),
    ("github-ingest", "knowledge-tools"),
    ("github-ingest", "scheduler"),
    ("github-ingest", "falkordb"),
    ("github-ingest", "github-platform"),
    ("knowledge-tools", "falkordb"),
    ("knowledge-tools", "llm-gateway"),
    ("knowledge-tools", "cb-connectors"),
    ("cb-mcp", "knowledge-tools"),
    ("cb-site", "knowledge-tools"),
    ("cb-onboarding", "knowledge-tools"),
    ("cb-onboarding", "cb-connectors"),
    ("cb-onboarding", "cb-tenancy"),
    ("cb-seeds", "falkordb"),
    ("cb-seeds", "cb-memory"),
    ("cb-slack", "turn-engine"),
    ("cb-slack", "slack-client"),
    ("cb-slack", "cb-memory"),
    ("cb-slack", "access-control"),
    ("cb-slack", "knowledge-tools"),
    ("cb-slack", "llm-gateway"),
    ("cb-slack", "cb-tenancy"),
    ("cb-slack", "scheduler"),
    ("cb-slack", "github-ingest"),
    ("cb-slack", "falkordb"),
    ("turn-engine", "access-control"),
    ("turn-engine", "llm-gateway"),
    ("turn-engine", "slack-client"),
    ("turn-engine", "scheduler"),
    ("turn-engine", "falkordb"),
    ("cb-memory", "llm-gateway"),
    ("cb-memory", "slack-client"),
    ("cb-memory", "falkordb"),
    ("access-control", "falkordb"),
    ("access-control", "cb-tenancy"),
    ("slack-client", "slack-platform"),
    ("slack-client", "falkordb"),
    ("llm-gateway", "claude-api"),
    ("cb-ops", "falkordb"),
    ("cb-deploy", "falkordb"),
]

# (person, relation, service): OWNS goes to whoever wrote most of an area's commits,
# WORKED_ON to the others who committed there (git log, 18 Oct 2026).
WORK = [
    # Tushar Umbarkar (codesensei-tushar)
    ("tushar", "OWNS", "knowledge-tools"),
    ("tushar", "OWNS", "cb-memory"),
    ("tushar", "OWNS", "cb-site"),
    ("tushar", "OWNS", "cb-mcp"),
    ("tushar", "OWNS", "llm-gateway"),
    ("tushar", "OWNS", "access-control"),
    ("tushar", "OWNS", "slack-client"),
    ("tushar", "OWNS", "github-ingest"),
    ("tushar", "OWNS", "cb-seeds"),
    ("tushar", "WORKED_ON", "cb-slack"),
    ("tushar", "WORKED_ON", "turn-engine"),
    ("tushar", "WORKED_ON", "scheduler"),
    ("tushar", "WORKED_ON", "cb-deploy"),
    ("tushar", "WORKED_ON", "falkordb"),
    ("tushar", "WORKED_ON", "cb-onboarding"),
    ("tushar", "KNOWS", "claude-api"),
    ("tushar", "KNOWS", "github-platform"),
    ("tushar", "KNOWS", "slack-platform"),
    # Yash Patil (noobsurd11)
    ("yash", "OWNS", "cb-connectors"),
    ("yash", "OWNS", "cb-jira"),
    ("yash", "OWNS", "cb-hubspot"),
    ("yash", "OWNS", "cb-console"),
    ("yash", "OWNS", "cb-tenancy"),
    ("yash", "OWNS", "scheduler"),
    ("yash", "OWNS", "cb-slack"),
    ("yash", "OWNS", "turn-engine"),
    ("yash", "OWNS", "falkordb"),
    ("yash", "OWNS", "cb-deploy"),
    ("yash", "OWNS", "cb-ops"),
    ("yash", "OWNS", "cb-onboarding"),
    ("yash", "WORKED_ON", "knowledge-tools"),
    ("yash", "WORKED_ON", "cb-memory"),
    ("yash", "WORKED_ON", "cb-site"),
    ("yash", "WORKED_ON", "cb-mcp"),
    ("yash", "WORKED_ON", "access-control"),
    ("yash", "WORKED_ON", "slack-client"),
    ("yash", "WORKED_ON", "github-ingest"),
    ("yash", "KNOWS", "slack-platform"),
    # Om Sherikar (omsherikar)
    ("om", "WORKED_ON", "github-ingest"),
    ("om", "WORKED_ON", "knowledge-tools"),
    ("om", "WORKED_ON", "cb-site"),
    ("om", "KNOWS", "github-platform"),
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
        "service": "cb-deploy",
        "author": "tushar",
        "summary": "docs/SETUP.md: run FalkorDB in Docker, seed the graph, start the API, and every setting.",
    },
    {
        "id": "doc-slack",
        "name": "Slack guide",
        "service": "cb-slack",
        "author": "tushar",
        "summary": "docs/SLACK.md: creating the Slack app, tokens, the permissions graph, and what was ported from company-brain.",
    },
    {
        "id": "doc-manifest-dev",
        "name": "Slack dev manifest",
        "service": "cb-slack",
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
        "service": "cb-slack",
        "author": "yash",
        "summary": "slack_app/manifest.prod.yaml: the same app pointed at https://brain.noobsurd.space, with the Sign in with Slack redirect.",
    },
    {
        "id": "doc-connectors",
        "name": "Connectors guide",
        "service": "cb-connectors",
        "author": "yash",
        "summary": "docs/CONNECTORS.md: the connector contract, scopes (org_internal), installs per org, and the batched, content-hashed writes.",
    },
    {
        "id": "doc-console",
        "name": "Console guide",
        "service": "cb-console",
        "author": "yash",
        "summary": "docs/CONSOLE.md: Sign in with Slack, sessions and CSRF, roles, invites, and the console pages.",
    },
    {
        "id": "doc-deploy",
        "name": "Deploy guide",
        "service": "cb-deploy",
        "author": "yash",
        "summary": "docs/DEPLOY.md: the VM, secrets, the deploy workflow, backups and their access control.",
    },
    {
        "id": "doc-github",
        "name": "GitHub guide",
        "service": "github-ingest",
        "author": "om",
        "summary": "docs/GITHUB.md: the GitHub App, its webhooks, repo scopes and the new-pull-request heads-up.",
    },
    {
        "id": "doc-design",
        "name": "Design notes",
        "service": "cb-site",
        "author": "tushar",
        "summary": "docs/DESIGN.md: the public site's layout, answer card, tour and theme.",
    },
]

# Work is tracked as pull requests on GitHub, which GitHub ingest writes; the seed adds none.
TICKETS: list[dict] = []

DECISIONS = [
    {
        "id": "decision-falkordb-memory",
        "name": "Store memory in FalkorDB instead of a hosted memory API",
        "summary": "company-brain keeps memories in supermemory. We port its Slack layer but keep every memory, scope, and membership edge in the FalkorDB graph, so the graph stays the only store.",
        "aliases": "why falkordb, why not supermemory, memory store, hosted memory api",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "cb-memory",
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
        "about": "cb-slack",
        "based_on": ["doc-slack", "doc-manifest-prod"],
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
        "based_on": ["doc-strategy"],
        "decided_at": 1_792_144_800_000,  # 16 Oct 2026 10:00 UTC
    },
    {
        "id": "decision-vector-recall",
        "name": "Hybrid recall with a vector index on Memory.embedding",
        "summary": "Add a vector index next to the full-text index and union both result sets in search, with a local sentence-transformers encoder so no key is needed.",
        "aliases": "vector recall, hybrid recall, vector index, embeddings, semantic search",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "cb-memory",
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
        "about": "cb-memory",
        "based_on": ["doc-strategy"],
        "supersedes": "decision-vector-recall",
        "decided_at": 1_792_225_800_000,  # 17 Oct 2026 08:30 UTC (14:00 IST)
    },
    {
        "id": "decision-open-mcp",
        "name": "Serve MCP without a token, read-only and rate-limited",
        "summary": "/mcp answers callers without a token: the read-only tools only, org_shared only, on the ask page's per-address budget of 30 calls a minute. A token skips the budget. (#115)",
        "aliases": "mcp without a token, open mcp, public mcp, mcp token",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "cb-mcp",
        "based_on": ["doc-strategy"],
        "pull_requests": [115],
        "decided_at": 1_792_314_527_000,  # 18 Oct 2026, when #115 merged
    },
    {
        "id": "decision-serve-quillfern",
        "name": "Production serves Quillfern as its seed company",
        "summary": "SEED_COMPANY=quillfern on production, so the seed refresh and team imports join the company the public page serves. This project's own seed is merged next to it by name. (#116)",
        "aliases": "seed company, why quillfern, production seed",
        "proposer": "tushar",
        "approver": "tushar",
        "about": "cb-seeds",
        "based_on": ["doc-strategy"],
        "pull_requests": [116],
        "decided_at": 1_792_314_552_000,  # 18 Oct 2026, when #116 merged
    },
    {
        "id": "decision-connector-framework",
        "name": "One connector framework for every external tool",
        "summary": "Jira, HubSpot and whatever comes next share one contract and sync loop in src/connectors, and connector data lands in org_internal, which no public surface (ask page, team pages, MCP, graph stats) can read. (#146, Jira #147, HubSpot #148)",
        "aliases": "connector framework decision, why a connector framework, org_internal, shared sync",
        "proposer": "yash",
        "approver": "yash",
        "about": "cb-connectors",
        "based_on": ["doc-connectors"],
        "pull_requests": [146, 147, 148],
        "decided_at": 1_792_332_134_000,  # 18 Oct 2026, when #146 merged
    },
    {
        "id": "decision-sign-in-with-slack",
        "name": "Sign in to the console with Slack",
        "summary": "The console has no passwords: Sign in with Slack (OpenID Connect, PKCE, RS256 id_token checked against Slack's keys) maps the Slack team to its org, with server-side sessions, CSRF tokens, invites and an audit log. (#151, manifests #152)",
        "aliases": "sign in with slack, console login, oidc, why slack sign in",
        "proposer": "yash",
        "approver": "yash",
        "about": "cb-console",
        "based_on": ["doc-console", "doc-manifest-prod"],
        "pull_requests": [151, 152],
        "decided_at": 1_792_336_468_000,  # 18 Oct 2026, when #151 merged
    },
    {
        "id": "decision-graph-per-org",
        "name": "A FalkorDB graph per org",
        "summary": "Every org gets its own graph, named in the control graph, and every graph call runs in its org: Slack per team, the console per session, the scheduler per job. The legacy org keeps the company graph for the public site and MCP. (#149, #156, #180)",
        "aliases": "graph per org, one graph per org, org graphs, tenancy decision, multi tenant graph",
        "proposer": "yash",
        "approver": "yash",
        "about": ["cb-tenancy", "falkordb"],
        "based_on": ["doc-console"],
        "pull_requests": [149, 156, 180],
        "decided_at": 1_792_337_366_000,  # 18 Oct 2026, when #156 merged
    },
    {
        "id": "decision-nightly-backups",
        "name": "Back up FalkorDB nightly to GCS, with a restore check",
        "summary": "A systemd timer takes a BGSAVE, checks node and edge counts did not move, and uploads the RDB with a manifest to a GCS bucket; a restore check loads it back. (#177, #187)",
        "aliases": "backups decision, nightly backups, gcs backups, restore check",
        "proposer": "yash",
        "approver": "yash",
        "about": "cb-ops",
        "based_on": ["doc-deploy"],
        "pull_requests": [177, 187],
        "decided_at": 1_792_338_285_000,  # 18 Oct 2026, when #177 merged
    },
    {
        "id": "decision-tenancy-write-strict",
        "name": "A graph write without an org raises in production",
        "summary": "TENANCY_WRITE_STRICT is on in production: a write that reaches the legacy fallback with no org in context raises NoOrgContext instead of landing in the legacy graph. Reads keep the fallback. (#194)",
        "aliases": "tenancy write strict, no org context, strict writes",
        "proposer": "yash",
        "approver": "yash",
        "about": "cb-tenancy",
        "based_on": ["doc-console"],
        "pull_requests": [194],
        "decided_at": 1_792_340_794_000,  # 18 Oct 2026, when #194 merged
    },
    {
        "id": "decision-content-hash-sync",
        "name": "Connector writes are batched and skip unchanged items by content hash",
        "summary": "A sync page is written in a handful of batched queries, an item whose content hash has not changed is skipped, and the graph's generation is bumped once per page: 50 new tickets went from 1,032 queries to 16, and a no-op poll writes nothing. (#207)",
        "aliases": "content hash, content-hash sync, batched connector writes, generation bump",
        "proposer": "yash",
        "approver": "yash",
        "about": "cb-connectors",
        "based_on": ["doc-connectors"],
        "pull_requests": [207],
        "decided_at": 1_792_343_737_000,  # 18 Oct 2026, when #207 merged
    },
    {
        "id": "decision-scheduler-pool",
        "name": "Scheduler runs a worker pool with leased claims and a fast lane",
        "summary": "Due jobs are claimed atomically with a lease (Lua), run by a pool of workers, and a fast-lane worker takes only approval expiry, so a slow sync can no longer delay an expiry by seconds. Cancels go through an index. (#216)",
        "aliases": "scheduler pool, worker pool, leased claims, fast lane, scheduler decision",
        "proposer": "yash",
        "approver": "yash",
        "about": "scheduler",
        "based_on": ["doc-slack"],
        "pull_requests": [216],
        "decided_at": 1_792_344_553_000,  # 18 Oct 2026, when #216 merged
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
