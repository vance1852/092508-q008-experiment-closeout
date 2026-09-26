from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.service import TaxonomyLabService

ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TaxonomyLabService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str | None = None, key: str | None = None) -> object:
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        if key:
            headers["Idempotency-Key"] = key
        return self.app.handle(
            "POST", path, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def _get(self, path: str, actor: str | None = None) -> object:
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle("GET", path, headers)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def test_new_roles_accepted(self) -> None:
        for role in ("instructor", "museum_officer"):
            response = self._post("/users", {"user_id": f"u-{role}", "display_name": role, "role": role})
            self.assertEqual(response.status, 201)


class ClosureApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"), ("stat", "statistician"), ("approver", "approver"),
            ("teacher", "instructor"), ("museum", "museum_officer"), ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        protocol = json.loads((ROOT / "fixtures" / "demo_evidence_protocol.json").read_text())
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", rows)
        self.material_id = self.service.register_material(
            "operator", "batch-a", "S1", "accession_candidate", "样品", 6, "件"
        )["material_id"]
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 60)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "采信")

    def tearDown(self) -> None:
        self.connection.close()

    def _headers(self, actor: str, key: str | None = None) -> dict[str, str]:
        headers = {"X-Actor-Id": actor, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def _post(self, path: str, payload: dict, actor: str, key: str | None = None):
        return self.app.handle(
            "POST", path, self._headers(actor, key),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def test_closure_routes_end_to_end(self) -> None:
        payload = {
            "participants": [{"user_id": "operator", "role": "操作员"}],
            "materials": [{
                "material_id": self.material_id, "accession_quantity": 4,
                "returned_quantity": 2, "destroyed_quantity": 0, "reason": "四件入藏两件退回",
            }],
        }
        submitted = self._post("/batches/batch-a/closures", payload, "teacher")
        self.assertEqual(submitted.status, 201)
        closure_id = submitted.body["closure_id"]

        listed = self.app.handle("GET", "/batches/batch-a/closures", {"X-Actor-Id": "auditor"})
        self.assertEqual(listed.status, 200)
        self.assertEqual(len(listed.body["closures"]), 1)

        instructor = self._post(f"/closures/{closure_id}/confirm", {"party": "instructor"}, "teacher", "ik")
        self.assertEqual(instructor.status, 200)
        self.assertTrue(instructor.body["replay_matched"])

        museum = self._post(f"/closures/{closure_id}/confirm", {"party": "museum"}, "museum", "mk")
        self.assertEqual(museum.status, 200)
        self.assertEqual(museum.body["state"], "confirmed")

        detail = self.app.handle("GET", f"/closures/{closure_id}", {"X-Actor-Id": "auditor"})
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.body["state"], "confirmed")
        self.assertEqual(len(detail.body["confirmations"]), 2)

        conservation = self.app.handle("GET", "/batches/batch-a/conservation", {"X-Actor-Id": "auditor"})
        self.assertEqual(conservation.status, 200)
        self.assertTrue(conservation.body["all_conserved"])

        trace = self.app.handle("GET", f"/materials/{self.material_id}/trace", {"X-Actor-Id": "auditor"})
        self.assertEqual(trace.status, 200)
        self.assertTrue(trace.body["conservation"]["conserved"])
        self.assertEqual(len(trace.body["accession_records"]), 1)

    def test_confirm_requires_idempotency_key(self) -> None:
        payload = {
            "participants": [{"user_id": "operator", "role": "操作员"}],
            "materials": [{
                "material_id": self.material_id, "accession_quantity": 6,
                "returned_quantity": 0, "destroyed_quantity": 0, "reason": "全入藏",
            }],
        }
        submitted = self._post("/batches/batch-a/closures", payload, "teacher")
        response = self._post(
            f"/closures/{submitted.body['closure_id']}/confirm", {"party": "instructor"}, "teacher"
        )
        self.assertEqual(response.status, 422)

    def test_withdraw_and_invalidate_routes(self) -> None:
        payload = {
            "participants": [{"user_id": "operator", "role": "操作员"}],
            "materials": [{
                "material_id": self.material_id, "accession_quantity": 6,
                "returned_quantity": 0, "destroyed_quantity": 0, "reason": "全入藏",
            }],
        }
        submitted = self._post("/batches/batch-a/closures", payload, "teacher")
        withdrawn = self._post(
            f"/closures/{submitted.body['closure_id']}/withdraw", {"reason": "填报错误"}, "teacher"
        )
        self.assertEqual(withdrawn.status, 200)
        self.assertEqual(withdrawn.body["state"], "withdrawn")

        second = self._post("/batches/batch-a/closures", payload, "teacher")
        self.assertEqual(second.body["version"], 2)
        self._post(f"/closures/{second.body['closure_id']}/confirm", {"party": "instructor"}, "teacher", "ik")
        self._post(f"/closures/{second.body['closure_id']}/confirm", {"party": "museum"}, "museum", "mk")
        invalidated = self._post(
            f"/closures/{second.body['closure_id']}/invalidate",
            {"category": "protocol_correction", "reason": "方案阈值更正"}, "teacher",
        )
        self.assertEqual(invalidated.status, 200)
        self.assertEqual(invalidated.body["state"], "invalidated")


if __name__ == "__main__":
    unittest.main()
