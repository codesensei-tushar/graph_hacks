# HubSpot

The HubSpot connector brings a CRM's customer accounts into the company graph, with who owns
each one and who has worked on it, so Slack can answer "who knows Larkspur Freightways", "who
owns it", and "what if Dev leaves" for accounts the way it does for services. It is a connector
on the framework in [CONNECTORS.md](CONNECTORS.md); the code is `src/connectors/hubspot/`.

## What comes in, and what never does

| HubSpot | Graph |
| --- | --- |
| The portal | one `(:Container {kind: 'portal'})`, `HubSpot portal <id>` |
| An active user who can own records (an owner) | `(:ExternalUser {name, email})`, `-[:IS]->(:Person)` when exactly one Person has that email |
| A company that is a customer or has any deal | `(:Service {kind: 'account'})-[:IN_CONTAINER]->(portal)`, named as HubSpot names it |
| The company's owner | `(owner)-[:OWNS]->(account)` |
| A deal that is open, or was created or closed in the last 12 months | `(deal owner)-[:WORKED_ON {evidence: 'deal', stage}]->(account)`, one edge per owner, with the stage of their latest deal. A deal is never a node of its own. |

`OWNS` and `WORKED_ON` start at the owner's Person when their account is linked, so
`who_owns`, `who_knows`, `if_person_leaves` and the team pages count them like any other
ownership. An unlinked owner still counts in `who_knows`, under their HubSpot name.

An account node carries `name`, `url` (the record in HubSpot), `state`, `status` (the raw
lifecycle stage), `source_updated_at`, `summary` ("HubSpot company, lifecycle stage customer;
1 open deal; ..."), and `domain`, `industry` and `open_deals`.

**Never read:** contacts, emails, calls, notes, meetings, tasks, deal amounts and every other
money field, quotes, forecasts, deal names and descriptions, and custom properties. The
connector asks HubSpot for a fixed list of properties (`mapping.COMPANY_PROPERTIES` and
`mapping.DEAL_PROPERTIES`) and nothing else, and a unit test fails if an amount, a free-text
field or a contact field joins them.

**Which companies.** A company is an account while its lifecycle stage is `customer` or
`evangelist`, or it has at least one deal. Leads and every other company stay out. The backfill
takes the 500 most recently changed (`MAX_COMPANIES`); the framework caps a container at 1000.

**Open or closed.** An account is `open` while it is a customer or has an open deal, and
`closed` otherwise: a customer moved to another stage, such as `other` or a custom "churned"
stage, or a company whose deals were all lost. `status` keeps the stage HubSpot has.

**Who left.** Only active users hold edges. A deal or company owned by a deactivated user
adds no edge, so an account whose owner has left reads as having no owner, and a person who
left is never named "next in line".

## Who can read it

Everything the connector writes is `org_internal`: every Slack asker in the workspace reads
it, in channels and DMs. The public ask page, the team pages and MCP read `org_shared` only,
so they never see an account, an owner or a stage from HubSpot.

## Setup

1. In HubSpot, create a private app (Settings, Integrations, Private apps; on accounts that
   have moved to the new developer platform, an app with a static access token) with these
   scopes, all read-only:

   | Scope | For |
   | --- | --- |
   | `crm.objects.companies.read` | the accounts, and the company-deal associations |
   | `crm.objects.deals.read` | deal owners, stages and dates, and the pipeline's stage labels |
   | `crm.objects.owners.read` | owners' names and emails |

   The seed script below also needs `crm.objects.companies.write` and `crm.objects.deals.write`.
   Drop them again once the demo data is in.

2. Put the app's access token where the server reads its environment:

   | Variable | |
   | --- | --- |
   | `CONNECTOR_HUBSPOT_TOKEN` | required; the install exists once it is set |
   | `CONNECTOR_HUBSPOT_API_VERSION` | optional: `2026-09` (the default, HubSpot's dated paths such as `/crm/objects/2026-09/companies`), or `v3` for the numbered paths |
   | `CONNECTOR_HUBSPOT_INSTALL_ID` | optional; `default` |

   The token stays in memory: it is sent as a bearer header to `api.hubapi.com` only, and is
   never logged, written to the graph, or returned by a route.

3. The connector checks the token with HubSpot's account details call, then syncs on its own.
   `GET /ops/connectors` (admin) shows the portal, the item count and the last error;
   `POST /ops/connectors/hubspot/sync` syncs now.

## How it stays in step

One code path, HubSpot's CRM search sorted on `hs_lastmodifieddate`, 100 companies a page:

- **Backfill:** the accounts, newest first, until 500.
- **Every 5 minutes:** the accounts changed since the last poll, then the deals changed since
  then (a new deal, or a stage or owner change), each sent as its company with every edge it
  has, since writing an account replaces its edges. A poll starts two minutes before the last
  one began, by HubSpot's clock, because a change takes a moment to reach search.

The owners list and the deal stage labels are read once every ten minutes.

**Not yet:** webhooks (polling only), and a company deleted or merged in HubSpot stays in the
graph until a later version notices deletions.

## Identity

An owner becomes a Person's account only when their HubSpot login email is the email of exactly
one Person in the graph. Quillfern's Sales team file gives Priya, Dev and Noor
`@quillfern.example.com` emails, so for the demo the HubSpot users who own the accounts must
have those emails, or an owner stays an account of their own: `who_knows` names them under
their HubSpot name, while `who_owns` and the team pages only count Persons. In a real team, the
team file (or Slack) and HubSpot use the same work emails and this takes care of itself.

## Demo data

`scripts/seed_hubspot.py` creates 20 invented companies and 26 deals in the HubSpot account the
token belongs to: Dev's renewals, Priya's new business, Noor's paperwork, two customers who left
and two companies whose only deals were lost. No contacts and no amounts.

```sh
set -a; . ~/.config/company-brain/connectors.env; set +a
.venv/bin/python scripts/seed_hubspot.py            # dry run: what it would create
.venv/bin/python scripts/seed_hubspot.py --apply    # create it
```

It only ever creates, never updates or deletes, and touches only what it made: each company's
domain ends in `.example` (reserved, so no real company has one), and every company and deal
has a description that starts with `[company-brain-seed]`. A domain already in the account
without that marker is skipped with its deals, and a second run creates nothing. Each account
goes to the HubSpot user whose email is its account manager's in the team file; the dry run
says which, and which accounts will not link because no user has that email.

Then, in Slack:

- `who knows Larkspur Freightways`: Dev owns it and won the renewal; Priya is on the upgrade.
- `who owns Saltwick Outfitters`: Dev, for a customer who left.
- `what if Dev Patel leaves`: the accounts only he owns, with Priya next in line where she has a deal.
