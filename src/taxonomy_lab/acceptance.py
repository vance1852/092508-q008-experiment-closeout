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
            service.create_user("instructor-1", "指导教师", "instructor")
            service.create_user("curator-1", "馆方保藏负责人", "curator")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.register_device("operator-1", "scope-a", "A 型标本事件实验采集设备", "示例设备供应商")
            service.register_build("operator-1", "build-a1", "scope-a", "1.0.0", "a" * 64)
            service.publish_evidence_protocol("stat-1", evidence_protocol)
            service.create_batch("operator-1", "batch-demo", evidence_protocol["evidence_protocol_id"], evidence_protocol["version"], "build-a1")
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_evidence_items(
                "operator-1", "batch-demo", "demo-import-1", evidence_item_rows
            )
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            # 登记四类物料并给出与初始数量守恒的去向。
            service.register_material_stock("operator-1", "batch-demo", "live_specimen", "LIVE-1", 3)
            service.record_disposition(
                "curator-1", "batch-demo", "live_specimen", "LIVE-1", "return", 3, "观察后活体放生归还"
            )
            service.register_material_stock("operator-1", "batch-demo", "temporary_slide", "SLIDE-1", 4)
            service.record_disposition(
                "curator-1", "batch-demo", "temporary_slide", "SLIDE-1", "destruction", 2, "临时玻片废弃"
            )
            service.record_disposition(
                "curator-1", "batch-demo", "temporary_slide", "SLIDE-1", "accession", 2, "关键形态特征转正式玻片入藏"
            )
            reagent = service.register_material_stock("operator-1", "batch-demo", "reagent", "REAGENT-1", 2)
            service.record_consumption("operator-1", "batch-demo", reagent["stock_id"], 1)
            service.record_disposition(
                "curator-1", "batch-demo", "reagent", "REAGENT-1", "destruction", 1, "剩余试剂无害化销毁"
            )
            service.register_material_stock("operator-1", "batch-demo", "collectible_sample", "SAMPLE-1", 5)
            service.record_disposition(
                "curator-1", "batch-demo", "collectible_sample", "SAMPLE-1", "accession", 5, "符合入藏标准转正式标本"
            )
            closeout = service.submit_closeout(
                "instructor-1", "batch-demo", "demo-closeout-1",
                ["operator-1", "instructor-1"], "学校联合实验结项",
            )
            service.confirm_closeout_instructor("instructor-1", closeout["closeout_id"])
            service.confirm_closeout_museum("curator-1", closeout["closeout_id"])
            # 馆方重复确认必须回放原结项单。
            replayed = service.confirm_closeout_museum("curator-1", closeout["closeout_id"])
            if replayed["content_sha256"] != closeout["content_sha256"]:
                raise RuntimeError("重复确认未能回放原结项结果")
            report = service.report("auditor-1", "batch-demo")
            closeout_report = service.closeout_report("auditor-1", "batch-demo")
            if not closeout_report["conservation"]["conserved"]:
                raise RuntimeError("结项物料数量不守恒")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "evidence_protocol": f"{evidence_protocol['evidence_protocol_id']}@{evidence_protocol['version']}",
        "evidence_item_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "closeout_id": closeout["closeout_id"],
        "closeout_state": closeout_report["current_closeout_id"] and "confirmed",
        "material_conservation": closeout_report["conservation"],
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
