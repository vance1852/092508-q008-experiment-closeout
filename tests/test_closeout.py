from __future__ import annotations

import json
import sqlite3
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from taxonomy_lab.jsonio import load_json
from taxonomy_lab.service import TaxonomyLabService

ROOT = Path(__file__).resolve().parents[1]


class CloseoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("instructor", "instructor"),
            ("curator", "curator"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        protocol = load_json(ROOT / "fixtures" / "demo_evidence_protocol.json")
        self.protocol = protocol
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_device("operator", "scope-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "scope-a", "1.0", "b" * 64)
        self.service.publish_evidence_protocol("stat", protocol)
        self.service.create_batch("operator", "batch-a", protocol["evidence_protocol_id"], 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.service.import_evidence_items("operator", "batch-a", "import-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 30)
        self.analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", self.analysis["analysis_id"], "approved", "采信通过")

    def tearDown(self) -> None:
        self.connection.close()

    def _balanced_materials(self) -> None:
        self.service.register_material_stock("operator", "batch-a", "live_specimen", "L1", 2)
        self.service.record_disposition("curator", "batch-a", "live_specimen", "L1", "return", 2, "活体归还学校")
        self.service.register_material_stock("operator", "batch-a", "temporary_slide", "S1", 2)
        self.service.record_disposition("curator", "batch-a", "temporary_slide", "S1", "destruction", 1, "一次性玻片废弃")
        self.service.record_disposition("curator", "batch-a", "temporary_slide", "S1", "accession", 1, "关键形态转正式标本")
        reagent = self.service.register_material_stock("operator", "batch-a", "reagent", "R1", 2)
        self.service.record_consumption("operator", "batch-a", reagent["stock_id"], 1)
        self.service.record_disposition("curator", "batch-a", "reagent", "R1", "destruction", 1, "剩余试剂销毁")
        self.service.register_material_stock("operator", "batch-a", "collectible_sample", "P1", 3)
        self.service.record_disposition("curator", "batch-a", "collectible_sample", "P1", "accession", 3, "符合入藏标准")

    def _submit_and_confirm(self) -> dict:
        self._balanced_materials()
        closeout = self.service.submit_closeout(
            "instructor", "batch-a", "close-1", ["operator", "instructor"], "联合实验结项"
        )
        self.service.confirm_closeout_instructor("instructor", closeout["closeout_id"])
        self.service.confirm_closeout_museum("curator", closeout["closeout_id"])
        return self.service.get_closeout(closeout["closeout_id"])

    # ---- 结项单内容与守恒 ----------------------------------------------

    def test_closeout_snapshots_protocol_participants_exclusions_and_conservation(self) -> None:
        closeout = self._submit_and_confirm()
        self.assertEqual(closeout["state"], "confirmed")
        self.assertEqual(closeout["serial"], 1)
        self.assertEqual(closeout["evidence_protocol"]["version"], 1)
        self.assertEqual({p["user_id"] for p in closeout["participants"]}, {"operator", "instructor"})
        self.assertEqual(closeout["decision"], "approved")
        self.assertEqual(len(closeout["content_sha256"]), 64)
        conservation = closeout["conservation"]
        self.assertTrue(conservation["conserved"])
        self.assertEqual(conservation["before"], conservation["after"])
        self.assertEqual(conservation["totals"]["initial"], 9)
        self.assertEqual(conservation["totals"]["accession"], 4)
        self.assertEqual(conservation["totals"]["return"], 2)
        self.assertEqual(conservation["totals"]["destruction"], 2)
        self.assertEqual(conservation["totals"]["consumed"], 1)

    def test_submit_rejects_unbalanced_materials(self) -> None:
        self.service.register_material_stock("operator", "batch-a", "live_specimen", "L1", 3)
        self.service.record_disposition("curator", "batch-a", "live_specimen", "L1", "return", 2, "部分归还")
        with self.assertRaisesRegex(InvalidState, "不守恒"):
            self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")

    def test_submit_requires_materials_registered(self) -> None:
        with self.assertRaisesRegex(InvalidState, "登记实验物料"):
            self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")

    def test_consumption_cannot_exceed_initial(self) -> None:
        reagent = self.service.register_material_stock("operator", "batch-a", "reagent", "R1", 1)
        with self.assertRaises(InvalidState):
            self.service.record_consumption("operator", "batch-a", reagent["stock_id"], 2)

    def test_material_type_restricts_outcomes(self) -> None:
        self.service.register_material_stock("operator", "batch-a", "reagent", "R1", 1)
        with self.assertRaisesRegex(ValidationFailed, "不允许的实物去向"):
            self.service.record_disposition("curator", "batch-a", "reagent", "R1", "return", 1, "试剂不能归还")

    def test_disposition_requires_reason(self) -> None:
        self.service.register_material_stock("operator", "batch-a", "live_specimen", "L1", 1)
        with self.assertRaises(ValidationFailed):
            self.service.record_disposition("curator", "batch-a", "live_specimen", "L1", "return", 1, "  ")

    # ---- 双方确认与状态 ------------------------------------------------

    def test_museum_cannot_confirm_before_instructor(self) -> None:
        self._balanced_materials()
        closeout = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        with self.assertRaisesRegex(InvalidState, "指导教师"):
            self.service.confirm_closeout_museum("curator", closeout["closeout_id"])

    def test_role_separation(self) -> None:
        self._balanced_materials()
        closeout = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        with self.assertRaises(Forbidden):
            self.service.confirm_closeout_instructor("curator", closeout["closeout_id"])
        with self.assertRaises(Forbidden):
            self.service.record_disposition(
                "operator", "batch-a", "live_specimen", "L1", "return", 1, "操作员不能登记去向"
            )

    def test_repeated_confirmation_replays_original_result(self) -> None:
        closeout = self._submit_and_confirm()
        replayed = self.service.confirm_closeout_museum("curator", closeout["closeout_id"])
        self.assertEqual(replayed["content_sha256"], closeout["content_sha256"])
        self.assertEqual(replayed["closeout_id"], closeout["closeout_id"])
        again = self.service.confirm_closeout_instructor("instructor", closeout["closeout_id"])
        self.assertEqual(again["state"], "confirmed")

    def test_replay_with_wrong_digest_is_rejected(self) -> None:
        closeout = self._submit_and_confirm()
        with self.assertRaises(Conflict):
            self.service.confirm_closeout_museum("curator", closeout["closeout_id"], "0" * 64)

    # ---- 撤回、退回、重新提交 ------------------------------------------

    def test_withdraw_keeps_reason_and_allows_resubmit(self) -> None:
        self._balanced_materials()
        first = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "初版")
        withdrawn = self.service.withdraw_closeout("instructor", first["closeout_id"], "参与者漏登")
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.assertEqual(withdrawn["return_reason"], "参与者漏登")
        second = self.service.submit_closeout(
            "instructor", "batch-a", "close-2", ["operator", "instructor"], "补登参与者"
        )
        self.assertEqual(second["serial"], 2)
        self.assertEqual(self.service.get_closeout(first["closeout_id"])["state"], "withdrawn")

    def test_submitter_can_withdraw(self) -> None:
        self._balanced_materials()
        closeout = self.service.submit_closeout("operator", "batch-a", "close-1", ["operator"], "结项")
        result = self.service.withdraw_closeout("operator", closeout["closeout_id"], "操作员自行撤回")
        self.assertEqual(result["state"], "withdrawn")

    def test_full_and_partial_return_have_distinct_states(self) -> None:
        self._balanced_materials()
        full = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        self.service.return_closeout(
            "instructor", full["closeout_id"], "物料原因缺失", returned_fields=["materials"]
        )
        self.assertEqual(self.service.get_closeout(full["closeout_id"])["state"], "returned")
        second = self.service.submit_closeout("instructor", "batch-a", "close-2", ["instructor"], "修订")
        self.service.confirm_closeout_instructor("instructor", second["closeout_id"])
        self.service.return_closeout(
            "curator", second["closeout_id"], "入藏数量需复核", partial=True, returned_fields=["materials"]
        )
        stored = self.service.get_closeout(second["closeout_id"])
        self.assertEqual(stored["state"], "partially_returned")
        self.assertEqual(stored["partially_returned_fields"], ["materials"])

    def test_instructor_cannot_partially_return_after_museum_confirmation_stage(self) -> None:
        self._balanced_materials()
        closeout = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        self.service.confirm_closeout_instructor("instructor", closeout["closeout_id"])
        with self.assertRaises(Forbidden):
            self.service.return_closeout(
                "instructor", closeout["closeout_id"], "无权", returned_fields=["materials"]
            )

    def test_idempotent_submit_replays_same_closeout(self) -> None:
        self._balanced_materials()
        first = self.service.submit_closeout("instructor", "batch-a", "dup", ["instructor"], "结项")
        second = self.service.submit_closeout("instructor", "batch-a", "dup", ["instructor"], "结项")
        self.assertEqual(first["closeout_id"], second["closeout_id"])
        same_key_different_body = Conflict
        with self.assertRaises(Conflict):
            self.service.submit_closeout("instructor", "batch-a", "dup", ["operator", "instructor"], "改了内容")

    # ---- 更正失效与归档不可变 ------------------------------------------

    def test_supersede_preserves_reason_reopens_batch_and_inherits_archived_materials(self) -> None:
        first = self._submit_and_confirm()
        batch = self.service.get_batch("batch-a")
        self.assertEqual(batch["state"], "closed")
        old_revision = batch["revision"]
        superseded = self.service.supersede_closeout("instructor", first["closeout_id"], "关键观测依据更正")
        self.assertEqual(superseded["state"], "superseded")
        self.assertEqual(superseded["superseded_reason"], "关键观测依据更正")
        self.assertEqual(superseded["superseded_by"], "instructor")
        reopened = self.service.get_batch("batch-a")
        self.assertEqual(reopened["state"], "running")
        self.assertEqual(reopened["revision"], old_revision + 1)

        # 关键观测更正：撤销此前批准的一条异常排除后重新封存分析。
        exclusion_id = self.connection.execute(
            "SELECT exclusion_id FROM exclusion_requests WHERE status='approved' LIMIT 1"
        ).fetchone()
        if exclusion_id is not None:
            self.service.revoke_exclusion("operator", exclusion_id[0], "原始记录复核有效")
        self.service.seal_batch("stat", "batch-a", reopened["revision"])
        job = self.service.claim_job("worker-2", 30)
        analysis = self.service.complete_job("worker-2", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "更正后采信")
        second = self.service.submit_closeout("instructor", "batch-a", "close-2", ["operator", "instructor"], "更正结项")
        self.assertEqual(second["serial"], 2)
        self.assertTrue(second["conservation"]["conserved"])
        # 新结项继承了旧结项已归档的全部实物去向。
        archived_total = sum(
            sum(item["archived_outcomes"].values()) for item in second["materials"]
        )
        self.assertEqual(archived_total, 8)
        self.service.confirm_closeout_instructor("instructor", second["closeout_id"])
        self.service.confirm_closeout_museum("curator", second["closeout_id"])
        # 旧归档行仍指向旧结项，没有被新结项覆盖。
        rows = self.connection.execute(
            "SELECT DISTINCT archived_in_closeout FROM material_dispositions"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [first["closeout_id"]])
        stored_first = self.service.get_closeout(first["closeout_id"])
        self.assertEqual(stored_first["state"], "superseded")

    def test_only_confirmed_closeout_can_be_superseded(self) -> None:
        self._balanced_materials()
        closeout = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        with self.assertRaises(InvalidState):
            self.service.supersede_closeout("instructor", closeout["closeout_id"], "未确认不能失效")

    def test_archived_dispositions_are_immutable_while_closeout_active(self) -> None:
        self._submit_and_confirm()
        with self.assertRaisesRegex(InvalidState, "冻结"):
            self.service.record_disposition(
                "curator", "batch-a", "collectible_sample", "P1", "accession", 1, "试图追加"
            )
        with self.assertRaisesRegex(InvalidState, "冻结"):
            reagent = self.connection.execute(
                "SELECT stock_id FROM material_stocks WHERE material_ref='R1'"
            ).fetchone()[0]
            self.service.record_consumption("operator", "batch-a", reagent, 1)

    def test_correct_protocol_version_after_reopen(self) -> None:
        first = self._submit_and_confirm()
        self.service.supersede_closeout("instructor", first["closeout_id"], "方案修订")
        version_two = deepcopy(self.protocol)
        version_two["version"] = 2
        version_two["title"] = f'{self.protocol["title"]}（修订版）'
        self.service.publish_evidence_protocol("stat", version_two)
        corrected = self.service.correct_batch_protocol("instructor", "batch-a", 2)
        self.assertEqual(corrected["evidence_protocol_version"], 2)
        with self.assertRaises(ValidationFailed):
            self.service.correct_batch_protocol("instructor", "batch-a", 2)

    def test_correct_protocol_forbidden_while_closed(self) -> None:
        self._submit_and_confirm()
        with self.assertRaises(InvalidState):
            self.service.correct_batch_protocol("instructor", "batch-a", 2)

    # ---- 报告 -----------------------------------------------------------

    def test_report_explains_why_each_material_was_accessioned_returned_or_destroyed(self) -> None:
        self._submit_and_confirm()
        report = self.service.closeout_report("auditor", "batch-a")
        self.assertEqual(report["current_closeout_id"], 1)
        self.assertTrue(report["conservation"]["conserved"])
        by_ref = {item["material_ref"]: item for item in report["materials"]}
        self.assertEqual(by_ref["L1"]["outcomes"], {"accession": 0, "return": 2, "destruction": 0})
        self.assertTrue(by_ref["L1"]["conserved"])
        reasons = {
            (d["outcome"], d["reason"])
            for item in report["materials"]
            for d in item["dispositions"]
        }
        self.assertIn(("return", "活体归还学校"), reasons)
        self.assertIn(("accession", "关键形态转正式标本"), reasons)
        self.assertIn(("destruction", "剩余试剂销毁"), reasons)
        for disposition in (d for item in report["materials"] for d in item["dispositions"]):
            self.assertTrue(disposition["immutable"])
            self.assertIsNotNone(disposition["archived_in_closeout"])

    def test_closeout_history_is_listed_with_states(self) -> None:
        self._balanced_materials()
        first = self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        self.service.withdraw_closeout("instructor", first["closeout_id"], "撤回")
        second = self.service.submit_closeout("instructor", "batch-a", "close-2", ["instructor"], "再提交")
        listing = self.service.list_closeouts("auditor", "batch-a")
        self.assertEqual([item["serial"] for item in listing["closeouts"]], [1, 2])
        self.assertEqual([item["state"] for item in listing["closeouts"]], ["withdrawn", "submitted"])

    def test_pending_exclusion_blocks_submit(self) -> None:
        self._balanced_materials()
        evidence_item_id = self.connection.execute(
            "SELECT evidence_item_id FROM evidence_items ORDER BY evidence_item_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", evidence_item_id, "结项前新发现异常")
        with self.assertRaisesRegex(InvalidState, "待复核的异常排除"):
            self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")
        self.service.review_exclusion("stat", requested["exclusion_id"], True, "批准但分析未重放")
        with self.assertRaisesRegex(InvalidState, "异常排除数量与分析结果不一致"):
            self.service.submit_closeout("instructor", "batch-a", "close-1", ["instructor"], "结项")


if __name__ == "__main__":
    unittest.main()
