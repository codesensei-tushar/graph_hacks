"""Slack turn machinery ported from company-brain ``brain/slack/*`` (event store, inbox,
steering, reactions, triage, chime) and ``brain/turn/approval-*``.

Durable Object SQL tables become Redis structures under FalkorDB (``brain:`` keys);
alarms become ``src.core.scheduler`` jobs. The DO ran one org per instance and
serialized its SQL; here a single in-process lock in ``_state`` plays that role, so
the app assumes one worker process.
"""
