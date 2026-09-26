from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService

ROOT = Path(__file__).resolve().parents[1]


class CloseoutApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        service = TaxonomyLabService(self.connection, clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("instructor", "instructor"),
            ("curator", "curator"),
            ("auditor", "auditor"),
        ):
            service.create_user(user_id, user_id, role)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        service.register_device("operator", "scope-a", "A 型", "厂商")
        service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        service.publish_evidence_protocol("stat", protocol)
        service.create_batch("operator", "batch-a", protocol["evidence_protocol_id"], 1, "build-a")
        service.start_batch("operator", "batch-a", 1)
        service.import_evidence_items("operator", "batch-a", "import-1", rows)
        service.seal_batch("stat", "batch-a", 2)
        job = service.claim_job("worker", 30)
        analysis = service.complete_job("worker", job["job_id"], "stat")
        service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "采信")
        service.register_material_stock("operator", "batch-a", "collectible_sample", "P1", 2)
        service.record_disposition("curator", "batch-a", "collectible_sample", "P1", "accession", 2, "入藏")
        self.service = service
        self.app = JsonApplication(service)

    def tearDown(self) -> None:
        self.connection.close()

    def _request(self, method: str, path: str, actor: str | None, payload: dict, key: str | None = None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        if key:
            headers["Idempotency-Key"] = key
        return self.app.handle(
            method, path, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def test_closeout_confirmation_chain_over_http(self) -> None:
        submitted = self._request(
            "POST", "/batches/batch-a/closeouts", "instructor",
            {"participant_ids": ["operator", "instructor"], "note": "结项"}, key="c1",
        )
        self.assertEqual(submitted.status, 201)
        closeout_id = submitted.body["closeout_id"]
        digest = submitted.body["content_sha256"]

        fetched = self._request("GET", f"/closeouts/{closeout_id}", "auditor", {})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["state"], "submitted")

        instructor = self._request(
            "POST", f"/closeouts/{closeout_id}/instructor_confirm", "instructor", {}
        )
        self.assertEqual(instructor.status, 200)
        self.assertEqual(instructor.body["state"], "instructor_confirmed")

        museum = self._request(
            "POST", f"/closeouts/{closeout_id}/museum_confirm", "curator",
            {"expected_content_sha256": digest},
        )
        self.assertEqual(museum.status, 200)
        self.assertEqual(museum.body["state"], "confirmed")

        replay = self._request(
            "POST", f"/closeouts/{closeout_id}/museum_confirm", "curator",
            {"expected_content_sha256": digest},
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body["content_sha256"], digest)

        report = self._request("GET", "/batches/batch-a/closeout_report", "auditor", {})
        self.assertEqual(report.status, 200)
        self.assertTrue(report.body["conservation"]["conserved"])
        self.assertEqual(report.body["materials"][0]["dispositions"][0]["reason"], "入藏")

    def test_withdraw_and_partial_return_routes(self) -> None:
        submitted = self._request(
            "POST", "/batches/batch-a/closeouts", "instructor",
            {"participant_ids": ["instructor"]}, key="c2",
        )
        closeout_id = submitted.body["closeout_id"]
        withdrawn = self._request(
            "POST", f"/closeouts/{closeout_id}/withdraw", "instructor", {"reason": "撤回修改"}
        )
        self.assertEqual(withdrawn.status, 200)
        self.assertEqual(withdrawn.body["state"], "withdrawn")

        resubmitted = self._request(
            "POST", "/batches/batch-a/closeouts", "instructor",
            {"participant_ids": ["instructor", "operator"]}, key="c3",
        )
        new_id = resubmitted.body["closeout_id"]
        self._request("POST", f"/closeouts/{new_id}/instructor_confirm", "instructor", {})
        partial = self._request(
            "POST", f"/closeouts/{new_id}/return", "curator",
            {"reason": "部分数量待核", "partial": True, "fields": ["materials"]},
        )
        self.assertEqual(partial.status, 200)
        self.assertEqual(partial.body["state"], "partially_returned")

        listing = self._request("GET", "/batches/batch-a/closeouts", "auditor", {})
        self.assertEqual([item["state"] for item in listing.body["closeouts"]],
                         ["withdrawn", "partially_returned"])

    def test_supersede_route_reopens_batch(self) -> None:
        submitted = self._request(
            "POST", "/batches/batch-a/closeouts", "instructor",
            {"participant_ids": ["instructor"]}, key="c4",
        )
        closeout_id = submitted.body["closeout_id"]
        self._request("POST", f"/closeouts/{closeout_id}/instructor_confirm", "instructor", {})
        self._request("POST", f"/closeouts/{closeout_id}/museum_confirm", "curator", {})
        superseded = self._request(
            "POST", f"/closeouts/{closeout_id}/supersede", "instructor", {"reason": "方案更正"}
        )
        self.assertEqual(superseded.status, 200)
        self.assertEqual(superseded.body["superseded_reason"], "方案更正")
        batch = self.service.get_batch("batch-a")
        self.assertEqual(batch["state"], "running")

    def test_submit_requires_idempotency_key(self) -> None:
        response = self._request(
            "POST", "/batches/batch-a/closeouts", "instructor", {"participant_ids": ["instructor"]}
        )
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
