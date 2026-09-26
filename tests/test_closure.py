from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from taxonomy_lab.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService

ROOT = Path(__file__).resolve().parents[1]


class ClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("teacher", "instructor"),
            ("museum", "museum_officer"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", "demo-taxonomy-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "key-1", self.rows)
        self.participants = [{"user_id": "operator", "role": "操作员", "note": "现场"}]

    def tearDown(self) -> None:
        self.connection.close()

    def _register_materials(self) -> dict[str, int]:
        specs = {
            "live": ("LIVE-1", "live_observation", 10, "只"),
            "slide": ("SLIDE-1", "temporary_slide", 6, "片"),
            "reagent": ("REAG-1", "residual_reagent", 500, "mL"),
            "specimen": ("SPEC-1", "accession_candidate", 4, "件"),
        }
        ids: dict[str, int] = {}
        for key, (code, category, quantity_value, unit) in specs.items():
            ids[key] = self.service.register_material(
                "operator", "batch-a", code, category, code, quantity_value, unit
            )["material_id"]
        return ids

    def _consume(self, ids: dict[str, int]) -> None:
        self.service.record_consumption("operator", ids["live"], 3, "观察放归")
        self.service.record_consumption("operator", ids["slide"], 2, "制片破碎")
        self.service.record_consumption("operator", ids["reagent"], 300, "实验消耗")

    def _decide(self) -> None:
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 60)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")

    def _dispositions(self, ids: dict[str, int]) -> list[dict[str, object]]:
        return [
            {"material_id": ids["live"], "accession_quantity": 0, "returned_quantity": 7,
             "destroyed_quantity": 0, "reason": "活体归还学校"},
            {"material_id": ids["slide"], "accession_quantity": 0, "returned_quantity": 0,
             "destroyed_quantity": 4, "reason": "临时玻片销毁"},
            {"material_id": ids["reagent"], "accession_quantity": 0, "returned_quantity": 0,
             "destroyed_quantity": 200, "reason": "废液销毁"},
            {"material_id": ids["specimen"], "accession_quantity": 3, "returned_quantity": 1,
             "destroyed_quantity": 0, "reason": "三件入藏一件退回"},
        ]

    def _confirmed_closure(self) -> dict[str, object]:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        closure = self.service.submit_closure("teacher", "batch-a", self.participants, self._dispositions(ids))
        self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "inst-key")
        self.service.confirm_closure("museum", closure["closure_id"], "museum", "mus-key")
        return self.service.get_closure(closure["closure_id"])

    # ------------------------------------------------------------------

    def test_full_closure_is_immutable_and_conserved(self) -> None:
        closure = self._confirmed_closure()
        self.assertEqual(closure["state"], "confirmed")
        self.assertEqual(closure["version"], 1)
        self.assertEqual(len(closure["content_sha256"]), 64)
        # 快照冻结了方案版本、参与者、异常排除与材料去向。
        snapshot = closure["snapshot"]
        self.assertEqual(snapshot["evidence_protocol"]["version"], 1)
        self.assertEqual([p["user_id"] for p in snapshot["participants"]], ["operator"])
        self.assertEqual(len(snapshot["materials"]), 4)
        # 馆方确认生成不可变入藏档案。
        accessions = self.connection.execute(
            "SELECT catalog_code,quantity,reason,closure_id FROM accession_records ORDER BY accession_id"
        ).fetchall()
        self.assertEqual(len(accessions), 1)
        self.assertEqual(accessions[0]["quantity"], "3")
        self.assertEqual(accessions[0]["closure_id"], closure["closure_id"])
        report = self.service.conservation_report("auditor", "batch-a")
        self.assertTrue(report["all_conserved"])
        self.assertTrue(all(item["conserved"] for item in report["materials"]))

    def test_closure_requires_decided_batch(self) -> None:
        ids = self._register_materials()
        with self.assertRaises(InvalidState):
            self.service.submit_closure("teacher", "batch-a", self.participants, self._dispositions(ids))

    def test_conservation_rejects_over_and_under(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        too_many = [dict(line, destroyed_quantity=5) for line in self._dispositions(ids)]
        # slide 剩 4，申报销毁 5。
        with self.assertRaises(ValidationFailed):
            self.service.submit_closure("teacher", "batch-a", self.participants, too_many)
        too_few = [dict(line) for line in self._dispositions(ids)]
        too_few[2]["destroyed_quantity"] = 100  # reagent 还有 100 未交代
        with self.assertRaises(ValidationFailed):
            self.service.submit_closure("teacher", "batch-a", self.participants, too_few)

    def test_material_lines_must_cover_every_material(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        partial = self._dispositions(ids)[:3]
        with self.assertRaises(ValidationFailed):
            self.service.submit_closure("teacher", "batch-a", self.participants, partial)

    def test_consumption_cannot_exceed_registration(self) -> None:
        ids = self._register_materials()
        with self.assertRaises(InvalidState):
            self.service.record_consumption("operator", ids["specimen"], 5, "超量")

    def test_role_separation(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        with self.assertRaises(Forbidden):
            self.service.submit_closure("operator", "batch-a", self.participants, self._dispositions(ids))
        closure = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        # 馆方角色无权执行指导教师确认。
        with self.assertRaises(Forbidden):
            self.service.confirm_closure("museum", closure["closure_id"], "instructor", "k")
        self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "k")
        with self.assertRaises(Forbidden):
            self.service.confirm_closure("operator", closure["closure_id"], "museum", "k2")

    def test_museum_cannot_confirm_before_instructor(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        closure = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        with self.assertRaises(InvalidState):
            self.service.confirm_closure("museum", closure["closure_id"], "museum", "k")

    def test_duplicate_confirmation_replays_original_result(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        closure = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        first = self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "same-key")
        second = self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "same-key")
        self.assertEqual(first, second)
        self.assertTrue(second["replay_matched"])
        # 确认行只有一条，没有重复落库。
        count = self.connection.execute(
            "SELECT count(*) FROM closure_confirmations WHERE closure_id=?", (closure["closure_id"],)
        ).fetchone()[0]
        self.assertEqual(count, 1)
        # 换幂等键重复确认被拒绝。
        with self.assertRaises(Conflict):
            self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "other-key")

    def test_withdraw_and_resubmit_creates_version_chain(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        first = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        withdrawn = self.service.withdraw_closure("teacher", first["closure_id"], "填报错误")
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.assertEqual(withdrawn["withdrawn_reason"], "填报错误")
        # 撤回后允许重新提交，产生新版本。
        second = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        self.assertEqual(second["version"], 2)
        chain = self.service.list_closures("auditor", "batch-a")["closures"]
        self.assertEqual([(row["version"], row["state"]) for row in chain], [
            (1, "withdrawn"), (2, "submitted"),
        ])

    def test_non_submitter_cannot_withdraw(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        closure = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        with self.assertRaises(Forbidden):
            self.service.withdraw_closure("museum", closure["closure_id"], "代撤")

    def test_confirmed_closure_cannot_be_withdrawn_but_can_be_invalidated(self) -> None:
        closure = self._confirmed_closure()
        with self.assertRaises(InvalidState):
            self.service.withdraw_closure("teacher", closure["closure_id"], "想撤")
        invalidated = self.service.invalidate_closure(
            "teacher", closure["closure_id"], "protocol_correction", "方案阈值更正"
        )
        self.assertEqual(invalidated["state"], "invalidated")
        self.assertEqual(invalidated["invalidation_category"], "protocol_correction")
        self.assertEqual(invalidated["invalidation_reason"], "方案阈值更正")

    def test_invalidation_keeps_archived_accession_and_blocks_override(self) -> None:
        closure = self._confirmed_closure()
        specimen_id = next(
            line["material_id"] for line in closure["materials"] if line["category"] == "accession_candidate"
        )
        self.service.invalidate_closure(
            "teacher", closure["closure_id"], "observation_correction", "关键观测校准漂移"
        )
        # 入藏档案仍在，不被作废清除。
        archived = self.connection.execute(
            "SELECT quantity FROM accession_records WHERE material_id=?", (specimen_id,)
        ).fetchall()
        self.assertEqual([row["quantity"] for row in archived], ["3"])

        def lines(specimen_accession: int) -> list[dict[str, object]]:
            # v1 已执行的归还/销毁/入藏都是既成事实，作废后仍计入基数；全部材料剩余为 0。
            result = []
            for row in self.connection.execute(
                "SELECT material_id,material_code FROM materials ORDER BY material_id"
            ):
                if row["material_code"] == "SPEC-1":
                    result.append({
                        "material_id": row["material_id"],
                        "accession_quantity": specimen_accession,
                        "returned_quantity": 0,
                        "destroyed_quantity": 0,
                        "reason": "重复入藏" if specimen_accession else "前期已闭环",
                    })
                else:
                    result.append({
                        "material_id": row["material_id"],
                        "accession_quantity": 0, "returned_quantity": 0,
                        "destroyed_quantity": 0, "reason": "前期已闭环",
                    })
            return result

        # specimen 已归档 3 件且已归还 1 件，剩余为 0，再次申报入藏被守恒拒绝。
        with self.assertRaises(ValidationFailed):
            self.service.submit_closure("teacher", "batch-a", self.participants, lines(3))
        # 正确的重新提交：全部剩余为 0。
        second = self.service.submit_closure("teacher", "batch-a", self.participants, lines(0))
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(self.connection.execute("SELECT * FROM accession_records").fetchall()), 1)

    def test_resubmit_requires_invalidation_after_confirmation(self) -> None:
        self._confirmed_closure()
        # 守恒合法的重新申报（全部材料前期均已闭环、剩余为 0）。
        zero_lines = [
            {"material_id": row["material_id"], "accession_quantity": 0,
             "returned_quantity": 0, "destroyed_quantity": 0, "reason": "前期已闭环"}
            for row in self.connection.execute(
                "SELECT material_id FROM materials ORDER BY material_id"
            )
        ]
        with self.assertRaises(InvalidState):
            self.service.submit_closure("teacher", "batch-a", self.participants, zero_lines)

    def test_partial_return_is_append_only_and_conserved(self) -> None:
        closure = self._confirmed_closure()
        slide = next(line for line in closure["materials"] if line["category"] == "temporary_slide")
        updated = self.service.partial_return(
            "museum", closure["closure_id"],
            [{"material_id": slide["material_id"], "quantity": 2, "reason": "学校索回两片"}],
            "部分退回",
        )
        self.assertEqual(updated["state"], "partially_returned")
        line = next(item for item in updated["materials"] if item["material_id"] == slide["material_id"])
        # 冻结值不变，有效值反映追加调整。
        self.assertEqual(line["frozen_destroyed_quantity"], "4")
        self.assertEqual(line["destroyed_quantity"], "2")
        self.assertEqual(line["returned_quantity"], "2")
        frozen = self.connection.execute(
            "SELECT returned_quantity,destroyed_quantity FROM closure_materials "
            "WHERE closure_id=? AND material_id=?",
            (closure["closure_id"], slide["material_id"]),
        ).fetchone()
        self.assertEqual((frozen["returned_quantity"], frozen["destroyed_quantity"]), ("0", "4"))
        # 守恒保持。
        report = self.service.conservation_report("auditor", "batch-a")
        slide_report = next(item for item in report["materials"] if item["material_code"] == "SLIDE-1")
        self.assertTrue(slide_report["conserved"])
        self.assertEqual(slide_report["returned_quantity"], "2")
        self.assertEqual(slide_report["destroyed_quantity"], "2")

    def test_partial_return_cannot_exceed_destroyed(self) -> None:
        closure = self._confirmed_closure()
        slide = next(line for line in closure["materials"] if line["category"] == "temporary_slide")
        with self.assertRaises(InvalidState):
            self.service.partial_return(
                "museum", closure["closure_id"],
                [{"material_id": slide["material_id"], "quantity": 5, "reason": "超量"}],
                "x",
            )

    def test_partial_return_respects_running_balance(self) -> None:
        closure = self._confirmed_closure()
        slide = next(line for line in closure["materials"] if line["category"] == "temporary_slide")
        self.service.partial_return(
            "museum", closure["closure_id"],
            [{"material_id": slide["material_id"], "quantity": 3, "reason": "第一次"}],
            "第一次退回",
        )
        # 只剩 1 件可改判。
        with self.assertRaises(InvalidState):
            self.service.partial_return(
                "museum", closure["closure_id"],
                [{"material_id": slide["material_id"], "quantity": 2, "reason": "第二次超量"}],
                "x",
            )

    def test_only_museum_can_partial_return(self) -> None:
        closure = self._confirmed_closure()
        slide = next(line for line in closure["materials"] if line["category"] == "temporary_slide")
        with self.assertRaises(Forbidden):
            self.service.partial_return(
                "teacher", closure["closure_id"],
                [{"material_id": slide["material_id"], "quantity": 1, "reason": "x"}], "x",
            )

    def test_material_trace_explains_why_and_conservation(self) -> None:
        closure = self._confirmed_closure()
        specimen = next(
            line for line in closure["materials"] if line["category"] == "accession_candidate"
        )
        trace = self.service.material_trace("auditor", specimen["material_id"])
        self.assertTrue(trace["conservation"]["conserved"])
        self.assertEqual(trace["conservation"]["initial_quantity"], "4")
        self.assertEqual(trace["conservation"]["accessioned_quantity"], "3")
        self.assertEqual(trace["conservation"]["returned_quantity"], "1")
        reasons = {(item["disposition"], item["quantity"]) for item in trace["closure_decisions"]}
        self.assertIn(("accession", "3"), reasons)
        self.assertIn(("return", "1"), reasons)
        self.assertEqual(len(trace["accession_records"]), 1)
        self.assertEqual(trace["accession_records"][0]["reason"], "三件入藏一件退回")

    def test_open_closure_counts_as_designated_in_conservation(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        self.service.submit_closure("teacher", "batch-a", self.participants, self._dispositions(ids))
        report = self.service.conservation_report("auditor", "batch-a")
        self.assertTrue(report["all_conserved"])
        self.assertTrue(all(item["conserved"] for item in report["materials"]))
        self.assertEqual(
            next(item for item in report["materials"] if item["material_code"] == "LIVE-1")[
                "open_closure_designated_quantity"
            ],
            "7",
        )

    def test_tampered_snapshot_fails_replay(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        closure = self.service.submit_closure(
            "teacher", "batch-a", self.participants, self._dispositions(ids)
        )
        # 直接篡改冻结快照文本，模拟底层被改动。
        snapshot = json.loads(
            self.connection.execute(
                "SELECT snapshot_json FROM closure_summaries WHERE closure_id=?",
                (closure["closure_id"],),
            ).fetchone()["snapshot_json"]
        )
        snapshot["materials"][0]["disposition_reason"] = "被篡改的原因"
        self.connection.execute(
            "UPDATE closure_summaries SET snapshot_json=? WHERE closure_id=?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), closure["closure_id"]),
        )
        with self.assertRaises(InvalidState):
            self.service.confirm_closure("teacher", closure["closure_id"], "instructor", "k")

    def test_unknown_participant_rejected(self) -> None:
        ids = self._register_materials()
        self._consume(ids)
        self._decide()
        with self.assertRaises(NotFound):
            self.service.submit_closure(
                "teacher", "batch-a", [{"user_id": "ghost", "role": "访客"}], self._dispositions(ids)
            )

    def test_invalid_invalidation_category_rejected(self) -> None:
        closure = self._confirmed_closure()
        with self.assertRaises(ValidationFailed):
            self.service.invalidate_closure("teacher", closure["closure_id"], "typo", "原因")


if __name__ == "__main__":
    unittest.main()
