"""Test settings. Loaded before any test module imports ``src``.

``src.core.config.settings`` is built once at first import, so per-module env
changes are too late once another module imported ``src``. Pinning them here keeps
every test off the dev ``company`` graph and gives the role tests a known owner.
"""

import os

# TEST_GRAPH_SUFFIX lets several checkouts run the suite on one FalkorDB at the same
# time without resetting each other's graph, e.g. TEST_GRAPH_SUFFIX=_a pytest.
TEST_GRAPH_SUFFIX = os.environ.get("TEST_GRAPH_SUFFIX", "")
os.environ["FALKORDB_GRAPH"] = f"company_test{TEST_GRAPH_SUFFIX}"
os.environ["SLACK_OWNER_ID"] = "UOWNER"
os.environ["SLACK_ADMIN_IDS"] = ""
# Tests never call a real model; LLM-gated code takes its no-model fallback.
os.environ["LLM_API_KEY"] = ""
# Tests never reach a real Slack workspace: with a blank bot token any call that slips
# past a fake fails on auth instead of posting somewhere. The signing secret is a fixed
# dummy so tests can sign events for /slack/events without reading the dev .env value.
os.environ["SLACK_BOT_TOKEN"] = ""
os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret"
