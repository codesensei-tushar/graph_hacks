"""Test settings. Loaded before any test module imports ``src``.

``src.core.config.settings`` is built once at first import, so per-module env
changes are too late once another module imported ``src``. Pinning them here keeps
every test off the dev ``company`` graph and gives the role tests a known owner.
"""

import os

os.environ["FALKORDB_GRAPH"] = "company_test"
os.environ["SLACK_OWNER_ID"] = "UOWNER"
os.environ["SLACK_ADMIN_IDS"] = ""
# Tests never call a real model; LLM-gated code takes its no-model fallback.
os.environ["LLM_API_KEY"] = ""
