"""Port of company-brain `brain/slack/membership-provenance.ts`.

Which Slack-provisioned org membership to drop when a Slack identity is revoked:
none while any mapping for that person is still active, otherwise the first one
Slack provisioned.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackMembershipMapping:
    status: str
    provisioned_member_id: str | None


def provisioned_membership_to_revoke(mappings: list[SlackMembershipMapping]) -> str | None:
    if any(mapping.status == "active" for mapping in mappings):
        return None
    return next((mapping.provisioned_member_id for mapping in mappings if mapping.provisioned_member_id), None)
