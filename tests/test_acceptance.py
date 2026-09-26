from __future__ import annotations

import unittest
from pathlib import Path

from taxonomy_lab.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence_item_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(result["closure"]["state"], "confirmed")
        self.assertEqual(result["closure"]["version"], 1)
        self.assertEqual(len(result["closure"]["content_sha256"]), 64)
        self.assertEqual(result["closure"]["confirmations"], ["instructor", "museum"])
        self.assertEqual(result["closure"]["material_count"], 4)
        self.assertTrue(result["conservation_conserved"])
        self.assertTrue(result["specimen_trace"]["conserved"])
        self.assertEqual(
            result["closure"]["accession_catalog_codes"], ["batch-demo-M4-C1"]
        )


if __name__ == "__main__":
    unittest.main()
