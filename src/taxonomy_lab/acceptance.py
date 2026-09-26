"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import TaxonomyLabService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    evidence_protocol = load_json(fixtures / "demo_evidence_protocol.json")
    evidence_item_rows = [
        json.loads(line)
        for line in (fixtures / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="device-reviews-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = TaxonomyLabService(connection)
            service.create_user("operator-1", "测试操作员", "operator")
            service.create_user("stat-1", "统计负责人", "statistician")
            service.create_user("approver-1", "观察材料采信审批人", "approver")
            service.create_user("instructor-1", "联合实验指导教师", "instructor")
            service.create_user("museum-1", "馆方接收负责人", "museum_officer")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.register_device("operator-1", "scope-a", "A 型标本事件实验采集设备", "示例设备供应商")
            service.register_build("operator-1", "build-a1", "scope-a", "1.0.0", "a" * 64)
            service.publish_evidence_protocol("stat-1", evidence_protocol)
            service.create_batch("operator-1", "batch-demo", evidence_protocol["evidence_protocol_id"], evidence_protocol["version"], "build-a1")
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_evidence_items(
                "operator-1", "batch-demo", "demo-import-1", evidence_item_rows
            )
            # 四类实验材料登记与样品消耗台账。
            materials = {
                "live": service.register_material(
                    "operator-1", "batch-demo", "LIVE-1", "live_observation", "活体观察对象", 10, "只"
                )["material_id"],
                "slide": service.register_material(
                    "operator-1", "batch-demo", "SLIDE-1", "temporary_slide", "临时玻片", 6, "片"
                )["material_id"],
                "reagent": service.register_material(
                    "operator-1", "batch-demo", "REAG-1", "residual_reagent", "剩余试剂", 500, "mL"
                )["material_id"],
                "specimen": service.register_material(
                    "operator-1", "batch-demo", "SPEC-1", "accession_candidate", "可入藏样品", 4, "件"
                )["material_id"],
            }
            service.record_consumption("operator-1", materials["live"], 3, "观察后现场放归")
            service.record_consumption("operator-1", materials["slide"], 2, "制片过程破碎")
            service.record_consumption("operator-1", materials["reagent"], 300, "实验过程消耗")
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            # 提交不可变结项单：方案版本、实际参与者、消耗、异常排除与去向汇总。
            participants = [
                {"user_id": "operator-1", "role": "操作员", "note": "现场观察与制片"},
                {"user_id": "stat-1", "role": "统计负责人"},
            ]
            disposition_lines = [
                {"material_id": materials["live"], "accession_quantity": 0,
                 "returned_quantity": 7, "destroyed_quantity": 0, "reason": "活体观察结束归还学校"},
                {"material_id": materials["slide"], "accession_quantity": 0,
                 "returned_quantity": 0, "destroyed_quantity": 4, "reason": "临时玻片到期销毁"},
                {"material_id": materials["reagent"], "accession_quantity": 0,
                 "returned_quantity": 0, "destroyed_quantity": 200, "reason": "废液按生物安全规定销毁"},
                {"material_id": materials["specimen"], "accession_quantity": 3,
                 "returned_quantity": 1, "destroyed_quantity": 0, "reason": "三件符合入藏标准，一件退回学校"},
            ]
            closure = service.submit_closure(
                "instructor-1", "batch-demo", participants, disposition_lines
            )
            instructor_confirmation = service.confirm_closure(
                "instructor-1", closure["closure_id"], "instructor", "demo-instructor-confirm"
            )
            # 重复确认回放原结果。
            replayed = service.confirm_closure(
                "instructor-1", closure["closure_id"], "instructor", "demo-instructor-confirm"
            )
            museum_confirmation = service.confirm_closure(
                "museum-1", closure["closure_id"], "museum", "demo-museum-confirm"
            )
            closure_detail = service.get_closure(closure["closure_id"])
            conservation = service.conservation_report("auditor-1", "batch-demo")
            specimen_trace = service.material_trace("auditor-1", materials["specimen"])
            report = service.report("auditor-1", "batch-demo")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not conservation["all_conserved"]:
        raise RuntimeError("结项前后数量不守恒")
    if not (instructor_confirmation["replay_matched"] and replayed["state"] == "instructor_confirmed"):
        raise RuntimeError("指导教师重复确认未能回放原结果")
    if len(specimen_trace["accession_records"]) != 1:
        raise RuntimeError("馆方确认后应恰好生成一条入藏档案")
    return {
        "status": "ok",
        "evidence_protocol": f"{evidence_protocol['evidence_protocol_id']}@{evidence_protocol['version']}",
        "evidence_item_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "closure": {
            "closure_id": closure_detail["closure_id"],
            "version": closure_detail["version"],
            "state": museum_confirmation["state"],
            "content_sha256": closure_detail["content_sha256"],
            "participant_count": len(closure_detail["participants"]),
            "material_count": len(closure_detail["materials"]),
            "confirmations": [
                item["party"] for item in closure_detail["confirmations"]
            ],
            "accession_catalog_codes": [
                item["catalog_code"] for item in specimen_trace["accession_records"]
            ],
        },
        "conservation_conserved": conservation["all_conserved"],
        "specimen_trace": {
            "why_accessioned": specimen_trace["accession_records"][0]["reason"],
            "conserved": specimen_trace["conservation"]["conserved"],
        },
        "event_count": len(report["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行校准数据基础工具的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
