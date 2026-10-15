"""Port of company-brain `brain/slack/identity-lock.ts`.

Postgres advisory locks serialised concurrent callbacks for one identity or
workspace. FalkorDB has no equivalent, so the writes they guarded are idempotent
MERGEs instead and these are no-ops kept for call-site symmetry.
"""


def slack_identity_advisory_lock() -> None:
    pass


def slack_workspace_advisory_lock() -> None:
    pass
