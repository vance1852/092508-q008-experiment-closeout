"""分类实验观察采信服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .analysis import ALGORITHM_VERSION, analyze
from .clock import SystemClock, isoformat
from .closure import (
    INVALIDATION_CATEGORIES,
    MATERIAL_CATEGORIES,
    build_snapshot,
    parse_lines,
    parse_participants,
    quantity,
    require_text,
    snapshot_digest,
)
from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "evidence_item.import",
        "exclusion.request", "exclusion.revoke",
        "material.register", "material.consume",
    },
    "statistician": {"evidence_protocol.publish", "batch.seal", "exclusion.review", "analysis.run"},
    "approver": {"decision.write"},
    "instructor": {
        "closure.submit", "closure.withdraw", "closure.instructor_confirm", "closure.invalidate",
    },
    "museum_officer": {"closure.museum_confirm", "closure.partial_return"},
    "auditor": {"report.read", "audit.read"},
}

CLOSURE_OPEN_STATES = ("submitted", "instructor_confirmed")
ZERO = Decimal(0)


class TaxonomyLabService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_device(
        self, actor_id: str, device_id: str, model_name: str, vendor: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capture_devices(device_id,model_name,vendor,created_at) VALUES(?,?,?,?)",
                    (device_id, model_name, vendor, self._now()),
                )
                self._audit("device", device_id, "device.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"实验采集设备已存在: {device_id}") from exc
        return {"device_id": device_id, "model_name": model_name, "vendor": vendor}

    def register_build(
        self, actor_id: str, build_id: str, device_id: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("构建摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO builds(build_id,device_id,version,content_sha256,created_at) VALUES(?,?,?,?,?)",
                    (build_id, device_id, version, content_sha256.lower(), self._now()),
                )
                self._audit("build", build_id, "build.registered", actor_id, {"device_id": device_id, "version": version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("构建编号、版本或摘要冲突") from exc
        return {"build_id": build_id, "device_id": device_id, "version": version}

    def publish_evidence_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence_protocol.publish")
        try:
            evidence_protocol = EvidenceProtocol.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_protocol_catalog(evidence_protocol_id,version,title,task_family,canonical_json,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        evidence_protocol.evidence_protocol_id,
                        evidence_protocol.version,
                        evidence_protocol.title,
                        evidence_protocol.task_family,
                        text,
                        digest,
                        self._now(),
                    ),
                )
                identity = f"{evidence_protocol.evidence_protocol_id}@{evidence_protocol.version}"
                self._audit("evidence_protocol", identity, "evidence_protocol.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"evidence_protocol_id": evidence_protocol.evidence_protocol_id, "version": evidence_protocol.version, "sha256": digest}

    def _evidence_protocol(self, evidence_protocol_id: str, version: int) -> tuple[EvidenceProtocol, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM evidence_protocol_catalog WHERE evidence_protocol_id=? AND version=?",
            (evidence_protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return EvidenceProtocol.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    def create_batch(
        self,
        actor_id: str,
        batch_id: str,
        evidence_protocol_id: str,
        evidence_protocol_version: int,
        build_id: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "batch.create")
        self._evidence_protocol(evidence_protocol_id, evidence_protocol_version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO batches(batch_id,evidence_protocol_id,evidence_protocol_version,build_id,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, evidence_protocol_id, evidence_protocol_version, build_id, "draft", actor_id, self._now()),
                )
                self._audit("batch", batch_id, "batch.created", actor_id, {"build_id": build_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号冲突或构建不存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def start_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.start")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET state='running',revision=revision+1,started_at=? "
                "WHERE batch_id=? AND state='draft' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是当前草稿版本")
            self._audit("batch", batch_id, "batch.started", actor_id, {"from_revision": expected_revision})
        return self.get_batch(batch_id)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def import_evidence_items(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        raw_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence_item.import")
        rows = tuple(raw_rows)
        if not rows:
            raise ValidationFailed("观察记录数组不能为空")
        request_digest = content_digest(rows)
        scope = f"evidence_items:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "running":
            raise InvalidState("只有运行中的批次可以导入观察记录")
        evidence_protocol, _ = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        parsed: list[EvidenceItem] = []
        for raw in rows:
            try:
                item = EvidenceItem.from_dict(raw, evidence_protocol)
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            if item.device_id != self.connection.execute(
                "SELECT device_id FROM builds WHERE build_id=?", (batch["build_id"],)
            ).fetchone()["device_id"]:
                raise ValidationFailed("观察记录设备与批次登记不一致")
            parsed.append(item)
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        try:
            with transaction(self.connection, immediate=True):
                for item, raw in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO evidence_items(batch_id,source_batch,source_row,device_id,evidence_group_key,observed_at," 
                        "indicators_json,content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            item.source_batch,
                            item.source_row,
                            item.device_id,
                            item.evidence_group_key,
                            item.observed_at,
                            canonical_json({key: format(value, "f") for key, value in item.indicators.items()}),
                            content_digest([raw]),
                            actor_id,
                            self._now(),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("batch", batch_id, "evidence_items.imported", actor_id, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源行重复或幂等键并发冲突") from exc
        return response

    def request_exclusion(self, actor_id: str, evidence_item_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.request")
        evidence_item = self.connection.execute(
            "SELECT evidence_item_id,batch_id FROM evidence_items WHERE evidence_item_id=?", (evidence_item_id,)
        ).fetchone()
        if evidence_item is None:
            raise NotFound("观察记录不存在")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO exclusion_requests(evidence_item_id,status,reason,requested_by,requested_at) "
                    "VALUES(?,?,?,?,?)",
                    (evidence_item_id, "pending", reason, actor_id, self._now()),
                )
                exclusion_id = cursor.lastrowid
                self._audit("evidence_item", str(evidence_item_id), "exclusion.requested", actor_id, {"reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该观察记录已有待处理或生效排除") from exc
        return {"exclusion_id": exclusion_id, "status": "pending"}

    def review_exclusion(
        self, actor_id: str, exclusion_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "exclusion.review")
        row = self.connection.execute(
            "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("排除申请不存在")
        if row["status"] != "pending":
            raise InvalidState("排除申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的排除申请")
        status = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE exclusion_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE exclusion_id=? AND status='pending'",
                (status, actor_id, self._now(), note, exclusion_id),
            )
            self._audit("exclusion", str(exclusion_id), f"exclusion.{status}", actor_id, {"note": note})
        return {"exclusion_id": exclusion_id, "status": status}

    def revoke_exclusion(self, actor_id: str, exclusion_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.revoke")
        row = self.connection.execute(
            "SELECT e.*,o.batch_id FROM exclusion_requests e "
            "JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id WHERE e.exclusion_id=?",
            (exclusion_id,),
        ).fetchone()
        if row is None:
            raise NotFound("排除记录不存在")
        if row["status"] != "approved":
            raise InvalidState("只有已批准的排除可以撤销")
        if row["requested_by"] != actor_id:
            raise Forbidden("只有原申请人可以撤销排除")
        batch = self.get_batch(row["batch_id"])
        if batch["state"] != "running":
            raise InvalidState("批次封存后不能改变排除状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exclusion_requests SET status='revoked',review_note=?,reviewed_at=? "
                "WHERE exclusion_id=? AND status='approved'",
                (reason, self._now(), exclusion_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("排除状态已变化")
            self._audit(
                "evidence_item",
                str(row["evidence_item_id"]),
                "exclusion.revoked",
                actor_id,
                {"exclusion_id": exclusion_id, "reason": reason},
            )
        return {"exclusion_id": exclusion_id, "status": "revoked"}

    def seal_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.seal")
        with transaction(self.connection, immediate=True):
            pending = self.connection.execute(
                "SELECT count(*) FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
                "WHERE o.batch_id=? AND e.status='pending'", (batch_id,)
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有待复核的排除申请")
            cursor = self.connection.execute(
                "UPDATE batches SET state='sealed',revision=revision+1,sealed_at=? "
                "WHERE batch_id=? AND state='running' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次状态或版本已变化")
            new_revision = expected_revision + 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO analysis_jobs(batch_id,batch_revision,state,available_at,created_at,updated_at) "
                "VALUES(?,?, 'queued', ?,?,?)",
                (batch_id, new_revision, now, now, now),
            )
            self._audit("batch", batch_id, "batch.sealed", actor_id, {"revision": new_revision})
        return self.get_batch(batch_id)

    def claim_job(self, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT job_id FROM analysis_jobs WHERE "
                "(state='queued' AND available_at<=?) OR (state='leased' AND lease_expires_at<=?) "
                "ORDER BY available_at,job_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=? "
                "WHERE job_id=?",
                (worker_id, expires, now, row["job_id"]),
            )
            claimed = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return dict(claimed)

    def _analysis_evidence_items(self, batch_id: str, evidence_protocol: EvidenceProtocol) -> tuple[EvidenceItem, ...]:
        rows = self.connection.execute(
            "SELECT o.*,e.reason AS excluded_reason FROM evidence_items o "
            "LEFT JOIN exclusion_requests e ON e.evidence_item_id=o.evidence_item_id AND e.status='approved' "
            "WHERE o.batch_id=? ORDER BY o.evidence_item_id",
            (batch_id,),
        ).fetchall()
        items: list[EvidenceItem] = []
        for row in rows:
            indicators = json.loads(row["indicators_json"])
            items.append(EvidenceItem(
                source_batch=row["source_batch"],
                source_row=row["source_row"],
                device_id=row["device_id"],
                evidence_protocol_id=evidence_protocol.evidence_protocol_id,
                evidence_protocol_version=evidence_protocol.version,
                evidence_group_key=row["evidence_group_key"],
                observed_at=row["observed_at"],
                indicators={key: Decimal(str(value)) for key, value in indicators.items()},
                excluded_reason=row["excluded_reason"],
            ))
        return tuple(items)

    def complete_job(self, worker_id: str, job_id: int, statistician_id: str) -> dict[str, Any]:
        self._require(statistician_id, "analysis.run")
        job = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise NotFound("分析任务不存在")
        if job["state"] != "leased" or job["lease_owner"] != worker_id:
            raise InvalidState("任务未由当前工作进程持有")
        if job["lease_expires_at"] <= self._now():
            raise InvalidState("任务租约已经过期")
        batch = self.get_batch(job["batch_id"])
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        evidence_items = self._analysis_evidence_items(batch["batch_id"], evidence_protocol)
        snapshot_rows = [
            {
                "source_batch": item.source_batch,
                "source_row": item.source_row,
                "evidence_group": item.evidence_group_key,
                "indicators": {key: format(value, "f") for key, value in item.indicators.items()},
                "excluded_reason": item.excluded_reason,
            }
            for item in evidence_items
        ]
        input_digest = content_digest(snapshot_rows)
        result = analyze(evidence_protocol, evidence_items)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT analysis_id,result_json FROM analyses WHERE batch_id=? AND batch_revision=? AND input_sha256=?",
                (batch["batch_id"], job["batch_revision"], input_digest),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO analyses(batch_id,batch_revision,evidence_protocol_sha256,input_sha256,algorithm_version,seed," 
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch["batch_id"], job["batch_revision"], evidence_protocol_digest, input_digest,
                        ALGORITHM_VERSION, evidence_protocol.seed, canonical_json(result), statistician_id, self._now(),
                    ),
                )
                analysis_id = cursor.lastrowid
            else:
                analysis_id = existing["analysis_id"]
                result = json.loads(existing["result_json"])
            self.connection.execute(
                "UPDATE analysis_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE job_id=? AND state='leased' AND lease_owner=?",
                (self._now(), job_id, worker_id),
            )
            self.connection.execute(
                "UPDATE batches SET state='analyzed' WHERE batch_id=? AND state IN ('sealed','analyzing')",
                (batch["batch_id"],),
            )
            self._audit(
                "batch",
                batch["batch_id"],
                "analysis.completed",
                statistician_id,
                {"analysis_id": analysis_id, "input_sha256": input_digest},
            )
        return {"analysis_id": analysis_id, "input_sha256": input_digest, "result": result}

    def fail_job(self, worker_id: str, job_id: int, error: str, retry_seconds: int = 0) -> dict[str, Any]:
        available = isoformat(self.clock.now() + timedelta(seconds=retry_seconds))
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL," 
                "last_error=?,updated_at=? WHERE job_id=? AND state='leased' AND lease_owner=?",
                (available, error[:1000], self._now(), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("任务未由当前工作进程持有")
        return {"job_id": job_id, "state": "queued", "available_at": available}

    def decide(
        self, actor_id: str, batch_id: str, analysis_id: int, decision: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if decision not in {"needs_more_data", "approved", "rejected"}:
            raise ValidationFailed("未知观察材料采信决定")
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=? AND batch_id=?", (analysis_id, batch_id)
        ).fetchone()
        if analysis_row is None:
            raise NotFound("分析版本不存在")
        if analysis_row["created_by"] == actor_id:
            raise Forbidden("统计负责人不能批准自己的分析")
        batch = self.get_batch(batch_id)
        if batch["state"] != "analyzed" or batch["revision"] != analysis_row["batch_revision"]:
            raise InvalidState("分析不是批次当前可审批版本")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO decisions(batch_id,analysis_id,decision,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, analysis_id, decision, reason, actor_id, self._now()),
                )
                self.connection.execute("UPDATE batches SET state='decided' WHERE batch_id=?", (batch_id,))
                self._audit(
                    "batch",
                    batch_id,
                    "decision.recorded",
                    actor_id,
                    {"decision_id": cursor.lastrowid, "analysis_id": analysis_id, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该分析版本已经形成决定") from exc
        return {"batch_id": batch_id, "analysis_id": analysis_id, "decision": decision}

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
            raise Forbidden("当前角色不能读取完整报告")
        batch = self.get_batch(batch_id)
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        decision_row = None
        if analysis_row is not None:
            decision_row = self.connection.execute(
                "SELECT * FROM decisions WHERE analysis_id=?", (analysis_row["analysis_id"],)
            ).fetchone()
        exclusions = self.connection.execute(
            "SELECT e.exclusion_id,e.evidence_item_id,e.status,e.reason,e.requested_by,e.reviewed_by "
            "FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
            "WHERE o.batch_id=? ORDER BY e.exclusion_id", (batch_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? "
            "ORDER BY event_id", (batch_id,)
        ).fetchall()
        return {
            "batch": batch,
            "evidence_protocol": {
                "evidence_protocol_id": evidence_protocol.evidence_protocol_id,
                "version": evidence_protocol.version,
                "sha256": evidence_protocol_digest,
                "seed": evidence_protocol.seed,
                "bootstrap_samples": evidence_protocol.bootstrap_samples,
            },
            "analysis": None if analysis_row is None else {
                "analysis_id": analysis_row["analysis_id"],
                "input_sha256": analysis_row["input_sha256"],
                "algorithm_version": analysis_row["algorithm_version"],
                "created_by": analysis_row["created_by"],
                "result": json.loads(analysis_row["result_json"]),
            },
            "decision": None if decision_row is None else dict(decision_row),
            "exclusions": [dict(row) for row in exclusions],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }

    # ------------------------------------------------------------------
    # 结项流程：材料台账
    # ------------------------------------------------------------------

    def register_material(
        self,
        actor_id: str,
        batch_id: str,
        material_code: str,
        category: str,
        description: str,
        initial_quantity: object,
        unit: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "material.register")
        if category not in MATERIAL_CATEGORIES:
            raise ValidationFailed(f"未知材料类别: {category}")
        initial = quantity(initial_quantity, "initial_quantity")
        if initial <= 0:
            raise ValidationFailed("initial_quantity 必须大于零")
        code = require_text(material_code, "material_code")
        text = require_text(description, "description")
        unit_text = require_text(unit, "unit")
        batch = self.get_batch(batch_id)
        if batch["state"] not in {"draft", "running"}:
            raise InvalidState("批次封存后不能再登记新材料")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO materials(batch_id,material_code,category,description,initial_quantity,unit,"
                    "registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?)",
                    (batch_id, code, category, text, format(initial, "f"), unit_text, actor_id, self._now()),
                )
                material_id = cursor.lastrowid
                self._audit("material", str(material_id), "material.registered", actor_id, {
                    "batch_id": batch_id, "material_code": code, "category": category,
                    "initial_quantity": format(initial, "f"), "unit": unit_text,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("材料编号在批次内重复或批次不存在") from exc
        return {
            "material_id": material_id, "batch_id": batch_id, "material_code": code,
            "category": category, "initial_quantity": format(initial, "f"), "unit": unit_text,
        }

    def record_consumption(
        self, actor_id: str, material_id: int, consumed_quantity: object, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "material.consume")
        amount = quantity(consumed_quantity, "consumed_quantity")
        if amount <= 0:
            raise ValidationFailed("consumed_quantity 必须大于零")
        reason_text = require_text(reason, "reason")
        material = self.connection.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)
        ).fetchone()
        if material is None:
            raise NotFound("材料不存在")
        batch = self.get_batch(material["batch_id"])
        if batch["state"] != "running":
            raise InvalidState("只有运行中的批次可以登记样品消耗")
        already = self._decimal_sum(
            "SELECT quantity FROM material_consumptions WHERE material_id=?",
            (material_id,),
        )
        if already + amount > Decimal(material["initial_quantity"]):
            raise InvalidState(
                f"累计消耗 {already + amount} 超过登记数量 {material['initial_quantity']}"
            )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO material_consumptions(material_id,quantity,reason,consumed_by,consumed_at) "
                "VALUES(?,?,?,?,?)",
                (material_id, format(amount, "f"), reason_text, actor_id, self._now()),
            )
            self._audit("material", str(material_id), "material.consumed", actor_id, {
                "consumption_id": cursor.lastrowid, "quantity": format(amount, "f"), "reason": reason_text,
            })
        return {
            "material_id": material_id, "consumed_quantity": format(amount, "f"),
            "consumed_total": format(already + amount, "f"),
        }

    def _decimal_sum(self, sql: str, params: tuple[Any, ...] = ()) -> Decimal:
        """取文本型数量列，在 Python 侧按 Decimal 求和，避免 SQLite 浮点强转。"""

        total = ZERO
        for (value,) in self.connection.execute(sql, params).fetchall():
            if value is not None:
                total += Decimal(str(value))
        return total

    def _materials(self, batch_id: str) -> dict[int, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM materials WHERE batch_id=? ORDER BY material_id", (batch_id,)
        ).fetchall()
        return {row["material_id"]: row for row in rows}

    def _material_balances(self, batch_id: str) -> dict[int, dict[str, Decimal]]:
        """汇总结项申报基数：登记数、累计消耗、历史已执行去向。

        入藏量取自已冻结的入藏台账（accession_records），即使旧结项失效也保留；
        归还/销毁量取自馆方已执行确认的结项明细，保证重新提交时数量仍守恒。
        """

        balances: dict[int, dict[str, Decimal]] = {}
        for material_id, material in self._materials(batch_id).items():
            consumed = self._decimal_sum(
                "SELECT quantity FROM material_consumptions WHERE material_id=?",
                (material_id,),
            )
            accessioned_prior = self._decimal_sum(
                "SELECT a.quantity FROM accession_records a WHERE a.material_id=?",
                (material_id,),
            )
            returned_prior = self._decimal_sum(
                "SELECT m.returned_quantity FROM closure_materials m "
                "JOIN closure_summaries c ON c.closure_id=m.closure_id "
                "WHERE c.museum_confirmed_at IS NOT NULL AND m.material_id=?",
                (material_id,),
            )
            destroyed_prior = self._decimal_sum(
                "SELECT m.destroyed_quantity FROM closure_materials m "
                "JOIN closure_summaries c ON c.closure_id=m.closure_id "
                "WHERE c.museum_confirmed_at IS NOT NULL AND m.material_id=?",
                (material_id,),
            )
            adjustment_rows = self.connection.execute(
                "SELECT a.field,a.old_value,a.new_value FROM closure_adjustments a "
                "JOIN closure_summaries c ON c.closure_id=a.closure_id "
                "WHERE c.museum_confirmed_at IS NOT NULL AND a.material_id=?",
                (material_id,),
            ).fetchall()
            for item in adjustment_rows:
                change = Decimal(item["new_value"]) - Decimal(item["old_value"])
                if item["field"] == "returned_quantity":
                    returned_prior += change
                elif item["field"] == "destroyed_quantity":
                    destroyed_prior += change
            balances[material_id] = {
                "initial": Decimal(material["initial_quantity"]),
                "consumed": consumed,
                "accessioned_prior": accessioned_prior,
                "returned_prior": returned_prior,
                "destroyed_prior": destroyed_prior,
            }
        return balances

    # ------------------------------------------------------------------
    # 结项流程：结项单提交与版本链
    # ------------------------------------------------------------------

    def _current_analysis_and_decision(self, batch_id: str) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        batch = self.get_batch(batch_id)
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? AND batch_revision=? ORDER BY analysis_id DESC LIMIT 1",
            (batch_id, batch["revision"]),
        ).fetchone()
        decision_row = None
        if analysis_row is not None:
            decision_row = self.connection.execute(
                "SELECT * FROM decisions WHERE analysis_id=? ORDER BY decision_id LIMIT 1",
                (analysis_row["analysis_id"],),
            ).fetchone()
        return analysis_row, decision_row

    def submit_closure(
        self,
        actor_id: str,
        batch_id: str,
        participants_raw: Iterable[Mapping[str, Any]],
        materials_raw: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "closure.submit")
        batch = self.get_batch(batch_id)
        if batch["state"] != "decided":
            raise InvalidState("只有已形成采信决定的批次可以提交结项")
        open_row = self.connection.execute(
            "SELECT closure_id,version,state FROM closure_summaries "
            "WHERE batch_id=? AND state IN ('submitted','instructor_confirmed')",
            (batch_id,),
        ).fetchone()
        if open_row is not None:
            raise InvalidState(f"已有未闭环结项单 v{open_row['version']}，请先撤回或完成确认")
        latest_row = self.connection.execute(
            "SELECT version,state,museum_confirmed_at FROM closure_summaries "
            "WHERE batch_id=? ORDER BY version DESC LIMIT 1", (batch_id,)
        ).fetchone()
        try:
            participants = parse_participants(list(participants_raw))
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        materials = self._materials(batch_id)
        if not materials:
            raise ValidationFailed("批次尚未登记任何材料，无法结项")
        balances = self._material_balances(batch_id)
        try:
            lines = parse_lines(list(materials_raw), balances, materials)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        for participant in participants:
            self._user(participant.user_id)
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(
            batch["evidence_protocol_id"], batch["evidence_protocol_version"]
        )
        analysis_row, decision_row = self._current_analysis_and_decision(batch_id)
        exclusions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT e.exclusion_id,e.evidence_item_id,e.status,e.reason,e.requested_by,e.reviewed_by "
                "FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
                "WHERE o.batch_id=? ORDER BY e.exclusion_id", (batch_id,)
            ).fetchall()
        ]
        snapshot = build_snapshot(
            batch=dict(batch),
            evidence_protocol={
                "evidence_protocol_id": evidence_protocol.evidence_protocol_id,
                "version": evidence_protocol.version,
                "content_sha256": evidence_protocol_digest,
            },
            analysis=None if analysis_row is None else dict(analysis_row),
            decision=None if decision_row is None else dict(decision_row),
            participants=participants,
            lines=lines,
            materials={key: dict(value) for key, value in materials.items()},
            exclusions=exclusions,
        )
        digest_of_snapshot = snapshot_digest(snapshot)
        participants_digest = content_digest([item.as_dict() for item in participants])
        try:
            with transaction(self.connection, immediate=True):
                locked_open = self.connection.execute(
                    "SELECT closure_id,version FROM closure_summaries "
                    "WHERE batch_id=? AND state IN ('submitted','instructor_confirmed')",
                    (batch_id,),
                ).fetchone()
                if locked_open is not None:
                    raise InvalidState(
                        f"已有未闭环结项单 v{locked_open['version']}，请先撤回或完成确认"
                    )
                if latest_row is not None and latest_row["state"] in {"confirmed", "partially_returned"}:
                    raise InvalidState(
                        "上一版结项已经双方确认；方案或关键观测更正时须先声明作废并保留原因，才能重新提交"
                    )
                version_row = self.connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS v FROM closure_summaries WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                version = version_row["v"] + 1
                closure_digest = content_digest([{
                    "version": version,
                    "snapshot_digest": digest_of_snapshot,
                    "participants_digest": participants_digest,
                }])
                cursor = self.connection.execute(
                    "INSERT INTO closure_summaries(batch_id,version,state,evidence_protocol_sha256,analysis_id,"
                    "analysis_input_sha256,participants_json,participants_digest,snapshot_json,snapshot_digest,"
                    "content_sha256,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id, version, "submitted", evidence_protocol_digest,
                        None if analysis_row is None else analysis_row["analysis_id"],
                        None if analysis_row is None else analysis_row["input_sha256"],
                        canonical_json([item.as_dict() for item in participants]),
                        participants_digest, canonical_json(snapshot), digest_of_snapshot,
                        closure_digest, actor_id, self._now(),
                    ),
                )
                closure_id = cursor.lastrowid
                for line in lines:
                    material = materials[line.material_id]
                    self.connection.execute(
                        "INSERT INTO closure_materials(closure_id,material_id,category,initial_quantity,"
                        "consumed_quantity,accession_quantity,returned_quantity,destroyed_quantity,unit,"
                        "disposition,disposition_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            closure_id, line.material_id, material["category"], format(line.initial, "f"),
                            format(line.consumed_before, "f"), format(line.accession, "f"),
                            format(line.returned, "f"), format(line.destroyed, "f"), material["unit"],
                            line.disposition, line.reason,
                        ),
                    )
                self._audit("closure", str(closure_id), "closure.submitted", actor_id, {
                    "batch_id": batch_id, "version": version, "content_sha256": closure_digest,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("结项单版本冲突") from exc
        return self.get_closure(closure_id)

    def _closure(self, closure_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM closure_summaries WHERE closure_id=?", (closure_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结项单不存在")
        return row

    def get_closure(self, closure_id: int, actor_id: str | None = None) -> dict[str, Any]:
        if actor_id is not None:
            self._user(actor_id)
        row = self._closure(closure_id)
        lines = self.connection.execute(
            "SELECT cm.*,m.material_code,m.description FROM closure_materials cm "
            "JOIN materials m ON m.material_id=cm.material_id "
            "WHERE cm.closure_id=? ORDER BY cm.material_id", (closure_id,)
        ).fetchall()
        effective_lines: list[dict[str, Any]] = []
        for item in lines:
            line = dict(item)
            delta = self._adjustment_delta(closure_id, item["material_id"])
            line["frozen_accession_quantity"] = line["accession_quantity"]
            line["frozen_returned_quantity"] = line["returned_quantity"]
            line["frozen_destroyed_quantity"] = line["destroyed_quantity"]
            effective = {
                "accession_quantity": Decimal(line["accession_quantity"]) + delta["accession_quantity"],
                "returned_quantity": Decimal(line["returned_quantity"]) + delta["returned_quantity"],
                "destroyed_quantity": Decimal(line["destroyed_quantity"]) + delta["destroyed_quantity"],
            }
            kinds = [
                label
                for value, label in (
                    (effective["accession_quantity"], "accession"),
                    (effective["returned_quantity"], "return"),
                    (effective["destroyed_quantity"], "destroy"),
                )
                if value > 0
            ]
            line["accession_quantity"] = format(effective["accession_quantity"], "f")
            line["returned_quantity"] = format(effective["returned_quantity"], "f")
            line["destroyed_quantity"] = format(effective["destroyed_quantity"], "f")
            line["disposition"] = (
                "consumed" if not kinds else (kinds[0] if len(kinds) == 1 else "mixed")
            )
            effective_lines.append(line)
        confirmations = self.connection.execute(
            "SELECT party,actor_id,content_sha256,replay_matched,confirmed_at "
            "FROM closure_confirmations WHERE closure_id=? ORDER BY confirmation_id", (closure_id,)
        ).fetchall()
        adjustments = self.connection.execute(
            "SELECT material_id,field,old_value,new_value,reason,adjusted_by,adjusted_at "
            "FROM closure_adjustments WHERE closure_id=? ORDER BY adjustment_id", (closure_id,)
        ).fetchall()
        result = dict(row)
        result["snapshot"] = json.loads(row["snapshot_json"])
        result["participants"] = json.loads(row["participants_json"])
        result["materials"] = effective_lines
        result["confirmations"] = [dict(item) for item in confirmations]
        result["adjustments"] = [dict(item) for item in adjustments]
        return result

    def list_closures(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._user(actor_id)
        self.get_batch(batch_id)
        rows = self.connection.execute(
            "SELECT closure_id,version,state,content_sha256,submitted_by,submitted_at,"
            "instructor_confirmed_at,museum_confirmed_at,withdrawn_at,withdrawn_reason,"
            "invalidated_at,invalidation_reason,invalidation_category "
            "FROM closure_summaries WHERE batch_id=? ORDER BY version", (batch_id,)
        ).fetchall()
        return {"batch_id": batch_id, "closures": [dict(row) for row in rows]}

    def withdraw_closure(self, actor_id: str, closure_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "closure.withdraw")
        reason_text = require_text(reason, "reason")
        row = self._closure(closure_id)
        if row["submitted_by"] != actor_id:
            raise Forbidden("只有结项提交人可以撤回")
        if row["state"] not in CLOSURE_OPEN_STATES:
            raise InvalidState("只有待确认的结项单可以撤回")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE closure_summaries SET state='withdrawn',withdrawn_by=?,withdrawn_at=?,withdrawn_reason=? "
                "WHERE closure_id=? AND state IN ('submitted','instructor_confirmed')",
                (actor_id, self._now(), reason_text, closure_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            self._audit("closure", str(closure_id), "closure.withdrawn", actor_id, {"reason": reason_text})
        return self.get_closure(closure_id)

    def invalidate_closure(
        self, actor_id: str, closure_id: int, category: str, reason: str
    ) -> dict[str, Any]:
        """方案版本或关键观测更正时，将旧结项单作废并保留原因。"""

        self._require(actor_id, "closure.invalidate")
        if category not in INVALIDATION_CATEGORIES:
            raise ValidationFailed(
                f"作废类别必须是 {INVALIDATION_CATEGORIES} 之一"
            )
        reason_text = require_text(reason, "reason")
        row = self._closure(closure_id)
        if row["state"] in {"withdrawn", "invalidated"}:
            raise InvalidState("结项单已经终止，不能重复作废")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE closure_summaries SET state='invalidated',invalidated_by=?,invalidated_at=?,"
                "invalidation_reason=?,invalidation_category=? WHERE closure_id=?",
                (actor_id, self._now(), reason_text, category, closure_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            self._audit("closure", str(closure_id), "closure.invalidated", actor_id, {
                "category": category, "reason": reason_text,
                "museum_executed": row["museum_confirmed_at"] is not None,
            })
        return self.get_closure(closure_id)

    # ------------------------------------------------------------------
    # 结项流程：双边确认与回放
    # ------------------------------------------------------------------

    def _replay_closure(self, row: sqlite3.Row) -> None:
        """重新计算已冻结快照摘要，核对结项单内容未被篡改。"""

        snapshot = json.loads(row["snapshot_json"])
        replay = snapshot_digest(snapshot)
        if replay != row["snapshot_digest"]:
            raise InvalidState("回放失败：结项快照摘要与冻结时不一致")
        expected = content_digest([{
            "version": row["version"],
            "snapshot_digest": replay,
            "participants_digest": row["participants_digest"],
        }])
        if expected != row["content_sha256"]:
            raise InvalidState("回放失败：结项单内容摘要与冻结时不一致")
        for line in snapshot["materials"]:
            initial = Decimal(line["initial_quantity"])
            consumed = Decimal(line["consumed_quantity"])
            prior = (
                Decimal(line["accessioned_prior_quantity"])
                + Decimal(line["returned_prior_quantity"])
                + Decimal(line["destroyed_prior_quantity"])
            )
            disposed = (
                Decimal(line["accession_quantity"])
                + Decimal(line["returned_quantity"])
                + Decimal(line["destroyed_quantity"])
            )
            if initial - consumed - prior != disposed:
                raise InvalidState("回放失败：结项快照数量不守恒")

    def confirm_closure(
        self,
        actor_id: str,
        closure_id: int,
        party: str,
        idempotency_key: str,
        replay_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        permission = {
            "instructor": "closure.instructor_confirm",
            "museum": "closure.museum_confirm",
        }.get(party)
        if permission is None:
            raise ValidationFailed("确认方必须是 instructor 或 museum")
        self._require(actor_id, permission)
        if not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        row = self._closure(closure_id)
        scope = f"closure_confirm:{closure_id}:{party}"
        request_digest = content_digest([{
            "closure_id": closure_id,
            "party": party,
            "content_sha256": row["content_sha256"],
            "replay": replay_payload or {},
        }])
        # 任何确认（含幂等回放）都必须先重放冻结快照与守恒关系。
        self._replay_closure(row)
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        prior = self.connection.execute(
            "SELECT * FROM closure_confirmations WHERE closure_id=? AND party=?",
            (closure_id, party),
        ).fetchone()
        if prior is not None:
            raise Conflict("该方已经确认；重复确认必须使用原幂等键回放原结果")
        if party == "instructor":
            if row["state"] != "submitted":
                raise InvalidState("只有已提交待指导教师确认的结项单可以确认")
            target_state = "instructor_confirmed"
        else:
            if row["state"] != "instructor_confirmed":
                raise InvalidState("结项单尚未经指导教师确认")
            target_state = "confirmed"
        response: dict[str, Any] = {
            "closure_id": closure_id, "party": party, "state": target_state,
            "replay_matched": True, "content_sha256": row["content_sha256"],
        }
        with transaction(self.connection, immediate=True):
            reconfirm = self.connection.execute(
                "SELECT * FROM closure_summaries WHERE closure_id=? AND state=?",
                (closure_id, row["state"]),
            ).fetchone()
            if reconfirm is None:
                raise InvalidState("结项单状态已变化")
            self.connection.execute(
                "INSERT INTO closure_confirmations(closure_id,party,actor_id,content_sha256,"
                "replay_matched,confirmed_at) VALUES(?,?,?,?,1,?)",
                (closure_id, party, actor_id, row["content_sha256"], self._now()),
            )
            if party == "instructor":
                self.connection.execute(
                    "UPDATE closure_summaries SET state='instructor_confirmed',"
                    "instructor_confirmed_by=?,instructor_confirmed_at=? WHERE closure_id=? AND state='submitted'",
                    (actor_id, self._now(), closure_id),
                )
            else:
                self.connection.execute(
                    "UPDATE closure_summaries SET state='confirmed',museum_confirmed_by=?,"
                    "museum_confirmed_at=? WHERE closure_id=? AND state='instructor_confirmed'",
                    (actor_id, self._now(), closure_id),
                )
                # 馆方确认即冻结入藏档案：只追加、永不覆盖。
                lines = self.connection.execute(
                    "SELECT * FROM closure_materials WHERE closure_id=? AND accession_quantity!='0'",
                    (closure_id,),
                ).fetchall()
                for line in lines:
                    self.connection.execute(
                        "INSERT INTO accession_records(material_id,closure_id,batch_id,quantity,unit,"
                        "catalog_code,reason,accessioned_by,accessioned_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            line["material_id"], closure_id, row["batch_id"],
                            line["accession_quantity"], line["unit"],
                            f"{row['batch_id']}-M{line['material_id']}-C{closure_id}",
                            line["disposition_reason"], actor_id, self._now(),
                        ),
                    )
            self.connection.execute(
                "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
            )
            self._audit("closure", str(closure_id), f"closure.{party}_confirmed", actor_id, {
                "replay_matched": True, "content_sha256": row["content_sha256"],
            })
        return response

    def partial_return(
        self,
        actor_id: str,
        closure_id: int,
        changes: Iterable[Mapping[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        """馆方把已确认结项中部分原定销毁的实物改为退回，只追加调整记录。

        已冻结的结项明细与入藏档案均不被改写；销毁 -> 退回的等量改判记入
        closure_adjustments，守恒查询与去向溯源在读取时叠加调整量。
        """

        self._require(actor_id, "closure.partial_return")
        reason_text = require_text(reason, "reason")
        row = self._closure(closure_id)
        if row["museum_confirmed_at"] is None:
            raise InvalidState("馆方尚未确认的结项单不能办理部分退回")
        if row["state"] not in {"confirmed", "partially_returned"}:
            raise InvalidState("结项单当前状态不允许部分退回")
        change_rows = tuple(changes)
        if not change_rows:
            raise ValidationFailed("changes 不能为空")
        with transaction(self.connection, immediate=True):
            for index, raw_change in enumerate(change_rows):
                path = f"changes[{index}]"
                if not isinstance(raw_change, Mapping):
                    raise ValidationFailed(f"{path} 必须是对象")
                material_id = raw_change.get("material_id")
                if isinstance(material_id, bool) or not isinstance(material_id, int):
                    raise ValidationFailed(f"{path}.material_id 必须是整数")
                delta = quantity(raw_change.get("quantity"), f"{path}.quantity")
                if delta <= 0:
                    raise ValidationFailed(f"{path}.quantity 必须大于零")
                change_reason = require_text(raw_change.get("reason", reason), f"{path}.reason")
                line = self.connection.execute(
                    "SELECT * FROM closure_materials WHERE closure_id=? AND material_id=?",
                    (closure_id, material_id),
                ).fetchone()
                if line is None:
                    raise NotFound(f"{path}.material_id 不在该结项单中")
                already = self._adjustment_delta(closure_id, material_id)
                available = Decimal(line["destroyed_quantity"]) + already["destroyed_quantity"]
                if available < delta:
                    raise InvalidState(
                        f"{path} 退回数量 {delta} 超过可改判的销毁数量 {available}"
                    )
                destroyed_before = Decimal(line["destroyed_quantity"]) + already["destroyed_quantity"]
                returned_before = Decimal(line["returned_quantity"]) + already["returned_quantity"]
                self.connection.execute(
                    "INSERT INTO closure_adjustments(closure_id,material_id,field,old_value,new_value,"
                    "reason,adjusted_by,adjusted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        closure_id, material_id, "destroyed_quantity",
                        format(destroyed_before, "f"), format(destroyed_before - delta, "f"),
                        change_reason, actor_id, self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO closure_adjustments(closure_id,material_id,field,old_value,new_value,"
                    "reason,adjusted_by,adjusted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        closure_id, material_id, "returned_quantity",
                        format(returned_before, "f"), format(returned_before + delta, "f"),
                        change_reason, actor_id, self._now(),
                    ),
                )
                self._audit("material", str(material_id), "closure.partial_return", actor_id, {
                    "closure_id": closure_id, "quantity": format(delta, "f"), "reason": change_reason,
                })
            self.connection.execute(
                "UPDATE closure_summaries SET state='partially_returned',returned_at=? WHERE closure_id=?",
                (self._now(), closure_id),
            )
            self._audit("closure", str(closure_id), "closure.partially_returned", actor_id, {"reason": reason_text})
        return self.get_closure(closure_id)

    def _adjustment_delta(self, closure_id: int, material_id: int) -> dict[str, Decimal]:
        """累计某结项明细上追加调整造成的去向净变化（new - old）。"""

        delta = {"accession_quantity": ZERO, "returned_quantity": ZERO, "destroyed_quantity": ZERO}
        rows = self.connection.execute(
            "SELECT field,old_value,new_value FROM closure_adjustments "
            "WHERE closure_id=? AND material_id=?",
            (closure_id, material_id),
        ).fetchall()
        for item in rows:
            delta[item["field"]] += Decimal(item["new_value"]) - Decimal(item["old_value"])
        return delta

    # ------------------------------------------------------------------
    # 结项流程：去向与守恒查询
    # ------------------------------------------------------------------

    def conservation_report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """回答结项前后数量是否守恒。"""

        self._user(actor_id)
        self.get_batch(batch_id)
        balances = self._material_balances(batch_id)
        materials = self._materials(batch_id)
        rows: list[dict[str, Any]] = []
        balanced = True
        for material_id, material in materials.items():
            base = balances[material_id]
            settled = (
                base["accessioned_prior"] + base["returned_prior"] + base["destroyed_prior"]
            )
            remaining = base["initial"] - base["consumed"] - settled
            line_balanced = remaining == ZERO
            open_designated = ZERO
            open_row = self.connection.execute(
                "SELECT closure_id FROM closure_summaries WHERE batch_id=? "
                "AND state IN ('submitted','instructor_confirmed') LIMIT 1", (batch_id,)
            ).fetchone()
            if open_row is not None:
                values = self.connection.execute(
                    "SELECT accession_quantity,returned_quantity,destroyed_quantity "
                    "FROM closure_materials WHERE closure_id=? AND material_id=?",
                    (open_row["closure_id"], material_id),
                ).fetchone()
                if values is not None:
                    open_designated = (
                        Decimal(values["accession_quantity"])
                        + Decimal(values["returned_quantity"])
                        + Decimal(values["destroyed_quantity"])
                    )
            rows.append({
                "material_id": material_id,
                "material_code": material["material_code"],
                "category": material["category"],
                "unit": material["unit"],
                "initial_quantity": format(base["initial"], "f"),
                "consumed_quantity": format(base["consumed"], "f"),
                "accessioned_quantity": format(base["accessioned_prior"], "f"),
                "returned_quantity": format(base["returned_prior"], "f"),
                "destroyed_quantity": format(base["destroyed_prior"], "f"),
                "open_closure_designated_quantity": format(open_designated, "f"),
                "unaccounted_quantity": format(remaining - open_designated, "f"),
                "conserved": line_balanced or remaining == open_designated,
            })
            if not (line_balanced or remaining == open_designated):
                balanced = False
        totals = {
            "material_count": len(rows),
            "all_conserved": balanced,
        }
        return {"batch_id": batch_id, "materials": rows, **totals}

    def material_trace(self, actor_id: str, material_id: int) -> dict[str, Any]:
        """回答每份样品为何入藏、归还或销毁。"""

        self._user(actor_id)
        material = self.connection.execute(
            "SELECT * FROM materials WHERE material_id=?", (material_id,)
        ).fetchone()
        if material is None:
            raise NotFound("材料不存在")
        consumptions = [
            dict(row) for row in self.connection.execute(
                "SELECT consumption_id,quantity,reason,consumed_by,consumed_at "
                "FROM material_consumptions WHERE material_id=? ORDER BY consumption_id", (material_id,)
            ).fetchall()
        ]
        closure_lines = [
            dict(row) for row in self.connection.execute(
                "SELECT c.closure_id,c.version,c.state,c.invalidation_category,c.invalidation_reason,"
                "m.closure_material_id,m.accession_quantity,m.returned_quantity,m.destroyed_quantity,"
                "m.disposition,m.disposition_reason FROM closure_materials m "
                "JOIN closure_summaries c ON c.closure_id=m.closure_id "
                "WHERE m.material_id=? ORDER BY c.version", (material_id,)
            ).fetchall()
        ]
        for line in closure_lines:
            delta = self._adjustment_delta(line["closure_id"], material_id)
            line["frozen_accession_quantity"] = line["accession_quantity"]
            line["frozen_returned_quantity"] = line["returned_quantity"]
            line["frozen_destroyed_quantity"] = line["destroyed_quantity"]
            line["accession_quantity"] = format(Decimal(line["accession_quantity"]) + delta["accession_quantity"], "f")
            line["returned_quantity"] = format(Decimal(line["returned_quantity"]) + delta["returned_quantity"], "f")
            line["destroyed_quantity"] = format(Decimal(line["destroyed_quantity"]) + delta["destroyed_quantity"], "f")
        accessions = [
            dict(row) for row in self.connection.execute(
                "SELECT accession_id,closure_id,catalog_code,quantity,unit,reason,accessioned_by,accessioned_at "
                "FROM accession_records WHERE material_id=? ORDER BY accession_id", (material_id,)
            ).fetchall()
        ]
        decisions: list[dict[str, Any]] = []
        for line in closure_lines:
            for field, label in (
                ("accession_quantity", "accession"),
                ("returned_quantity", "return"),
                ("destroyed_quantity", "destroy"),
            ):
                if Decimal(line[field]) > 0:
                    decisions.append({
                        "closure_id": line["closure_id"],
                        "version": line["version"],
                        "closure_state": line["state"],
                        "disposition": label,
                        "quantity": line[field],
                        "reason": line["disposition_reason"],
                        "invalidation_category": line["invalidation_category"],
                        "invalidation_reason": line["invalidation_reason"],
                    })
        initial = Decimal(material["initial_quantity"])
        consumed = sum((Decimal(item["quantity"]) for item in consumptions), ZERO)
        accessioned = sum((Decimal(item["quantity"]) for item in accessions), ZERO)
        executed = self.connection.execute(
            "SELECT m.returned_quantity,m.destroyed_quantity "
            "FROM closure_materials m JOIN closure_summaries c ON c.closure_id=m.closure_id "
            "WHERE c.museum_confirmed_at IS NOT NULL AND m.material_id=?",
            (material_id,),
        ).fetchall()
        returned = sum((Decimal(row["returned_quantity"]) for row in executed), ZERO)
        destroyed = sum((Decimal(row["destroyed_quantity"]) for row in executed), ZERO)
        accounted = consumed + accessioned + returned + destroyed
        return {
            "material": dict(material),
            "conservation": {
                "initial_quantity": format(initial, "f"),
                "consumed_quantity": format(consumed, "f"),
                "accessioned_quantity": format(accessioned, "f"),
                "returned_quantity": format(returned, "f"),
                "destroyed_quantity": format(destroyed, "f"),
                "unaccounted_quantity": format(initial - accounted, "f"),
                "conserved": initial == accounted,
            },
            "consumptions": consumptions,
            "closure_decisions": decisions,
            "accession_records": accessions,
        }
