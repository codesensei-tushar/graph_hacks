"""``who_should_know`` on a real FalkorDB: Quillfern, its Sales team, and the scripted repos.

The setup adds a private ticket Lena and Maya both worked on, a second pull request
numbered 45 in another repo, an open pull request on HubSpot by a GitHub account that is
linked to no one, and a chain of its own for the unowned cases: Tax report (a process, no
owner) depends on Payouts (Arjun's), which depends on Ledger (no owner). For the review
fixes: Lena's open notebook#77 touches Auth and Search (Search depends on Auth); docs#12345
is titled like a service and is one digit longer than docs#1234; docs#12 shares a number
with the issue notebook#12; a second GitHub account linked to Maya worked with Lena in a
private repo; and Audit log, co-owned by Maya and Sara, has a dependent Kofi owns.
"""

import unittest
from unittest import mock

import tests  # noqa: F401  pins the test graph and owner before src loads

from src.db import falkordb_client  # noqa: E402
from src.domain.knowledge import agent, ask_service, catalogue, coordination, mcp_tools  # noqa: E402
from src.seed import quillfern, quillfern_sales, seed  # noqa: E402
from scripts.replay.driver import load_github_history  # noqa: E402

GRAPH = f"company_coordination_test{tests.TEST_GRAPH_SUFFIX}"
ORG = ["org_shared"]
PRIVATE_REPO = "github_repo_9002"
PRIVATE_ROOM = "slack_channel_GCOORD"
SIDE_REPO = "github_repo_9005"


def _ask(name: str, scopes=None):
    return catalogue.get_tool("who_should_know").run({"name": name}, scopes or ORG)


def _cites(result) -> list[str]:
    return [step.cite() for step in result.path]


class WhoShouldKnowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=GRAPH),
            mock.patch.object(seed, "graph_name", return_value=GRAPH),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)
        quillfern_sales.seed_sales()
        load_github_history()
        _, graph = falkordb_client.connect()
        graph.query(
            """
            MATCH (lena:Person {id: 'lena'}), (maya:Person {id: 'maya'}), (hub:Service {id: 'hubspot'})
            CREATE (lena)-[:WORKED_ON]->(:Ticket {id: 'TKT-PRIV', name: 'TKT-PRIV', title: 'Refund export audit', scope: $room})<-[:WORKED_ON]-(maya),
                   (:PullRequest {id: 'github:9003:pr:45', name: 'docs#45', number: 45, title: 'Document the token rotation', state: 'closed', merged: true}),
                   (crm:PullRequest {id: 'github:9004:pr:3', name: 'crm-sync#3', number: 3, title: 'Sync renewal dates from the CRM', state: 'open', merged: false}),
                   (crm)-[:TOUCHES]->(hub),
                   (:GitHubUser {id: '9999', login: 'outside-dev', name: 'outside-dev'})-[:AUTHORED]->(crm)
            WITH 1 AS done
            MATCH (arjun:Person {id: 'arjun'})
            CREATE (tax:Service {id: 'tax-report', name: 'Tax report', kind: 'process'})-[:DEPENDS_ON]->
                   (payouts:Service {id: 'payouts', name: 'Payouts'})-[:DEPENDS_ON]->(:Service {id: 'ledger', name: 'Ledger'}),
                   (arjun)-[:OWNS]->(payouts)
            """,
            {"room": PRIVATE_ROOM},
        )
        graph.query(
            """
            MATCH (auth:Service {id: 'auth-service'}), (search:Service {id: 'search-service'}), (lena:GitHubUser {login: 'lena-fischer'}),
                  (maya:Person {id: 'maya'}), (sara:Person {id: 'sara'}), (kofi:Person {id: 'kofi'})
            CREATE (pr77:PullRequest {id: 'github:9001:pr:77', name: 'notebook#77', number: 77, title: 'Share the session cache with search', state: 'open', merged: false}),
                   (lena)-[:AUTHORED]->(pr77), (pr77)-[:TOUCHES]->(auth), (pr77)-[:TOUCHES]->(search),
                   (:PullRequest {id: 'github:9003:pr:12345', name: 'docs#12345', number: 12345, title: 'Auth service', state: 'open', merged: false}),
                   (:PullRequest {id: 'github:9003:pr:12', name: 'docs#12', number: 12, title: 'Fix a broken link', state: 'open', merged: false}),
                   (alt:GitHubUser {id: '9201', login: 'maya-alt', name: 'maya-alt'})-[:IS]->(maya),
                   (side:PullRequest {id: 'github:9005:pr:60', name: 'side#60', number: 60, title: 'Try a faster tokenizer', state: 'closed', merged: true, scope: $side}),
                   (alt)-[:AUTHORED]->(side), (lena)-[:REVIEWED]->(side),
                   (audit:Service {id: 'audit-log', name: 'Audit log'}), (maya)-[:OWNS]->(audit), (sara)-[:OWNS]->(audit),
                   (kofi)-[:OWNS]->(:Service {id: 'audit-export', name: 'Audit export'})-[:DEPENDS_ON]->(audit)
            """,
            {"side": SIDE_REPO},
        )

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(GRAPH).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    # --- a pull request -------------------------------------------------------------------

    def test_notebook_45_names_who_owns_what_depends_on_auth_and_was_not_on_it(self):
        result = _ask("who should know about notebook#45")
        self.assertTrue(result.found)
        self.assertEqual(result.tool, "who_should_know")
        self.assertEqual(
            result.answer,
            "notebook#45 changes Auth Service. 2 people who own something that depends on it weren't on the pull request. "
            "Lena Fischer owns Notification Service (1 hop) and Invoice Worker (2 hops), and the graph shows no pull request, "
            "ticket or decision shared with Maya Ortiz: introduce them now, since it has already merged. "
            "Kofi Mensah owns Search Service (1 hop) and worked with Maya Ortiz on notebook#41, so a quick ping is enough.",
        )
        self.assertEqual(
            _cites(result),
            [
                "Maya Ortiz -[AUTHORED]-> notebook#45",
                "notebook#45 -[TOUCHES]-> Auth Service",
                "Notification Service -[DEPENDS_ON]-> Auth Service",
                "Lena Fischer -[OWNS]-> Notification Service",
                "Invoice Worker -[DEPENDS_ON]-> Notification Service",
                "Lena Fischer -[OWNS]-> Invoice Worker",
                "Search Service -[DEPENDS_ON]-> Auth Service",
                "Kofi Mensah -[OWNS]-> Search Service",
                "Kofi Mensah -[AUTHORED]-> notebook#41",
                "Maya Ortiz -[REVIEWED]-> notebook#41",
            ],
        )
        self.assertEqual(result.depth, coordination.MAX_HOPS)
        # Arjun owns Billing Service, which depends on Auth too, but he reviewed the pull request.
        self.assertNotIn("Arjun", result.answer)
        self.assertNotIn("Arjun Mehta", [step.from_name for step in result.path])

    def test_the_same_pull_request_by_number_or_by_the_model_s_argument(self):
        expected = _ask("who should know about notebook#52").answer
        self.assertEqual(
            expected,
            "notebook#52 changes Billing Service and Invoice Worker. 1 person who owns something that depends on them wasn't "
            "on the pull request. Maya Ortiz owns API Gateway (1 hop), and the graph shows no pull request, ticket or decision "
            "shared with Lena Fischer: introduce them now, since it has already merged.",
        )
        for name in ("PR 52", "pr #52", "PR#52", "who should know about pr#52", "pull request 52", "#52", "notebook#52", "52"):
            with self.subTest(name=name):
                self.assertEqual(_ask(name).answer, expected)
        self.assertEqual(
            _cites(_ask("notebook#52")),
            [
                "Lena Fischer -[AUTHORED]-> notebook#52",
                "notebook#52 -[TOUCHES]-> Billing Service",
                "notebook#52 -[TOUCHES]-> Invoice Worker",
                "API Gateway -[DEPENDS_ON]-> Billing Service",
                "Maya Ortiz -[OWNS]-> API Gateway",
            ],
        )

    def test_an_open_pull_request_by_an_unlinked_account_keeps_owners_with_no_github_link(self):
        result = _ask("who else should review crm-sync#3")
        self.assertEqual(
            result.answer,
            "crm-sync#3 changes the HubSpot system. 2 people who own what it changes or something that depends on it weren't on "
            "the pull request. Noor Haddad owns the HubSpot system (which it changes), and the graph cannot tell whether Noor "
            "Haddad and @outside-dev have worked together: introduce them before this merges. Priya Shah runs the Renewals "
            "process (1 hop), owns the Pricing playbook (2 hops), and the graph cannot tell whether Priya Shah and @outside-dev "
            "have worked together: introduce them before this merges.",
        )
        self.assertIn("Pricing playbook -[DEPENDS_ON]-> Renewals", _cites(result))
        self.assertIn("Priya Shah -[OWNS]-> Pricing playbook", _cites(result))
        self.assertEqual(
            _cites(result)[:3],
            ["@outside-dev -[AUTHORED]-> crm-sync#3", "crm-sync#3 -[TOUCHES]-> HubSpot", "Noor Haddad -[OWNS]-> HubSpot"],
        )

    def test_owners_of_what_changes_come_first_even_when_one_changed_thing_depends_on_another(self):
        result = _ask("who should know about notebook#77")
        self.assertEqual(
            result.answer,
            "notebook#77 changes Auth Service and Search Service. 3 people who own what it changes or something that depends on "
            "them weren't on the pull request. Maya Ortiz owns Auth Service (which it changes) and API Gateway (1 hop), and the "
            "graph shows no pull request, ticket or decision shared with Lena Fischer: introduce them before this merges. Kofi "
            "Mensah owns Search Service (which it changes), and the graph shows no pull request, ticket or decision shared with "
            "Lena Fischer: introduce them before this merges. Arjun Mehta owns Billing Service (1 hop) and worked with Lena Fischer on "
            "notebook#52, so a quick ping is enough.",
        )
        cites = _cites(result)
        self.assertIn("Maya Ortiz -[OWNS]-> Auth Service", cites)
        self.assertIn("Kofi Mensah -[OWNS]-> Search Service", cites)
        # Search depends on Auth, but it changed too: no DEPENDS_ON edge is cited for it.
        self.assertNotIn("Search Service -[DEPENDS_ON]-> Auth Service", cites)

    def test_a_pull_request_that_touches_no_service(self):
        result = _ask("who should know about docs#45")
        self.assertTrue(result.found)
        self.assertEqual(result.answer, "docs#45 touches no service in the graph, so nothing depends on what it changes.")
        self.assertIsNone(result.depth)

    def test_nobody_missing_when_every_owner_was_on_the_pull_request(self):
        result = _ask("who should know about notebook#41")
        self.assertEqual(result.answer, "Everyone who owns something that depends on Graph Store or Memory Layer was on notebook#41.")
        self.assertEqual(
            _cites(result),
            [
                "Kofi Mensah -[AUTHORED]-> notebook#41",
                "notebook#41 -[TOUCHES]-> Graph Store",
                "notebook#41 -[TOUCHES]-> Memory Layer",
                "Search Service -[DEPENDS_ON]-> Memory Layer",
                "Kofi Mensah -[OWNS]-> Search Service",
            ],
        )

    # --- refusals -------------------------------------------------------------------------

    def test_an_unknown_pull_request_is_refused_with_the_nearest_names(self):
        result = _ask("who should know about notebook#999")
        self.assertFalse(result.found)
        self.assertEqual(result.path, [])
        self.assertEqual(len(result.candidates), 3)
        self.assertTrue(all(name.startswith("notebook#") for name in result.candidates), result.candidates)
        self.assertTrue(result.answer.startswith("Nothing in the graph matches “notebook 999”. Nearest: notebook#"), result.answer)
        bare = _ask("who should know about #999")
        self.assertFalse(bare.found)
        self.assertEqual(bare.answer, "Nothing in the graph matches “999”. Name a pull request or service that exists in the graph.")

    def test_a_number_two_repos_share_is_refused_with_both(self):
        for name in ("who should know about #45", "PR 45"):
            with self.subTest(name=name):
                result = _ask(name)
                self.assertFalse(result.found)
                self.assertEqual(result.candidates, ["docs#45", "notebook#45"])
                self.assertEqual(result.answer, "More than one pull request is #45: docs#45 and notebook#45. Name the repo, as in docs#45.")

    def test_a_question_that_names_nothing_leaves_room_for_the_thread_subject(self):
        result = _ask("who should know")
        self.assertFalse(result.found)
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.answer, "I can't tell what that refers to. Name a pull request or service.")

    def test_a_repo_named_apart_from_the_number_picks_that_repo(self):
        expected = _ask("notebook#45").answer
        for name in ("who should know about notebook #45", "who should know about PR 45 in notebook", "notebook PR 45"):
            with self.subTest(name=name):
                self.assertEqual(_ask(name).answer, expected)

    def test_a_number_one_digit_longer_is_a_different_pull_request(self):
        result = _ask("who should know about docs#1234")
        self.assertFalse(result.found)
        self.assertEqual(result.candidates[0], "docs#12345")
        self.assertTrue(result.answer.startswith("Nothing in the graph matches “docs 1234”. Nearest: docs#12345"), result.answer)
        self.assertEqual(_ask("docs#12345").answer, "docs#12345 touches no service in the graph, so nothing depends on what it changes.")

    def test_an_issue_number_is_not_a_pull_request_number(self):
        self.assertEqual(_ask("who should know about #12").answer, "docs#12 touches no service in the graph, so nothing depends on what it changes.")
        issue = _ask("who should know about issue #12")
        self.assertFalse(issue.found)
        self.assertNotIn("docs#12", issue.answer)

    def test_a_pull_request_title_never_hides_a_service(self):
        # docs#12345 is titled "Auth service".
        self.assertTrue(_ask("who should know if the auth service changes").answer.startswith("Auth Service is owned by Maya Ortiz."))

    def test_a_document_or_ticket_by_name_gets_who_knows_and_an_issue_named_like_a_pull_request_is_refused(self):
        who_knows = catalogue.get_tool("who_knows")
        document = _ask("who should I tell about the billing runbook")
        self.assertTrue(document.found)
        self.assertEqual(document.tool, "who_knows")
        self.assertTrue(document.answer.startswith("Billing runbook is about Billing Service. Ask first: Arjun Mehta"), document.answer)
        self.assertEqual(document.answer, who_knows.run({"name": "the billing runbook"}, ORG).answer)
        ticket = _ask("who should know about TKT-101")
        self.assertEqual((ticket.tool, ticket.answer), ("who_knows", who_knows.run({"name": "TKT-101"}, ORG).answer))
        # notebook#12 is an issue: named like a pull request, it is refused rather than answered about.
        self.assertEqual(
            _ask("who should know about notebook#12").answer,
            "notebook#12 is a ticket, not a change. Name the pull request or service that changes, or ask who knows notebook#12.",
        )

    def test_a_service_named_only_by_the_end_of_an_unknown_name_is_refused(self):
        # The graph knows API Gateway as "api" and nothing as "claude".
        result = _ask("who should know if the claude api changes")
        self.assertFalse(result.found)
        self.assertEqual(result.candidates, ["API Gateway"])
        self.assertEqual(result.answer, "Nothing in the graph matches “claude api”. Nearest: API Gateway (known as “api”).")
        self.assertTrue(_ask("who should know if the api changes").answer.startswith("API Gateway is owned by Maya Ortiz."))

    def test_a_word_that_only_says_which_version_still_resolves(self):
        self.assertTrue(_ask("who should know about the old billing runbook").answer.startswith("Billing runbook is about Billing Service."))
        self.assertTrue(_ask("who should know if the current auth service changes").answer.startswith("Auth Service is owned by Maya Ortiz."))
        self.assertEqual(_ask("who should know if the old claude api changes").candidates, ["API Gateway"])

    # --- in a thread ------------------------------------------------------------------------

    def test_a_follow_up_takes_the_pull_request_the_thread_named(self):
        thread = [{"user": "UMAYA", "text": "auth session refresh rotates tokens every hour now, see notebook#45"}]
        question = "who should know about it"
        choice = agent.ToolChoice("who_should_know", {"name": question}, "keywords")
        result, used = agent.run_choice(choice, question, ORG, thread=thread)
        self.assertEqual(used.args, {"name": "notebook#45"})
        self.assertEqual(result.answer, _ask("notebook#45").answer)
        self.assertNotIn("Arjun", result.answer)

    def test_a_pull_request_that_is_not_there_is_never_swapped_for_the_thread_subject(self):
        thread = [{"user": "UTOMAS", "text": "who owns billing"}]
        for question in ("who should know about #999", "who should know about notebook#999"):
            with self.subTest(question=question):
                choice = agent.ToolChoice("who_should_know", {"name": question}, "keywords")
                result, used = agent.run_choice(choice, question, ORG, thread=thread)
                self.assertFalse(result.found)
                self.assertEqual(used.args, {"name": question})
                self.assertNotIn("Billing Service", result.answer)

    # --- privacy --------------------------------------------------------------------------

    def test_a_private_pull_request_is_refused_exactly_like_one_that_does_not_exist(self):
        hidden = _ask("who should know about billing-internal#7")
        self.assertFalse(hidden.found)
        self.assertEqual((hidden.path, hidden.candidates), ([], []))
        self.assertEqual(hidden.answer, "Nothing in the graph matches “billing internal 7”. Name a pull request or service that exists in the graph.")
        # A collaborator of the private repo reads it, and the collaborations it finds are the public ones.
        seen = _ask("who should know about billing-internal#7", ORG + [PRIVATE_REPO])
        self.assertTrue(seen.found)
        self.assertEqual(
            seen.answer,
            "billing-internal#7 changes Billing Service. 2 people who own something that depends on it weren't on the pull request. "
            "Maya Ortiz owns API Gateway (1 hop) and worked with Arjun Mehta on notebook#45, so a quick ping is enough. "
            "Lena Fischer owns Invoice Worker (1 hop) and worked with Arjun Mehta on notebook#52, so a quick ping is enough.",
        )

    def test_past_work_counts_only_what_the_asker_may_read(self):
        self.assertIn("Lena Fischer owns Notification Service (1 hop) and Invoice Worker (2 hops), and the graph shows no", _ask("notebook#45").answer)
        private = _ask("notebook#45", ORG + [PRIVATE_ROOM])
        self.assertIn(
            "Lena Fischer owns Notification Service (1 hop) and Invoice Worker (2 hops) and worked with Maya Ortiz on TKT-PRIV, so a quick ping is enough.",
            private.answer,
        )
        self.assertIn("Lena Fischer -[WORKED_ON]-> TKT-PRIV", _cites(private))
        self.assertIn("Maya Ortiz -[WORKED_ON]-> TKT-PRIV", _cites(private))
        self.assertNotIn("TKT-PRIV", " ".join(_cites(_ask("notebook#45"))))

    def test_work_under_another_account_linked_to_the_author_counts(self):
        result = _ask("notebook#45", ORG + [SIDE_REPO])
        self.assertIn("Lena Fischer owns Notification Service (1 hop) and Invoice Worker (2 hops) and worked with Maya Ortiz on side#60", result.answer)
        self.assertIn("Maya Ortiz -[AUTHORED]-> side#60", _cites(result))
        self.assertIn("Lena Fischer -[REVIEWED]-> side#60", _cites(result))

    # --- a service ------------------------------------------------------------------------

    def test_co_owners_who_share_the_same_work_are_named_the_same_way_every_time(self):
        self.assertEqual(
            _ask("who should know if the audit log changes").answer,
            "Audit log is owned by Maya Ortiz and Sara Lindqvist. If it changes, 1 other person owns something that depends on it. "
            "Kofi Mensah owns Audit export (1 hop) and worked with Maya Ortiz on notebook#41, so a quick ping is enough.",
        )

    def test_a_service_change_measures_past_work_against_its_owner(self):
        result = _ask("who should know if auth changes")
        self.assertTrue(result.found)
        self.assertEqual(
            result.answer,
            "Auth Service is owned by Maya Ortiz. If it changes, 3 other people own something that depends on it. "
            "Arjun Mehta owns Billing Service (1 hop) and worked with Maya Ortiz on notebook#45, so a quick ping is enough. "
            "Lena Fischer owns Notification Service (1 hop) and Invoice Worker (2 hops), and the graph shows no pull request, "
            "ticket or decision shared with Maya Ortiz: introduce them before it changes. "
            "Kofi Mensah owns Search Service (1 hop) and worked with Maya Ortiz on notebook#41, so a quick ping is enough.",
        )
        cites = _cites(result)
        self.assertEqual(cites[0], "Maya Ortiz -[OWNS]-> Auth Service")
        for line in ("Billing Service -[DEPENDS_ON]-> Auth Service", "Arjun Mehta -[REVIEWED]-> notebook#45", "Maya Ortiz -[AUTHORED]-> notebook#45"):
            self.assertIn(line, cites)
        # Maya owns API Gateway, which depends on Auth, but she owns the thing that changes.
        self.assertNotIn("Maya Ortiz -[OWNS]-> API Gateway", cites)

    def test_four_people_are_grouped_and_three_told_in_full_and_two_reviewers_have_worked_together(self):
        result = _ask("who should know if postgres changes")
        self.assertEqual(
            result.answer,
            "Postgres is owned by Sara Lindqvist. If it changes, 4 other people own something that depends on it. "
            "Maya Ortiz owns Auth Service (1 hop) and API Gateway (2 hops) and worked with Sara Lindqvist on notebook#41, "
            "so a quick ping is enough. Arjun Mehta owns Billing Service (1 hop), and the graph shows no pull request, ticket "
            "or decision shared with Sara Lindqvist: introduce them before it changes. Lena Fischer owns Invoice Worker (2 hops) "
            "and Notification Service (2 hops), and the graph shows no pull request, ticket or decision shared with Sara "
            "Lindqvist: introduce them before it changes. And 1 more: Kofi Mensah.",
        )
        # Maya and Sara both reviewed notebook#41; the person named last still has an edge.
        for line in ("Maya Ortiz -[REVIEWED]-> notebook#41", "Sara Lindqvist -[REVIEWED]-> notebook#41", "Kofi Mensah -[OWNS]-> Search Service"):
            self.assertIn(line, _cites(result))

    def test_a_service_with_no_owner_lists_people_without_measuring_past_work(self):
        result = _ask("who should know if the ledger changes")
        self.assertEqual(
            result.answer,
            "Ledger has no named owner. If it changes, 1 person owns something that depends on it. "
            "Arjun Mehta owns Payouts (1 hop). The Tax report process (2 hops) has no named owner.",
        )
        self.assertEqual(
            _cites(result),
            ["Payouts -[DEPENDS_ON]-> Ledger", "Arjun Mehta -[OWNS]-> Payouts", "Tax report -[DEPENDS_ON]-> Payouts"],
        )

    def test_the_answers_when_there_is_no_one_else_to_tell(self):
        self.assertEqual(
            _ask("who should know if payouts changes").answer,
            "Payouts is owned by Arjun Mehta. Nothing that depends on it has a named owner: the Tax report process (1 hop).",
        )
        self.assertEqual(
            _ask("who should know if the tax report changes").answer,
            "The Tax report process has no named owner. Nothing in the graph depends on it, so no one else needs to know when it changes.",
        )
        self.assertEqual(
            _ask("who should know if notification service changes").answer,
            "Notification Service is owned by Lena Fischer. No one else owns anything that depends on it.",
        )

    def test_a_shared_decision_counts_as_working_together_and_a_process_is_run(self):
        result = _ask("who needs to know if hubspot changes")
        self.assertEqual(
            result.answer,
            "The HubSpot system is owned by Noor Haddad. If it changes, 1 other person owns something that depends on it. "
            "Priya Shah runs the Renewals process (1 hop), owns the Pricing playbook (2 hops) and worked with Noor Haddad on "
            "*Sell annual plans only at renewal*, so a quick ping is enough.",
        )
        cites = _cites(result)
        self.assertIn("Priya Shah -[APPROVED]-> Sell annual plans only at renewal", cites)
        self.assertIn("Noor Haddad -[PROPOSED]-> Sell annual plans only at renewal", cites)

    def test_an_account_has_nothing_depending_on_it(self):
        result = _ask("who should know if enterprise accounts changes")
        self.assertTrue(result.found)
        self.assertEqual(result.path, [])
        self.assertEqual(result.answer, "Enterprise accounts is an account: nothing depends on it. Ask who owns it or who knows it instead.")

    # --- what the reply claims --------------------------------------------------------------

    def test_with_the_repos_loaded_a_reviewer_of_the_owner_s_pull_request_is_not_a_stranger(self):
        # Arjun reviewed Maya's notebook#45. On the seed alone the reply can only say what it could compare (below).
        self.assertIn(
            "Arjun Mehta owns Billing Service (1 hop) and worked with Maya Ortiz on notebook#45, so a quick ping is enough.",
            _ask("who should know if auth changes").answer,
        )

    def test_every_person_named_in_a_reply_has_a_cited_edge(self):
        _, graph = falkordb_client.connect()
        people = [row[0] for row in graph.ro_query("MATCH (p:Person) RETURN p.name AS name UNION MATCH (g:GitHubUser) RETURN '@' + g.login AS name").result_set]
        for question in ("who else should review notebook#52", "notebook#45", "notebook#77", "crm-sync#3", "who should know if postgres changes"):
            with self.subTest(question=question):
                result = _ask(question)
                cited = {name for step in result.path for name in (step.from_name, step.to_name)}
                named = [person for person in people if person in result.answer]
                self.assertTrue(named)
                self.assertEqual([person for person in named if person not in cited], [], result.answer)
        self.assertEqual(_cites(_ask("who else should review notebook#52"))[0], "Lena Fischer -[AUTHORED]-> notebook#52")

    # --- served the same way everywhere ---------------------------------------------------

    def test_it_is_a_read_only_tool_served_over_mcp(self):
        spec = catalogue.get_tool("who_should_know")
        self.assertFalse(spec.needs_approval)
        self.assertEqual(spec.args_schema["required"], ["name"])
        self.assertIn("who_should_know", mcp_tools.tool_names())
        served = mcp_tools.call_tool("who_should_know", {"name": "notebook#45"})
        self.assertTrue(served["found"])
        self.assertEqual(served["answer"], _ask("notebook#45").answer)
        self.assertIn({"from": "notebook#45", "rel": "TOUCHES", "to": "Auth Service"}, served["path"])

    def test_the_keyword_path_with_no_model_runs_it(self):
        routed = ask_service.ask("who should know about notebook#45", ORG)
        self.assertEqual(routed.tool, "who_should_know")
        self.assertEqual(routed.answer, _ask("notebook#45").answer)

    def test_the_keyword_path_sends_people_questions_to_who_knows(self):
        # What the public page does with no model: these name people to ask, not a change.
        for question, about in (
            ("who should review the billing runbook", "Billing runbook is about Billing Service."),
            ("who should know about the billing runbook", "Billing runbook is about Billing Service."),
            ("who should I ping about billing", "Billing Service is owned by Arjun Mehta."),
            ("who should we ping about the auth outage", "Auth Service is owned by Maya Ortiz."),
        ):
            with self.subTest(question=question):
                routed = ask_service.ask(question, ORG)
                self.assertEqual((routed.tool, routed.found), ("who_knows", True))
                self.assertTrue(routed.answer.startswith(about), routed.answer)
        # "PR-45" is a pull request number, not a ticket id: two repos have a #45.
        self.assertEqual(
            ask_service.ask("who should know about PR-45", ORG).answer,
            "More than one pull request is #45: docs#45 and notebook#45. Name the repo, as in docs#45.",
        )
        self.assertEqual(ask_service.ask("who should know about PR-52", ORG).answer, _ask("notebook#52").answer)


class WhoShouldKnowOnTheSeedAloneTest(unittest.TestCase):
    """Production's shape: the Quillfern seed with no GitHub history and no linked accounts."""

    @classmethod
    def setUpClass(cls):
        graph = f"company_coordination_seed_test{tests.TEST_GRAPH_SUFFIX}"
        cls._patches = [
            mock.patch.object(falkordb_client, "graph_name", return_value=graph),
            mock.patch.object(seed, "graph_name", return_value=graph),
        ]
        for patch in cls._patches:
            patch.start()
        seed.reset_and_seed(quillfern)

    @classmethod
    def tearDownClass(cls):
        try:
            falkordb_client.client().select_graph(falkordb_client.graph_name()).delete()
        finally:
            for patch in cls._patches:
                patch.stop()

    def test_with_no_linked_accounts_no_pull_request_is_claimed(self):
        result = _ask("who should know if postgres changes")
        self.assertEqual(
            result.answer,
            "Postgres is owned by Sara Lindqvist. If it changes, 4 other people own something that depends on it. "
            "Maya Ortiz owns Auth Service (1 hop) and API Gateway (2 hops), and the graph shows no ticket or decision shared "
            "with Sara Lindqvist: introduce them before it changes. Arjun Mehta owns Billing Service (1 hop), and the graph "
            "shows no ticket or decision shared with Sara Lindqvist: introduce them before it changes. Lena Fischer owns "
            "Invoice Worker (2 hops) and Notification Service (2 hops), and the graph shows no ticket or decision shared with "
            "Sara Lindqvist: introduce them before it changes. And 1 more: Kofi Mensah.",
        )
        self.assertIn(
            "Arjun Mehta owns Billing Service (1 hop), and the graph shows no ticket or decision shared with Maya Ortiz: introduce them before it changes.",
            _ask("who should know if auth changes").answer,
        )
        for question in ("who should know if postgres changes", "who should know if auth changes", "who should know if billing changes"):
            with self.subTest(question=question):
                answer = _ask(question).answer
                self.assertNotIn("pull request", answer)
                self.assertNotIn("never", answer)


if __name__ == "__main__":
    unittest.main()
