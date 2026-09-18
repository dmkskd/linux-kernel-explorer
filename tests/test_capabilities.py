"""Unsupported data is distinct from empty data and provider/probe faults."""
import unittest
from unittest.mock import Mock

from kexplore.catalog.registry import Entry
from kexplore.core.nav import collection_rows


class CapabilityTests(unittest.TestCase):
    def test_unavailable_does_not_run_provider_or_claim_empty(self):
        provider = Mock()
        entry = Entry("test", "test", "test", provider,
                      capability=lambda _: "Required tracking is not enabled")
        result = entry.check(None)
        self.assertFalse(result.supported)
        self.assertFalse(result.ok)
        provider.assert_not_called()
        rows = collection_rows(result.collection)
        self.assertEqual(rows[0].name, "Unavailable on this kernel")
        self.assertIn("not enabled", rows[0].value)
        self.assertNotEqual(rows[0].kind, "error")

    def test_supported_empty_collection_is_success(self):
        result = Entry("test", "test", "test", lambda _: (),
                       capability=lambda _: None).check(None)
        self.assertTrue(result.supported)
        self.assertTrue(result.ok)

    def test_probe_and_provider_faults_are_real_failures(self):
        for provider, capability in ((lambda _: (), Mock(side_effect=RuntimeError("probe fault"))),
                                     (Mock(side_effect=RuntimeError("provider fault")), lambda _: None)):
            result = Entry("test", "test", "test", provider, capability=capability).check(None)
            self.assertTrue(result.supported)
            self.assertFalse(result.ok)
            self.assertIn("fault", result.detail)


if __name__ == "__main__":
    unittest.main()
