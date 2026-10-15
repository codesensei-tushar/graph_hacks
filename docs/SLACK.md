# Slack

Company Brain lives in Slack. You DM it or @mention it, and it answers from the FalkorDB graph and cites the path it walked. Messages in channels it's in become scoped memories in the same graph.

The Slack layer is ported from [company-brain](https://github.com/supermemoryai/company-brain). Its memory store (a hosted memory API) is replaced by FalkorDB.

## Setup

1. Start FalkorDB and seed the graph (see [SETUP.md](SETUP.md)).
2. Start a public tunnel to port 8000, for example `ngrok http 8000`.
3. In [api.slack.com/apps](https://api.slack.com/apps), choose **Create New App → From a manifest**. Pick your sandbox workspace and paste [`slack_app/manifest.yaml`](../slack_app/manifest.yaml) after replacing `YOUR-TUNNEL.ngrok-free.app` with your tunnel host.
4. **Install to Workspace**.
5. Copy the values into `.env`:

   | Variable | Where to find it |
   | --- | --- |
   | `SLACK_BOT_TOKEN` | OAuth & Permissions → Bot User OAuth Token (`xoxb-...`) |
   | `SLACK_SIGNING_SECRET` | Basic Information → App Credentials → Signing Secret |
   | `SLACK_OWNER_ID` | Your Slack member id (profile → ⋯ → Copy member ID). Becomes the owner. |
   | `SLACK_ADMIN_IDS` | Optional comma-separated member ids that start as admins |

6. Run the app:

   ```bash
   uvicorn src.main:app --reload
   ```

7. In the app settings, open **Event Subscriptions** and confirm the request URL shows **Verified**. If it doesn't, click **Retry**.
8. Invite the bot to a channel with `/invite @company-brain`, or DM it.

Endpoints: `POST /slack/events` (Events API, signed), `POST /slack/interactions` (buttons, signed), `GET /api/health`.

## Using it

The bot answers when you DM it, when you @mention it, or when you reply in a thread it's already in.

| Message | What happens |
| --- | --- |
| `help` | Lists the commands. |
| `remember: <text>` | Saves the text to the current room's scope. |
| `ticket: <title>` | Posts an Approve/Deny card. Only you can click it, and it expires in 15 minutes. Approving writes a `Ticket` into the graph. |
| `what can you see` | Lists the scopes this conversation can read. |
| `promote @user` / `demote @user` | Admins and owner only. Grants or removes admin. |
| `forget this channel` | In a DM, clears your own DM memory. In a channel, admins and owner only: deletes that room's memories (for a public channel, only messages from that channel). |
| anything else | A question. It's answered from the graph plus the memories you can read, and the reply cites the nodes and edges it used. |

The bot keeps ordinary channel messages of three or more words as `Memory` nodes in that room's scope. Each one links to the services and tickets it names, so later answers can find what people said about a service.

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
- Private channel membership is stored in FalkorDB. `member_joined_channel` and `member_left_channel` events keep it current, and a `conversations.members` backfill runs when the bot joins a channel. If you leave a channel, its memory stops showing up in your DMs.
- Roles rank `owner > admin > member`. The owner is `SLACK_OWNER_ID`. Admins come from `SLACK_ADMIN_IDS` or `promote`.
- Graph writes (tickets) wait for an explicit Approve from the person who asked.

## Ported from company-brain

| Here | Source |
| --- | --- |
| Signature check, 5 minute skew, retry header | `brain/slack/verify.ts` |
| Event envelopes and classifiers (answered, context retention, membership, name wake) | `brain/slack/events.ts` |
| Plain-name wake ("hey brain, ...") | `brain/slack/bot-address.ts` |
| Reply target resolution | `brain/slack/routing.ts` |
| Event dedupe and per-message turn lock | `routes/slack/index.ts` |
| Memory scopes, write tag, read set | `brain/memory/writeback.ts`, `brain/memory/read-scope.ts` |
| Private channel membership and backfill | `brain/slack/channel-membership.ts` |
| Roles and rank | `repo-lib/permissions.ts` |
| Approve/Deny cards, asker-only, 15 minute expiry | `brain/turn/approval.ts`, `brain/slack/client.ts` |

Storage moved from Durable Object SQL and KV to FalkorDB. Graph nodes hold memories and membership. The Redis keyspace under FalkorDB holds dedupe keys and approvals.

## Not built yet

- The LLM agent turn. Answers come from fixed Cypher tools for now.
- Triage and proactive chime-in. The classifiers are ported, but nothing routes to them yet.
- Public-channel rollout and history backfill.
- Access leasing between teammates.
- MCP and tool connectors (GitHub, Linear, and others).
- Multi-workspace OAuth install. Tokens come from `.env`, so it's one workspace per deployment.
