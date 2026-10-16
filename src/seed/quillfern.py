"""A fictional company used as test data by the integration tests.

Quillfern is made up: a small SaaS that sells a team notebook. Every person,
service, document, ticket, and decision here is invented.
"""

COMPANY = {"id": "quillfern", "name": "Quillfern"}

PEOPLE = [
    {"id": "maya", "name": "Maya Ortiz", "role": "Platform lead", "email": "maya@quillfern.example.com"},
    {"id": "arjun", "name": "Arjun Mehta", "role": "Billing engineer", "email": "arjun@quillfern.example.com"},
    {"id": "lena", "name": "Lena Fischer", "role": "Backend engineer", "email": "lena@quillfern.example.com"},
    {"id": "kofi", "name": "Kofi Mensah", "role": "Data engineer", "email": "kofi@quillfern.example.com"},
    {"id": "sara", "name": "Sara Lindqvist", "role": "Site reliability engineer", "email": "sara@quillfern.example.com"},
    {"id": "tomas", "name": "Tomas Reyes", "role": "Engineering manager", "email": "tomas@quillfern.example.com"},
]

TEAMS = [
    {"id": "platform", "name": "Platform", "members": ["maya", "sara", "tomas"]},
    {"id": "payments", "name": "Payments", "members": ["arjun", "lena"]},
    {"id": "data", "name": "Data", "members": ["kofi"]},
]

SERVICES = [
    {"id": "auth-service", "name": "Auth Service", "team": "platform", "aliases": "auth, authentication, login, sessions"},
    {"id": "api-gateway", "name": "API Gateway", "team": "platform", "aliases": "gateway, api"},
    {"id": "billing-service", "name": "Billing Service", "team": "payments", "aliases": "billing, payments, subscriptions"},
    {"id": "invoice-worker", "name": "Invoice Worker", "team": "payments", "aliases": "invoices, invoicing"},
    {"id": "notification-service", "name": "Notification Service", "team": "platform", "aliases": "notifications, email, alerts"},
    {"id": "search-service", "name": "Search Service", "team": "data", "aliases": "search"},
    {"id": "memory-layer", "name": "Memory Layer", "team": "data", "aliases": "memory, memory layer, notebook memory"},
    {"id": "postgres", "name": "Postgres", "team": "platform", "aliases": "postgresql, primary database, db"},
    {"id": "graph-store", "name": "Graph Store", "team": "data", "aliases": "graph, graph database"},
]

# child depends on parent
DEPENDS_ON = [
    ("api-gateway", "auth-service"),
    ("billing-service", "auth-service"),
    ("notification-service", "auth-service"),
    ("search-service", "auth-service"),
    ("api-gateway", "billing-service"),
    ("invoice-worker", "billing-service"),
    ("billing-service", "postgres"),
    ("auth-service", "postgres"),
    ("search-service", "memory-layer"),
    ("memory-layer", "graph-store"),
    ("invoice-worker", "notification-service"),
]

# (person, relation, service)
WORK = [
    ("maya", "OWNS", "auth-service"),
    ("maya", "OWNS", "api-gateway"),
    ("arjun", "OWNS", "billing-service"),
    ("lena", "OWNS", "invoice-worker"),
    ("lena", "OWNS", "notification-service"),
    ("kofi", "OWNS", "memory-layer"),
    ("kofi", "OWNS", "search-service"),
    ("sara", "OWNS", "postgres"),
    ("sara", "OWNS", "graph-store"),
    ("lena", "WORKED_ON", "billing-service"),
    ("tomas", "WORKED_ON", "billing-service"),
    ("sara", "KNOWS", "billing-service"),
    ("sara", "KNOWS", "auth-service"),
    ("maya", "WORKED_ON", "memory-layer"),
    ("arjun", "KNOWS", "auth-service"),
]

DOCUMENTS = [
    {
        "id": "doc-billing-runbook",
        "name": "Billing runbook",
        "service": "billing-service",
        "author": "arjun",
        "summary": "How renewals, retries, and refunds flow through the billing service.",
    },
    {
        "id": "doc-auth-design",
        "name": "Auth session design",
        "service": "auth-service",
        "author": "maya",
        "summary": "Session tokens, refresh rules, and which services call auth on every request.",
    },
    {
        "id": "doc-memory-graph-rfc",
        "name": "RFC: memory layer on a graph store",
        "service": "memory-layer",
        "author": "kofi",
        "summary": "Notebook links are many-hop joins in Postgres. A graph store answers them in one traversal.",
    },
    {
        "id": "doc-invoice-format",
        "name": "Invoice format spec",
        "service": "invoice-worker",
        "author": "lena",
        "summary": "Fields, currencies, and the PDF layout the invoice worker renders.",
    },
]

TICKETS = [
    {
        "id": "TKT-101",
        "name": "TKT-101",
        "title": "Memory queries time out on large workspaces",
        "service": "memory-layer",
        "owner": "kofi",
        "summary": "Linked-note lookups over five hops take more than 8 seconds on Postgres.",
    },
    {
        "id": "TKT-102",
        "name": "TKT-102",
        "title": "Annual plan renewals charge twice on retry",
        "service": "billing-service",
        "owner": "lena",
        "summary": "A retried renewal webhook creates a second charge.",
    },
    {
        "id": "TKT-103",
        "name": "TKT-103",
        "title": "Session refresh fails after token rotation",
        "service": "auth-service",
        "owner": "maya",
        "summary": "Clients holding an old refresh token get logged out.",
    },
]

DECISIONS = [{
    "id": "decision-memory-off-postgres",
    "name": "Move the memory layer off Postgres",
    "summary": "Store notebook links in the graph store instead of Postgres join tables, because multi-hop lookups timed out.",
    "aliases": "memory layer off postgres, graph store migration, why move memory",
    "proposer": "kofi",
    "approver": "tomas",
    "about": "memory-layer",
    "cites": ["doc-memory-graph-rfc", "TKT-101"],
}]

CHATS = [{
    "id": "chat-memory-migration",
    "name": "#data-eng thread on the memory migration",
    "summary": "Kofi shared timings from TKT-101; Sara agreed to run the graph store; Tomas signed off.",
    "author": "kofi",
    "about": "decision-memory-off-postgres",
}]
