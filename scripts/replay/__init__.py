"""The demo loader: a fake Slack Web API, a fake GitHub client, and a driver that replays
the scripted Quillfern workspace into the app through its real routes.

It lives under ``scripts/`` because it is a tool, not a test: ``replay_workspace.py``
runs it to load a demo graph, and ``tests/integration/test_demo_replay.py`` runs the
same code and asserts on the replies. (Nothing here may import ``tests``: that package
pins the test environment the moment it is imported.)
"""
