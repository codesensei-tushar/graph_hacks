# Connectors

A connector brings one external source (Jira first, then HubSpot) into the company graph. It
only reads the source and returns normalised records; the framework in `src/connectors/`
writes them, schedules the sync, takes signed webhooks, and keeps who may read what.

## Contract

The contract is `src/connectors/base.py`. It takes additive changes only.

**A connector** subclasses `Connector`, sets `source` (`"jira"`), `required` (the env fields
that must be set), `poll_seconds` and `accepts_webhooks`, and calls
`registry.register(TheConnector())` when its module is imported (`src/connectors/<source>/__init__.py`;
`registry.KNOWN_MODULES` lists the modules). Its methods:

| Method | Returns | Notes |
| --- | --- | --- |
| `check_credentials(install)` | `InstallInfo(account, site)` | One cheap call. Stored on the state node, so nothing secret. |
| `containers(install)` | `list[Container]` | The allow-list. A container that drops off it is removed with its items. |
| `fetch(install, container, cursor)` | `Page` | One page. `cursor` is None on the backfill; fetch newest first then. |
| `readers(install, container)` | `list[ExternalUser] \| None` | Restricted containers only. None means nobody (fails closed). |
| `verify_webhook(install, request)` | `bool` | False answers 401 and nothing runs. |
| `parse_webhook(install, request)` | `Page \| None` | May call the source for the full record. Cursors are not touched. |

Raise `RetryLater(seconds)` to wait, `CredentialsRejected` on a 401, any `ConnectorError`
otherwise. `src/connectors/http.py`'s `JsonClient(base_url, headers)` does this already: it
retries short 429/503 waits and 5xx in place, raises `RetryLater` for long waits, never sends
the headers to another host, and never logs them. `basic_auth(email, token)` and `bearer(token)`
build the headers.

**The install.** `Install(source, id, config)`. In v1 it comes from environment variables
`CONNECTOR_<SOURCE>_<FIELD>`; `config` maps each field, lowercased, to its value
(`CONNECTOR_JIRA_API_TOKEN` is `config["api_token"]`). Secrets stay in memory: they are never
written to the graph, logged, or returned, and `repr(install)` leaves `config` out. Build every
id with the install: `install.item_id(ext)` is `<source>:<install>:<ext>`, `install.user_id(ext)`
and `install.container_id(ext)` add `user:` and `container:`. The graph writer refuses ids from
outside the install, so a connector cannot touch a seeded or team-imported node.

**Records** (`Page` holds `items`, `users`, `edges`, `deleted_ids`, `next_cursor`, `has_more`, `retry_after`):

| Record | Written as |
| --- | --- |
| `Container(id, name, external_id, url, kind, restricted)` | `(:Container)`. Scope `org_internal`, or its own scope when `restricted`. |
| `Item(label, id, external_id, container, name, title, url, state, status, updated_at, summary, kind, about, props)` | `(:Ticket\|Service\|Document)-[:IN_CONTAINER]->(:Container)`, scope from the container. `name` is the short handle (`PAY-12`), `title` the full text. `state` is `open` or `closed`, the raw value goes in `status`. A CRM company is `Service` with `kind='account'`. |
| `ExternalUser(id, source, external_id, name, email)` | `(:ExternalUser)-[:IS]->(:Person)` when exactly one Person has the email. Connectors never create a Person. |
| `Edge(rel, from_id, to_id, props)` | `OPENED`, `ASSIGNED`, `OWNS`, `WORKED_ON`, `ABOUT` (within the install) or `BLOCKS`. `OWNS` and `WORKED_ON` from a linked user are written from its Person. |
| `Item.about` hints | `(item)-[:ABOUT]->(:Service)`: each hint (a component, a label) resolved strictly against the graph's services, else the best guess over all hints. |

Every item node gets `id`, `name`, `title`, `summary` (clipped to 2000 characters), `url`,
`state`, `status`, `source_updated_at`, `external_id`, `source`, `install`, `scope` and
`synced_at` (ms), then `props` under any name not already taken.

**Edges belong to an item**: the `from_id` item, else the `to_id` item. Writing an item
replaces all of its edges, so a page carries every edge of each item in it, and an edge between
two items comes once, with the item it points out of (`PAY-13 BLOCKS PAY-12` comes with
PAY-13). An edge whose item is not in the page is only added.

**Cursors.** `next_cursor` is stored per container exactly as returned and handed to the next
`fetch`; None starts the next fetch over. `has_more` fetches the next page on the next
scheduler step; otherwise the next fetch is `poll_seconds` away. `retry_after` is the least wait
before the next fetch either way. A container keeps at most `sync.MAX_ITEMS_PER_CONTAINER`
items; past that, new items are dropped and existing ones still update.

**Pull requests.** `graph.link_references(pull_request_id, handles, source, rel="REFERENCES")`
links a pull request `-[:REFERENCES]->` (or, with `rel="CLOSES"`, `-[:CLOSES]->`) the tickets
of `source` named by `handles` (`["PAY-12"]`, any case), replacing that pull request's earlier
links of that source and relationship. The handles are kept on the pull request
(`ticket_references`, `ticket_closes`), so a ticket synced later links back when it is written.

**Reserved properties.** `props` may not use a name graph.py writes itself, nor `number`:
`who_should_know` looks tickets and pull requests up by `#12`, and a Jira ticket must not answer
for a GitHub issue.

**Scopes.** Connector data is `org_internal`: every Slack asker reads it, in channels and DMs.
The public ask page, the team pages and MCP read `org_shared` only, so they never see it.
