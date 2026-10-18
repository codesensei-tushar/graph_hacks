"""Kind wording and validation, pure."""

import unittest

from src.domain.knowledge import kinds


class KindsTest(unittest.TestCase):
    def test_normalize_defaults_and_refuses_unknown_kinds(self):
        self.assertEqual(kinds.normalize_kind(None), "service")
        self.assertEqual(kinds.normalize_kind(" Process "), "process")
        with self.assertRaises(ValueError):
            kinds.normalize_kind("department")
        self.assertEqual(kinds.kind_or_default("department"), "service")

    def test_wording_per_kind(self):
        self.assertEqual(kinds.subject("Billing Service", "service"), "Billing Service")
        self.assertEqual(kinds.subject("Billing Service", None), "Billing Service")
        self.assertEqual(kinds.subject("Renewals", "process"), "the Renewals process")
        self.assertEqual(kinds.subject("Pricing playbook", "playbook"), "the Pricing playbook")
        # A plural name takes no article: "Enterprise accounts is owned by", not "The Enterprise accounts is".
        self.assertEqual(kinds.subject("Enterprise accounts", "account"), "Enterprise accounts")
        self.assertEqual(kinds.subject("Key account", "account"), "the Key account")
        self.assertEqual(kinds.listed("HubSpot", "system"), "HubSpot system")
        self.assertEqual(kinds.listed("Auth Service", "service"), "Auth Service")
        self.assertEqual(
            [kinds.down(kind) for kind in kinds.SERVICE_KINDS],
            ["goes down", "goes down", "stalls", "is affected", "is affected", "no longer applies", "no longer applies"],
        )
        self.assertEqual((kinds.owned("process"), kinds.owned("system")), ("run by", "owned by"))
