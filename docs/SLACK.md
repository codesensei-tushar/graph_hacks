# Slack

Company Brain lives in Slack. You DM it, @mention it, or say its name, and it answers from the FalkorDB graph and cites the path it walked. Messages in channels it's in become scoped memories in the same graph.

The Slack layer is a port of [company-brain](https://github.com/supermemoryai/company-brain)'s Slack agent, file by file. Its hosted memory API is replaced by FalkorDB, and its Cloudflare Durable Object state by Redis keys in the same FalkorDB instance. Billing, MCP connectors, leasing, skills, research and telemetry were left out on purpose.

## Setup

The app uses the Events API over HTTPS: Slack POSTs each event to `/slack/events`, and every request is checked against the signing secret. This is the mode Slack recommends for production. Socket Mode is not used; it's meant for development and is not allowed in the Slack Marketplace. Locally, a tunnel stands in for the public URL. In production you swap the tunnel for your server's domain and nothing else changes.

### 1. Start the graph and the app

```bash
docker compose up -d
source .venv/bin/activate
python scripts/seed_graph.py
uvicorn src.main:app --reload
```

Check that the running server is this app:

```bash
curl -s localhost:8000/api/health
# {"ok":true,"nodes":28,"slack":false}
```

`"slack":false` is expected until step 4. If the response has no `slack` field, an older server is still holding port 8000. Stop it and start `src.main:app` again.

### 2. Open a public URL

```bash
ngrok http 8000
```

Use the `https://` forwarding URL that ngrok prints. A free ngrok account comes with one static `*.ngrok-free.dev` domain, so the URL stays the same across restarts. Check that Slack's URL test gets through:

```bash
curl -s -X POST https://YOUR-TUNNEL/slack/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"ok"}'
# {"challenge":"ok"}
```

If you get `{"detail":"Not Found"}`, see step 1: an older server is on port 8000.

### 3. Create the Slack app

1. Open [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**, and pick your workspace.
2. Paste [`slack_app/manifest.yaml`](../slack_app/manifest.yaml). Replace `YOUR-TUNNEL.ngrok-free.app` in both `request_url` lines with your tunnel host. Don't commit that change; the URL belongs to one machine.
3. **Create**, then **Install to Workspace** and **Allow**.

Slack checks `request_url` when it saves the manifest. The app answers that check before any secrets are in `.env`, so creating the app first is fine.

### 4. Put the tokens in `.env`

Edit `.env` directly. Don't paste tokens into chats, tickets, or commits. `.env` is gitignored.

| Variable | Where to find it |
| --- | --- |
| `SLACK_BOT_TOKEN` | **OAuth & Permissions** → Bot User OAuth Token (`xoxb-...`). Appears after install. |
| `SLACK_SIGNING_SECRET` | **Basic Information** → App Credentials → Signing Secret (**Show**) |
| `SLACK_OWNER_ID` | Your member id: in Slack, your profile → ⋯ → **Copy member ID** (`U...`). Becomes the owner. |
| `SLACK_ADMIN_IDS` | Optional. Comma-separated member ids that start as admins. |
| `LLM_API_KEY`, `LLM_MODEL` | Optional. An Anthropic API key turns on triage, chime-in, steering and memory extraction. See [The model seam](#the-model-seam). |
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `PUBLIC_URL`, `BRAIN_ADMIN_TOKEN` | Only for the multi-workspace install. See [Multi-workspace install](#multi-workspace-install). |

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_OWNER_ID=U...
SLACK_ADMIN_IDS=
```

Restart uvicorn, because `--reload` doesn't watch `.env`. Then check:

```bash
curl -s localhost:8000/api/health
# {"ok":true,"nodes":28,"slack":true}
```

### 5. Try it

1. In the app settings, open **Event Subscriptions** and confirm the request URL shows **Verified**. If it doesn't, click **Retry**.
2. DM the bot, or invite it to a channel with `/invite @company-brain` and @mention it.
3. Send `help`.

### Production

- Run the app behind HTTPS on a stable domain and put that domain in both `request_url` values, in **Event Subscriptions** and **Interactivity & Shortcuts**.
- Set the same four variables in the host's secret store instead of a `.env` file.
- Keep FalkorDB off the public internet: don't publish port 6379. The local `docker-compose.yml` publishes it with no password. The app reads `FALKORDB_PASSWORD`, and production sets it; see [DEPLOY.md](DEPLOY.md).
- If you add scopes or events to the manifest, Slack asks you to **Reinstall to Workspace**. The bot token may change; update `SLACK_BOT_TOKEN` if it does.
- If the signing secret leaks, regenerate it under **Basic Information** and redeploy.

## Routes

| Method | Path | Auth | Does |
| --- | --- | --- | --- |
| POST | `/slack/events` | Slack signature | Events API |
| POST | `/slack/interactions` | Slack signature | Approve/Deny, the public-channel rollout button, team invite cards |
| GET | `/slack/oauth/install` | `BRAIN_ADMIN_TOKEN` (header or `?token=`) | Starts a workspace install |
| GET | `/slack/oauth/callback` | Slack redirect | Exchanges the code, stores the workspace, bootstraps `#company-brain` |
| GET | `/slack/status` | `BRAIN_ADMIN_TOKEN` | Connected workspaces |
| DELETE | `/slack/workspace` | `BRAIN_ADMIN_TOKEN` | Farewell post, then uninstall and teardown |
| GET/POST | `/slack/account-link/{token}` | One-time token from a DM | Links a Slack user to a person in the graph |
| GET | `/slack/manifest.json` | none | The app manifest for this server's `PUBLIC_URL` |
| GET | `/api/health` | none | Graph node count and whether Slack is configured |

Admin routes answer 503 until `BRAIN_ADMIN_TOKEN` is set. There is no web login, so that token stands in for company-brain's admin session.

### Multi-workspace install

A single workspace only needs `SLACK_BOT_TOKEN`. To install into several workspaces, set `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` (Basic Information → App Credentials), `PUBLIC_URL` and `BRAIN_ADMIN_TOKEN`, then open `PUBLIC_URL/slack/oauth/install?token=...`. Each install is stored as a `SlackWorkspace` node. The installer of record becomes that workspace's owner.

Bot tokens are encrypted with `TOKEN_ENCRYPTION_KEY` (Fernet) when the `cryptography` package is installed. Without it they're stored as plain text and the app logs a warning once.

### Backfill when you onboard

What happens after an install, ported from company-brain's `bootstrap.ts` and `public-channel-rollout.ts`:

1. **Bootstrap.** Create or adopt `#company-brain`, post the welcome and a read-only status card, and invite the installer. The installer gets a DM with the "Add me to my public channels" button.
2. **Beachhead, 5 minutes later.** The bot joins the two busiest eligible public channels on its own, imports their last 7 days, and posts a read-out in `#company-brain`. It skips this if a run already exists.
3. **Full rollout, opt-in.** The button, or the `backfill public channels` command, runs discover → join → collect → introduce, one scheduled step at a time. It imports the last 7 days of history and thread replies, written as daily documents into `org_shared`. It skips #general, #company-brain, private, archived and Slack Connect channels. Slack rate limits reschedule the step instead of failing it. Progress shows on the card.
4. **After that,** new messages in joined channels are kept as they arrive. With a model configured, channel-observe distills them instead.

When this runs:

- **OAuth install:** the callback runs steps 1–2 for the installer.
- **Token install (`SLACK_BOT_TOKEN`):** there is no callback, so the app runs the same steps once per workspace at startup, with `SLACK_OWNER_ID` as the installer (`src/domain/slack/token_install.py`). A Redis marker stops it running again; a disconnect clears it. Without `SLACK_OWNER_ID` nobody gets the DM button, so an admin uses `backfill public channels` instead.

Not backfilled, as in company-brain: private channel history (the bot records who is in a private channel when invited, then keeps only new messages), DMs, and per-person history when someone joins the workspace. A new member reads public memory straight away, and a private channel's memory once Slack shows them in it.

## Using it

The bot answers when you DM it, @mention it, address it by name ("hey company brain, ..."), or reply in a thread it's already in.

| Message | What happens |
| --- | --- |
| `help` | Lists the commands. |
| `remember: <text>` | Saves the text to the current room's scope. |
| `ticket: <title>` | Posts an Approve/Deny card. Only you can click it, and it expires in 15 minutes. Approving writes a `Ticket` into the graph. |
| `what can you see` | Lists the scopes this conversation can read. |
| `link my account` | Sends you a private one-time link to connect your Slack user to a person in the graph. |
| `promote @user` / `demote @user` | Admins and owner only. Grants or removes admin. |
| `forget this channel` | In a DM, clears your own DM memory. In a channel, admins and owner only: deletes that room's memories (for a public channel, only messages from that channel). |
| `backfill public channels` | Admins and owner only. Joins eligible public channels and imports their last 7 days (see [Backfill when you onboard](#backfill-when-you-onboard)). |
| `disconnect` | Admins and owner only. Posts a farewell, uninstalls the app and clears this workspace's state. |
| `stop` (in a thread while it's working) | Cancels the running answer. Only the person who asked can stop it. |
| anything else | A question, answered from the graph plus the memories you can read. The reply cites the nodes and edges it used. |

Reactions on the bot's replies:

- :black_square_for_stop: mutes the bot in that thread for proactive replies. Only the person who muted can unmute, by removing the reaction. Mentioning the bot still works.
- :mag: or :bug: shows you the reply's debug id privately.

Guests and external users get a denial. Members the graph doesn't know yet are added as people on first contact when their email is visible; otherwise they're sent the account-link prompt.

## What the turn does

Each turn follows company-brain's `runSlackTurnInner`:

1. The event is recorded in a local event store (a 50-message ring per channel, kept 7 days). Bot messages are context only.
2. A thread reply that doesn't address the bot goes to passive triage. Proactivity settings and thread mutes are checked first, then triage decides ANSWER, ACK (a reaction), INVESTIGATE or PASS. Unsolicited answers spend a per-channel budget: at most 12 replies an hour, with 3 minutes between normal ones.
3. A top-level channel message goes to chime-in, which runs the same triage and budget.
4. If a turn is already running in the thread, a new message from the asker can stop it. Otherwise the active-turn gate classifies it as IGNORE, APPEND (queued into the running turn) or REPLACE (restarts the turn).
5. The bot reacts with `:ack:`, falling back to :white_check_mark: when the workspace has no custom emoji, and posts a progress card while it works.
6. `compute_turn` produces the answer. A graph write suspends for an Approve/Deny card, and an expiry job collapses the card after 15 minutes.
7. The reply goes out, the thread is remembered as a bot thread, the reaction becomes `:brain:` (or :white_check_mark:), and any memories the turn proposed are saved to its scope.

Startup recovery re-arms the reaction queue and channel-observe schedules, and re-syncs private channel membership for every connected workspace.

## The model seam

Every model call goes through `src/integrations/llm/model.py`, which uses the Anthropic SDK with `LLM_API_KEY` and `LLM_MODEL`. When no key is set, or a call fails, each caller takes the fallback company-brain itself uses when its model fails:

| Feature | With a model | Without one |
| --- | --- | --- |
| Thread and channel triage | ANSWER / ACK / INVESTIGATE / PASS | PASS. A short "yes" right after the bot's offer still gets an answer. |
| Active-turn gate | IGNORE / APPEND / REPLACE | APPEND |
| Channel observe (memory from channel chatter) | Distills durable memories | Backs off. The raw message is kept in its scope instead (see below). |
| Channel introductions, install greeting | Written for the workspace | Static text |
| Memory split, entity lookup | Splits and enriches | Keeps the memory whole |

The answer itself comes from `src/domain/slack/compute.py`. `compute_turn` handles the commands above, then calls the registered agent, or falls back to the graph tools (`domain/knowledge`) plus scoped memory search. To plug in an agent, call `register_turn_agent(fn)` with a function that takes a `TurnRequest` (question, asker, memory scope, read set, thread, progress card) and returns a `TurnOutcome` (reply, memory writeback, or an approval to suspend on). Turn control, steering, approvals, delivery and memory writeback stay in `turn_service.py`.

## Permissions graph

A memory is written to exactly one scope: the narrowest room it was said in.

| Scope | Holds | Visible to |
| --- | --- | --- |
| `org_shared` | Public channels | Everyone in the workspace |
| `slack_channel_<id>` | One private channel or group DM | Members of that channel |
| `user_<slack id>` | One person's DMs with the bot | That person |

What a conversation can read depends on where you ask:

| Asking from | Can read |
| --- | --- |
| A public channel | `org_shared` |
| A private channel | That channel + `org_shared` |
| A DM with the bot | Your own scope + `org_shared` + every private channel you belong to |

- If a channel's privacy can't be confirmed, it's treated as private (fails closed).
- Private channel membership is stored in FalkorDB. `member_joined_channel` and `member_left_channel` events keep it current, and a `conversations.members` backfill runs when the bot joins a channel and at startup. If you leave a channel, its memory stops showing up in your DMs.
- Roles rank `owner > admin > member`. Owners are `SLACK_OWNER_ID` and each workspace's installer of record. Admins come from `SLACK_ADMIN_IDS` or `promote`.
- Graph writes (tickets) wait for an explicit Approve from the person who asked. A card for a turn that was stopped or replaced is cancelled instead.
- Memories carry tags that double as a topic tree (`product/billing/invoices`). Search fans out over every container you can read and keeps results scoring at least 0.3.

## Ported from company-brain

| Area | Here | Source |
| --- | --- | --- |
| HTTP | `api/v1/slack.py`, `api/v1/slack_install.py` | `routes/slack/index.ts` |
| Signing, envelopes, classifiers, name wake, reply target | `integrations/slack/verify.py`, `events.py`, `bot_address.py`, `routing.py` | `brain/slack/verify.ts`, `events.ts`, `bot-address.ts`, `routing.ts` |
| Web API client, mrkdwn, channel references, streaming and progress cards, attachments, debug traces | `integrations/slack/client.py`, `format.py`, `references.py`, `stream.py`, `attachments.py`, `message_trace.py`, `debug_trace_delivery.py` | `brain/slack/client.ts`, `format.ts`, `references.ts`, `stream.ts`, `attachments.ts`, `message-trace.ts`, `debug-trace-delivery.ts` |
| Turn loop, passive threads, reactions, approvals | `domain/slack/turn_service.py`, `turn/bot_threads.py` | `brain/slack/turn.ts` |
| Event store, inbox, steering, active-turn gate | `domain/slack/turn/event_store.py`, `turn_inbox.py`, `turn_control.py`, `active_turn_gate.py` | `brain/slack/event-store.ts`, `turn-inbox.ts`, `turn-control.ts`, `active-turn-gate.ts` |
| Triage, chime-in, budgets, proactivity, reaction queue | `domain/slack/turn/triage.py`, `chime.py`, `chime_budget.py`, `proactivity.py`, `reaction_queue.py`, `reaction_policy.py` | `brain/slack/triage.ts`, `chime.ts`, `chime-budget.ts`, `proactivity.ts`, `reaction-queue.ts`, `reaction-policy.ts` |
| Approval expiry and superseded cards | `domain/slack/turn/approval_expiry.py`, `approval_requests.py`, `domain/access/approvals.py` | `brain/turn/approval-expiry.ts`, `approval-requests.ts`, `approval.ts` |
| Workspaces, install, teardown, farewell | `domain/slack/workspace.py`, `bootstrap.py`, `farewell.py`, `home_welcome.py`, `install_greeting.py`, `greet.py` | `brain/slack/workspace.ts`, `bootstrap.ts`, `farewell.ts`, `home-welcome.ts`, `install-greeting.ts`, `greet.ts` |
| Members, invites, account linking, profiles | `domain/slack/member_provisioning.py`, `team_invite.py`, `team_invite_card.py`, `account_link.py`, `profile_cache.py` | `brain/slack/member-provisioning.ts`, `turn/team-invite.ts`, `team-invite-card.ts`, `account-link.ts`, `profile-cache.ts` |
| Channels: directory, lookup, public rollout, history import, introductions, observe | `domain/slack/channels/` | `brain/slack/channel-directory.ts`, `channel-lookup.ts`, `public-channel-rollout.ts`, `history-document.ts`, `channel-introduction.ts`, `channel-observe.ts` |
| Private channel membership | `domain/slack/channel_membership.py` | `brain/slack/channel-membership.ts` |
| Memory: scopes, write and read sets, tags, topic tree, search, profiles, documents | `domain/memory/` | `brain/memory/*.ts`, `src/memory/*.ts` |
| Roles | `domain/access/roles.py` | `repo-lib/permissions.ts` |
| Manifest | `domain/slack/manifest.py`, `slack_app/manifest.yaml` | `setup/manifest.ts` |

Delayed work (approval expiry, rollout paging, observe batches, reaction spacing, invite waves) runs on `src/core/scheduler.py`, a Redis sorted set polled by one thread, in place of Durable Object alarms.

## What is next

The Slack layer runs entirely on FalkorDB. Where the product stands:

- **Agent.** Built. `src/domain/knowledge/agent.py` makes one structured model call that picks a tool from the fixed catalogue; `src/domain/slack/agent_turn.py` runs it inside the turn and renders the path. Without a model key the keyword router picks the tool, so every path works offline. The plug-in point is still `register_turn_agent()` in `src/domain/slack/compute.py`.
- **Search.** Full-text over memories, plus recall that follows `MENTIONS` edges from the entity a question names to the memories about it and its neighbours. Semantic (vector) recall was left out on purpose for the hackathon: an embedding model would have added about a gigabyte to the image, and similarity search is not graph work. FalkorDB has the vector index for it when it is added.
- **MCP.** The same catalogue over MCP, read-only and `org_shared`, is specified in docs/STRATEGY.md and is the next item.

## Differences from company-brain

- **Without a model, raw messages become memories.** company-brain only saves what channel-observe distills. With no model, `turn_service.retain_without_model` keeps human channel messages of three or more words in their scope, so answers still pick up what was said. With a model this is off.
- **The answer comes from graph tools,** not an agent loop, until an agent is registered.
- **Admin routes use a bearer token** instead of a web session.
- **Unknown full members become people on first contact** when Slack shows their email.
- **The approval card uses standard blocks** instead of Slack's `container` block, and has no tool icon.
- **No fibers.** Slack's retries plus the event store handle redelivery. A turn interrupted by a crash is closed by `finalize_interrupted_slack_turn` when called.

## Not built

- MCP tool connectors, connect buttons, and the connected-tool cross-check (serving our own tools over MCP is a separate item, see above).
- Access leasing between teammates.
- Post-turn reflection, research and skills.
- Billing, trials and telemetry.
