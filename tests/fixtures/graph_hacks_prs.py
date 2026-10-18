"""Real pull requests from codesensei-tushar/graph_hacks (a public repo), as GitHub's API returns them.

Taken with ``gh pr view <n> --json number,title,state,author,mergedAt,updatedAt,headRefName,url,body,files,reviews``
on 18 Oct 2026, bodies cut to their first 1,200 characters. Public data only. None of these had a
formal review or closed an issue; tests that need either add them and say so.
"""

REPO = {"id": 1420908267, "full_name": "codesensei-tushar/graph_hacks", "private": False, "html_url": "https://github.com/codesensei-tushar/graph_hacks"}
USERS = {
    "codesensei-tushar": {"id": 101, "login": "codesensei-tushar"},
    "noobsurd11": {"id": 102, "login": "noobsurd11"},
    "omsherikar": {"id": 103, "login": "omsherikar"},
}

PULL_REQUESTS = [{'number': 207,
  'title': 'I1: batched connector writes, content-hash skip, one generation bump per page',
  'state': 'closed',
  'merged_at': '2026-10-18T17:15:37Z',
  'updated_at': '2026-10-18T17:15:37Z',
  'html_url': 'https://github.com/codesensei-tushar/graph_hacks/pull/207',
  'user': 'noobsurd11',
  'head': {'ref': 'infra/i1-connector-writes'},
  'body': "**Size: LARGE** (the connector write path for every org's graph, and account→Person identity links).\n"
          '\n'
          '## Before / after\n'
          '\n'
          'Measured with `infra_bench_wpage.py` on `infra_before_db` / `infra_after_db`: a Quillfern seed plus 1,000 People, one '
          'Jira install. Each page has 50 tickets, 2 person edges each, ~90 accounts and an ABOUT hint per ticket. Generation '
          'bumps are the actual Redis INCRs.\n'
          '\n'
          '| page | before: queries | before: ms | before: gen bumps | after: queries | after: ms | after: gen bumps |\n'
          '| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n'
          '| 50 new tickets | 1,032 | 486 | 446 | **16** | **39** | **1** |\n'
          '| same page again (no-op poll) | 648 | 213 | 350 | **6** (all reads) | **10** | **0** |\n'
          '| 5 of 50 tickets edited | 648 | 226 | 350 | **12** | **15** | **1** |\n'
          '\n'
          'End state is identical on both: 50 tickets, 50 ABOUT, 50 ASSIGNED.\n'
          '\n'
          '## What changed (`src/connectors/graph.py`)\n'
          '\n'
          '**Accounts: `upsert_users`, batched.**\n'
          "- One read gets each account's stored hash and IS links. One read finds Person candidates by `email_lc`.\n"
          '- Writes happen only for accounts whose content hash changed. Unlink and link each take one UNWIND.',
  'files': ['docs/CONNECTORS.md',
            'src/connectors/base.py',
            'src/connectors/graph.py',
            'src/connectors/hubspot/connector.py',
            'src/connectors/hubspot/mapping.py',
            'src/connectors/jira/mapper.py',
            'src/connectors/sync.py',
            'src/db/falkordb_client.py',
            'src/domain/knowledge/ticket_status.py',
            'tests/integration/test_connector_writes.py',
            'tests/unit/test_hubspot_mapping.py',
            'tests/unit/test_jira_connector.py',
            'tests/unit/test_jira_mapper.py']},
 {'number': 216,
  'title': 'I2: scheduler worker pool, atomic leased claims, fast lane, indexed cancel',
  'state': 'closed',
  'merged_at': '2026-10-18T17:29:13Z',
  'updated_at': '2026-10-18T17:29:13Z',
  'html_url': 'https://github.com/codesensei-tushar/graph_hacks/pull/216',
  'user': 'noobsurd11',
  'head': {'ref': 'infra/i2-scheduler'},
  'body': '**Size: LARGE**: it changes how every background job runs. Stacked on #210, which adds the `QUEUE`/`PAYLOADS` '
          'constants; #210 should merge first.\n'
          '\n'
          '## Before / after (`infra_bench_sched.py`, isolated queue)\n'
          '\n'
          '| | before | after |\n'
          '| --- | ---: | ---: |\n'
          '| approval expiry due while 4 slow (2 s) jobs are due | waits **8.50 s** | **0.50 s** (one poll) |\n'
          '| `cancel_matching` of 10 jobs among 5,010 pending | 13.8 ms (HGETALL of everything) | **2.3 ms** (index set) |\n'
          '\n'
          '## Design (`src/core/scheduler.py`; the module docstring has the key layout)\n'
          '\n'
          '**Pool**\n'
          '- `SCHEDULER_WORKERS` (default 4) workers take any due job.\n'
          '- One fast-lane worker takes only `FAST` jobs (`runApprovalExpiry`), so a long sync or model call cannot starve an '
          'expiry.\n'
          '- One lease-keeper thread renews the leases of running jobs and runs the reaper.\n'
          '\n'
          '**Atomic claim (Lua)**\n'
          '- The claim moves a due job from `QUEUE` to `QUEUE:running`, scored with its lease deadline (300 s, renewed every 60 '
          's while the handler runs).\n'
          '- It does this only if it can take `QUEUE:lock:<org>:<group>` (SET NX PX). So **at most one job per (org, group) runs '
          'at a time**.',
  'files': ['docs/SLACK.md',
            'src/core/scheduler.py',
            'src/domain/slack/channels/rollout.py',
            'tests/integration/test_scheduler.py',
            'tests/unit/test_slack_channels.py']},
 {'number': 221,
  'title': 'Console follow-ups: rotate the session on promotion to owner; discard abandoned Jira connects',
  'state': 'closed',
  'merged_at': '2026-10-18T17:29:26Z',
  'updated_at': '2026-10-18T17:29:26Z',
  'html_url': 'https://github.com/codesensei-tushar/graph_hacks/pull/221',
  'user': 'noobsurd11',
  'head': {'ref': 'infra/console-followups'},
  'body': '**Size: LARGE** (session/auth).\n'
          '\n'
          '**1. Rotate the session on promotion** (follow-up from the #181 review). The spec rotates on privilege changes, so a '
          'pending creator who becomes owner now gets a new session id and CSRF token (`session.rotate`).\n'
          '- **Slack install callback:** `_promote_pending_creator` returns the new id, and the callback\'s "connected" page '
          'sets the cookie, since the SameSite=Lax cookie rides that navigation.\n'
          "- **Console request:** if a request's role refresh is what promoted the session (`_pending_but_routed`), "
          '`console_session` rotates it and sets the cookie on the response. It does this only when FastAPI hands in the '
          '`response`; an internal caller without one leaves the id as is and never loses the session.\n'
          '- The old id is deleted in both cases.\n'
          '- Tests: the callback helper leaves the old key dead and the new one owner, and `/console/api/me` after the install '
          'answers `owner` with a new `Set-Cookie` while the pending key is gone.\n'
          '\n'
          '**2. Abandoned Jira connects** (explorer round 9, item 2). A Jira connect whose consent covered several sites waits '
          'at the site picker with no site and no account; left there, it stayed `connecting` forever.',
  'files': ['docs/CONSOLE.md',
            'src/api/v1/console_connectors.py',
            'src/api/v1/slack_install.py',
            'src/domain/console/session.py',
            'tests/integration/test_console_auth.py',
            'tests/integration/test_console_connectors.py']},
 {'number': 217,
  'title': "Take the 60-second tour: the original button back at the hero's foot",
  'state': 'closed',
  'merged_at': '2026-10-18T17:08:46Z',
  'updated_at': '2026-10-18T17:08:46Z',
  'html_url': 'https://github.com/codesensei-tushar/graph_hacks/pull/217',
  'user': 'codesensei-tushar',
  'head': {'ref': 'tour-button-back'},
  'body': "Puts the original **Take the 60-second tour** button back, as #201 shipped it. #211's minimalism pass had turned it "
          'into a small "▸ 60-second tour" text link under the chips. Everything else in #211\'s minimal hero is unchanged.\n'
          '\n'
          '- **index.html:** the button is `btn btn-ghost tour-start` with the label "Take the 60-second tour", in a '
          '`.hero-foot` at the end of the hero. It is the same element (`#tour-start`, hidden until tour.js loads), so the click '
          'handler and `/?tour=1` behave as before.\n'
          '- **tour.css:** `.hero-foot` is positioned bottom-right of the hero, 28px from the bottom, at the page width. The '
          'button is 36px high, small text, 8px radius, as in #201. Under 760px it is a static row under the chips.\n'
          '- **style.css:** the three link rules (`.hero .tour-start` and its "▶") are removed. `.hero .ask-area` keeps the 34px '
          'the link took, so the composer stays where #211 put it (520 at 1536x864, the same before and after). Cache versions '
          'bumped: style.css v=18, tour.css v=3.\n'
          '- **test_tour.py and DESIGN.md:** updated to match.\n'
          '\n'
          '**Checked**\n'
          '- `ruff check .` is clean and `TEST_GRAPH_SUFFIX=_v pytest -q` passes.\n'
          '- Headless Chrome on a scratch graph:',
  'files': ['docs/DESIGN.md', 'src/static/index.html', 'src/static/style.css', 'src/static/tour.css', 'tests/unit/test_tour.py']}]


def as_api(pr: dict) -> dict:
    """One fixture as the pulls API shape ingest reads: ``user`` as an object, ``merged_at`` set when merged."""
    return {key: value for key, value in pr.items() if key != "files"} | {"user": USERS[pr["user"]]}


class FakeGitHub:
    """The GitHub client backfill uses, answering with the fixtures. ``reviews`` maps a number to review payloads."""

    def __init__(self, reviews: dict[int, list[dict]] | None = None, issues: list[dict] | None = None, installation_id: int = 7):
        self.installation_id = installation_id
        self.reviews = reviews or {}
        self.issues = issues or []

    def list_repositories(self):
        return [REPO]

    def list_collaborators(self, _full_name):
        return list(USERS.values())

    def list_issues(self, _full_name, _limit):
        return list(self.issues)

    def list_pull_requests(self, _full_name, _limit):
        return [as_api(pr) for pr in PULL_REQUESTS]

    def list_pull_request_files(self, _full_name, number):
        return [{"filename": path} for pr in PULL_REQUESTS if pr["number"] == number for path in pr["files"]]

    def list_pull_request_reviews(self, _full_name, number):
        return list(self.reviews.get(number, []))
