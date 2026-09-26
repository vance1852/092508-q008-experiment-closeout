"""实验批次结项流程：不可变结项单、双方确认、更正失效与物料守恒。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import transaction

# 登记物料时允许的实物去向；活体归还或销毁、临时玻片销毁或入藏、
# 试剂只能消耗后销毁、可入藏样品入藏或归还。
ALLOWED_OUTCOMES: Mapping[str, frozenset[str]] = {
    "live_specimen": frozenset({"return", "destruction"}),
    "temporary_slide": frozenset({"accession", "destruction"}),
    "reagent": frozenset({"destruction"}),
    "collectible_sample": frozenset({"accession", "return"}),
}

ACTIVE_STATES = frozenset({"submitted", "instructor_confirmed", "confirmed"})


class CloseoutMixin:
    """为 TaxonomyLabService 提供结项用例；依赖同类上的连接与辅助方法。"""

    # ---- 物料登记与去向 -------------------------------------------------

    def register_material_stock(
        self,
        actor_id: str,
        batch_id: str,
        material_type: str,
        material_ref: str,
        initial_quantity: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "material.register")
        if material_type not in ALLOWED_OUTCOMES:
            raise ValidationFailed("未知物料类型")
        material_ref = material_ref.strip()
        if not material_ref:
            raise ValidationFailed("物料编号不能为空")
        if isinstance(initial_quantity, bool) or not isinstance(initial_quantity, int) or initial_quantity <= 0:
            raise ValidationFailed("初始数量必须是正整数")
        batch = self.get_batch(batch_id)
        if batch["state"] not in {"running", "sealed", "analyzed", "decided"}:
            raise InvalidState("当前批次状态不能登记物料")
        self._ensure_no_active_closeout(batch_id)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO material_stocks(batch_id,material_type,material_ref,initial_quantity,"
                    "consumed_quantity,registered_by,registered_at) VALUES(?,?,?,?,0,?,?)",
                    (batch_id, material_type, material_ref, initial_quantity, actor_id, self._now()),
                )
                stock_id = cursor.lastrowid
                self._audit(
                    "material_stock", str(stock_id), "material.registered", actor_id,
                    {"batch_id": batch_id, "material_type": material_type, "material_ref": material_ref,
                     "initial_quantity": initial_quantity},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一批次的物料编号已经登记") from exc
        return {
            "stock_id": stock_id, "batch_id": batch_id, "material_type": material_type,
            "material_ref": material_ref, "initial_quantity": initial_quantity, "consumed_quantity": 0,
        }

    def record_consumption(self, actor_id: str, batch_id: str, stock_id: int, quantity: int) -> dict[str, Any]:
        self._require(actor_id, "material.register")
        stock = self._get_stock(batch_id, stock_id)
        self._ensure_no_active_closeout(batch_id)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationFailed("消耗数量必须是正整数")
        ledger = self._material_ledger(batch_id)
        entry = next(item for item in ledger if item["stock_id"] == stock_id)
        if entry["consumed_quantity"] + entry["outcome_total"] + quantity > stock["initial_quantity"]:
            raise InvalidState("累计消耗与去向数量不能超过初始数量")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE material_stocks SET consumed_quantity=consumed_quantity+? WHERE stock_id=? AND batch_id=?",
                (quantity, stock_id, batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("物料记录已变化")
            self._audit(
                "material_stock", str(stock_id), "material.consumed", actor_id,
                {"batch_id": batch_id, "quantity": quantity},
            )
        return self._stock_payload(batch_id, stock_id)

    def record_disposition(
        self,
        actor_id: str,
        batch_id: str,
        material_type: str,
        material_ref: str,
        outcome: str,
        quantity: int,
        reason: str,
        evidence_item_id: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "disposition.record")
        if material_type not in ALLOWED_OUTCOMES:
            raise ValidationFailed("未知物料类型")
        if outcome not in ALLOWED_OUTCOMES[material_type]:
            raise ValidationFailed(f"{material_type} 不允许的实物去向: {outcome}")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationFailed("去向数量必须是正整数")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("实物去向必须说明原因")
        stock_row = self.connection.execute(
            "SELECT * FROM material_stocks WHERE batch_id=? AND material_type=? AND material_ref=?",
            (batch_id, material_type, material_ref.strip()),
        ).fetchone()
        if stock_row is None:
            raise NotFound("物料尚未登记，不能记录去向")
        self._ensure_no_active_closeout(batch_id)
        if evidence_item_id is not None:
            item = self.connection.execute(
                "SELECT evidence_item_id FROM evidence_items WHERE evidence_item_id=? AND batch_id=?",
                (evidence_item_id, batch_id),
            ).fetchone()
            if item is None:
                raise ValidationFailed("关联的观察记录不属于该批次")
        # 已归档去向不可变：允许对剩余数量追加，但每条记录只增不改。
        ledger = self._material_ledger(batch_id)
        entry = next(item for item in ledger if item["stock_id"] == stock_row["stock_id"])
        if entry["consumed_quantity"] + entry["outcome_total"] + quantity > stock_row["initial_quantity"]:
            raise InvalidState("累计消耗与去向数量不能超过初始数量")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO material_dispositions(batch_id,stock_id,material_type,material_ref,outcome,quantity,"
                "reason,evidence_item_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_id, stock_row["stock_id"], material_type, stock_row["material_ref"], outcome,
                 quantity, reason, evidence_item_id, actor_id, self._now()),
            )
            disposition_id = cursor.lastrowid
            self._audit(
                "material_disposition", str(disposition_id), "disposition.recorded", actor_id,
                {"batch_id": batch_id, "material_ref": stock_row["material_ref"], "outcome": outcome,
                 "quantity": quantity, "evidence_item_id": evidence_item_id},
            )
        return {"disposition_id": disposition_id, "outcome": outcome, "quantity": quantity, "reason": reason}

    # ---- 结项单 ---------------------------------------------------------

    def submit_closeout(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        participant_ids: Iterable[str],
        note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "closeout.submit")
        participant_ids = tuple(participant_ids)
        request_payload = {"participants": list(participant_ids), "note": note}
        request_digest = content_digest([request_payload])
        scope = f"closeout:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "decided":
            raise InvalidState("只有已形成采信决定的批次可以提交结项")
        active = self.connection.execute(
            "SELECT closeout_id FROM closeouts WHERE batch_id=? AND state IN ('submitted','instructor_confirmed','confirmed')",
            (batch_id,),
        ).fetchone()
        if active is not None:
            raise Conflict("批次存在尚未失效的结项单")
        participants = self._participants_snapshot(participant_ids)
        ledger = self._material_ledger(batch_id)
        if not ledger:
            raise InvalidState("结项前必须登记实验物料")
        conservation = self._conservation(ledger)
        if not conservation["conserved"]:
            raise InvalidState("物料数量不守恒，不能提交结项")
        protocol, protocol_digest = self._evidence_protocol(
            batch["evidence_protocol_id"], batch["evidence_protocol_version"]
        )
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        if analysis_row is None:
            raise InvalidState("批次缺少分析结果")
        decision_row = self.connection.execute(
            "SELECT * FROM decisions WHERE analysis_id=? ORDER BY decision_id DESC LIMIT 1",
            (analysis_row["analysis_id"],),
        ).fetchone()
        if decision_row is None:
            raise InvalidState("批次缺少采信决定")
        result = json.loads(analysis_row["result_json"])
        exclusions = self._exclusions_snapshot(batch_id)
        pending_exclusions = [item for item in exclusions if item["status"] == "pending"]
        if pending_exclusions:
            raise InvalidState("仍有待复核的异常排除，不能提交结项")
        approved_exclusions = [item for item in exclusions if item["status"] == "approved"]
        if result.get("excluded_count") != len(approved_exclusions):
            raise InvalidState(
                f"异常排除数量与分析结果不一致：排除 {len(approved_exclusions)} 条，"
                f"分析剔除 {result.get('excluded_count')} 条"
            )
        snapshot = {
            "batch_id": batch_id,
            "batch_revision": batch["revision"],
            "protocol": {
                "evidence_protocol_id": protocol.evidence_protocol_id,
                "version": protocol.version,
                "sha256": protocol_digest,
            },
            "analysis": {
                "analysis_id": analysis_row["analysis_id"],
                "input_sha256": analysis_row["input_sha256"],
                "algorithm_version": analysis_row["algorithm_version"],
                "result_digest": content_digest([result]),
                "conclusion": result.get("conclusion"),
                "included_count": result.get("included_count"),
                "excluded_count": result.get("excluded_count"),
            },
            "decision": {
                "decision_id": decision_row["decision_id"],
                "decision": decision_row["decision"],
            },
            "participants": participants,
            "exclusions": exclusions,
            "materials": ledger,
            "conservation": conservation,
            "note": note.strip(),
        }
        content_sha = content_digest([snapshot])
        response: dict[str, Any]
        try:
            with transaction(self.connection, immediate=True):
                serial_row = self.connection.execute(
                    "SELECT COALESCE(MAX(serial),0)+1 AS next_serial FROM closeouts WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                serial = serial_row["next_serial"]
                cursor = self.connection.execute(
                    "INSERT INTO closeouts(batch_id,serial,state,evidence_protocol_id,evidence_protocol_version,"
                    "evidence_protocol_sha256,analysis_id,input_sha256,result_digest,decision_id,decision,"
                    "batch_revision,participants_json,exclusions_json,material_snapshot_json,conservation_json,"
                    "request_sha256,content_sha256,submitted_by,submitted_at) "
                    "VALUES(?,?, 'submitted',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id, serial, protocol.evidence_protocol_id, protocol.version, protocol_digest,
                        analysis_row["analysis_id"], analysis_row["input_sha256"],
                        snapshot["analysis"]["result_digest"], decision_row["decision_id"],
                        decision_row["decision"], batch["revision"],
                        canonical_json(participants), canonical_json(exclusions),
                        canonical_json(ledger), canonical_json(conservation),
                        request_digest, content_sha, actor_id, self._now(),
                    ),
                )
                closeout_id = cursor.lastrowid
                self.connection.execute(
                    "UPDATE closeouts SET superseded_by_closeout_id=? "
                    "WHERE batch_id=? AND serial=? AND state='superseded'",
                    (closeout_id, batch_id, serial - 1),
                )
                stored_row = self.connection.execute(
                    "SELECT * FROM closeouts WHERE closeout_id=?", (closeout_id,)
                ).fetchone()
                response = self._serialize_closeout(stored_row)
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "closeout", str(closeout_id), "closeout.submitted", actor_id,
                    {"batch_id": batch_id, "serial": serial, "content_sha256": content_sha},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("结项单版本或幂等键冲突") from exc
        return response

    def confirm_closeout_instructor(
        self, actor_id: str, closeout_id: int, expected_content_sha256: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "closeout.confirm.instructor")
        return self._confirm_closeout(
            actor_id, closeout_id, expected_content_sha256,
            awaiting="submitted", next_state="instructor_confirmed",
            actor_column="instructor_confirmed_by", at_column="instructor_confirmed_at",
            event="closeout.instructor_confirmed",
        )

    def confirm_closeout_museum(
        self, actor_id: str, closeout_id: int, expected_content_sha256: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "closeout.confirm.museum")
        row = self._get_closeout_row(closeout_id)
        if row["state"] == "confirmed":
            return self._replay(row, expected_content_sha256)
        if row["state"] != "instructor_confirmed":
            if row["state"] in ACTIVE_STATES:
                raise InvalidState("结项单尚未经指导教师确认")
            raise InvalidState("结项单已失效，不能确认")
        self._check_expected_digest(row, expected_content_sha256)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE closeouts SET state='confirmed',museum_confirmed_by=?,museum_confirmed_at=? "
                "WHERE closeout_id=? AND state='instructor_confirmed' AND content_sha256=?",
                (actor_id, self._now(), closeout_id, row["content_sha256"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            # 馆方确认时把实物去向归档；归档行之后不提供任何修改路径。
            self.connection.execute(
                "UPDATE material_dispositions SET archived_in_closeout=? "
                "WHERE batch_id=? AND archived_in_closeout IS NULL",
                (closeout_id, row["batch_id"]),
            )
            self.connection.execute(
                "UPDATE batches SET state='closed' WHERE batch_id=? AND state='decided'",
                (row["batch_id"],),
            )
            self._audit(
                "closeout", str(closeout_id), "closeout.museum_confirmed", actor_id,
                {"batch_id": row["batch_id"], "serial": row["serial"]},
            )
        return self.get_closeout(closeout_id)

    def withdraw_closeout(self, actor_id: str, closeout_id: int, reason: str) -> dict[str, Any]:
        row = self._get_closeout_row(closeout_id)
        actor = self._user(actor_id)
        if actor["role"] != "instructor" and row["submitted_by"] != actor_id:
            raise Forbidden("只有提交人或指导教师可以撤回结项单")
        if row["state"] != "submitted":
            raise InvalidState("只有待确认的结项单可以撤回")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("撤回必须填写原因")
        return self._invalidate(row, "withdrawn", actor_id, reason, "closeout.withdrawn", reason_column="return_reason")

    def return_closeout(
        self,
        actor_id: str,
        closeout_id: int,
        reason: str,
        partial: bool = False,
        returned_fields: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        row = self._get_closeout_row(closeout_id)
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("退回必须填写原因")
        fields = tuple(dict.fromkeys(item.strip() for item in (returned_fields or ()) if item.strip()))
        if not fields:
            raise ValidationFailed("退回必须指明需要修改的字段")
        if row["state"] == "submitted":
            self._require(actor_id, "closeout.confirm.instructor")
        elif row["state"] == "instructor_confirmed":
            self._require(actor_id, "closeout.confirm.museum")
        else:
            raise InvalidState("当前结项单状态不能退回")
        target_state = "partially_returned" if partial else "returned"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE closeouts SET state=?,return_reason=?,"
                f"{'partially_returned_fields_json' if partial else 'returned_fields_json'}=? "
                "WHERE closeout_id=? AND state=?",
                (target_state, reason, canonical_json(fields), closeout_id, row["state"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            self._audit(
                "closeout", str(closeout_id),
                "closeout.partially_returned" if partial else "closeout.returned",
                actor_id, {"reason": reason, "fields": list(fields)},
            )
        return self.get_closeout(closeout_id)

    def supersede_closeout(self, actor_id: str, closeout_id: int, reason: str) -> dict[str, Any]:
        """方案或关键观测更正后，由指导教师宣告已确认结项单失效并重开批次。"""
        self._require(actor_id, "closeout.supersede")
        row = self._get_closeout_row(closeout_id)
        if row["state"] != "confirmed":
            raise InvalidState("只有馆方已确认的结项单可以因更正而失效")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("宣告旧结项失效必须保留原因")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE closeouts SET state='superseded',superseded_reason=?,superseded_at=?,superseded_by=? "
                "WHERE closeout_id=? AND state='confirmed'",
                (reason, self._now(), actor_id, closeout_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            # 重开批次并抬升 revision，使后续封存生成新的分析任务。
            reopened = self.connection.execute(
                "UPDATE batches SET state='running',revision=revision+1 "
                "WHERE batch_id=? AND state='closed'",
                (row["batch_id"],),
            )
            if reopened.rowcount != 1:
                raise InvalidState("批次未处于已结项状态，拒绝重开")
            self._audit(
                "closeout", str(closeout_id), "closeout.superseded", actor_id,
                {"batch_id": row["batch_id"], "reason": reason},
            )
        return self.get_closeout(closeout_id)

    def correct_batch_protocol(
        self, actor_id: str, batch_id: str, evidence_protocol_version: int
    ) -> dict[str, Any]:
        """结项失效重开后，把批次切换到新的方案版本。"""
        self._require(actor_id, "batch.protocol_correct")
        batch = self.get_batch(batch_id)
        if batch["state"] != "running":
            raise InvalidState("只有更正重开后的批次可以切换方案版本")
        if evidence_protocol_version == batch["evidence_protocol_version"]:
            raise ValidationFailed("新方案版本必须与旧版本不同")
        self._evidence_protocol(batch["evidence_protocol_id"], evidence_protocol_version)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET evidence_protocol_version=? "
                "WHERE batch_id=? AND state='running'",
                (evidence_protocol_version, batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次状态已变化")
            self._audit(
                "batch", batch_id, "batch.protocol_corrected", actor_id,
                {"from_version": batch["evidence_protocol_version"], "to_version": evidence_protocol_version},
            )
        return self.get_batch(batch_id)

    # ---- 查询 -----------------------------------------------------------

    def get_closeout(self, closeout_id: int) -> dict[str, Any]:
        return self._serialize_closeout(self._get_closeout_row(closeout_id))

    def read_closeout(self, actor_id: str, closeout_id: int) -> dict[str, Any]:
        self._require(actor_id, "closeout.read")
        return self.get_closeout(closeout_id)

    def list_closeouts(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "closeout.read")
        self.get_batch(batch_id)
        rows = self.connection.execute(
            "SELECT closeout_id,serial,state,content_sha256,submitted_by,submitted_at,"
            "instructor_confirmed_by,instructor_confirmed_at,museum_confirmed_by,museum_confirmed_at,"
            "return_reason,superseded_reason,superseded_by_closeout_id "
            "FROM closeouts WHERE batch_id=? ORDER BY serial",
            (batch_id,),
        ).fetchall()
        return {"batch_id": batch_id, "closeouts": [dict(row) for row in rows]}

    def closeout_report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """回答每份物料为何入藏/归还/销毁，以及结项前后数量是否守恒。"""
        self._require(actor_id, "closeout.read")
        batch = self.get_batch(batch_id)
        closeouts_rows = self.connection.execute(
            "SELECT * FROM closeouts WHERE batch_id=? ORDER BY serial", (batch_id,)
        ).fetchall()
        closeouts = [self._serialize_closeout(row) for row in closeouts_rows]
        current = next((item for item in reversed(closeouts) if item["state"] in ACTIVE_STATES), None)
        ledger = self._material_ledger(batch_id)
        ledger_payload = []
        for entry in ledger:
            dispositions = []
            for disposition in entry["dispositions"]:
                archived_closeout = None
                if disposition["archived_in_closeout"] is not None:
                    archived_closeout = disposition["archived_in_closeout"]
                dispositions.append({
                    "disposition_id": disposition["disposition_id"],
                    "outcome": disposition["outcome"],
                    "quantity": disposition["quantity"],
                    "reason": disposition["reason"],
                    "evidence_item_id": disposition["evidence_item_id"],
                    "recorded_by": disposition["recorded_by"],
                    "recorded_at": disposition["recorded_at"],
                    "archived_in_closeout": archived_closeout,
                    "immutable": archived_closeout is not None,
                })
            ledger_payload.append({
                "stock_id": entry["stock_id"],
                "material_type": entry["material_type"],
                "material_ref": entry["material_ref"],
                "initial_quantity": entry["initial_quantity"],
                "consumed_quantity": entry["consumed_quantity"],
                "outcomes": entry["outcomes"],
                "archived_outcomes": entry["archived_outcomes"],
                "balance": entry["balance"],
                "conserved": entry["balance"] == 0,
                "dispositions": dispositions,
            })
        return {
            "batch": batch,
            "current_closeout_id": None if current is None else current["closeout_id"],
            "closeouts": closeouts,
            "materials": ledger_payload,
            "conservation": self._conservation(ledger),
        }

    # ---- 内部辅助 -------------------------------------------------------

    def _confirm_closeout(
        self,
        actor_id: str,
        closeout_id: int,
        expected_content_sha256: str | None,
        *,
        awaiting: str,
        next_state: str,
        actor_column: str,
        at_column: str,
        event: str,
    ) -> dict[str, Any]:
        row = self._get_closeout_row(closeout_id)
        if row["state"] != awaiting:
            if row["state"] in {next_state, "confirmed"}:
                return self._replay(row, expected_content_sha256)
            if row["state"] in ACTIVE_STATES:
                raise InvalidState("结项单当前状态不能执行该确认")
            raise InvalidState("结项单已失效，不能确认")
        self._check_expected_digest(row, expected_content_sha256)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE closeouts SET state=?,{actor_column}=?,{at_column}=? "
                f"WHERE closeout_id=? AND state=? AND content_sha256=?",
                (next_state, actor_id, self._now(), closeout_id, awaiting, row["content_sha256"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            self._audit(
                "closeout", str(closeout_id), event, actor_id,
                {"batch_id": row["batch_id"], "serial": row["serial"]},
            )
        return self.get_closeout(closeout_id)

    def _invalidate(
        self, row: sqlite3.Row, state: str, actor_id: str, reason: str, event: str, *, reason_column: str
    ) -> dict[str, Any]:
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE closeouts SET state=?,{reason_column}=? WHERE closeout_id=? AND state='submitted'",
                (state, reason, row["closeout_id"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结项单状态已变化")
            self._audit(
                "closeout", str(row["closeout_id"]), event, actor_id,
                {"batch_id": row["batch_id"], "reason": reason},
            )
        return self.get_closeout(row["closeout_id"])

    def _replay(self, row: sqlite3.Row, expected_content_sha256: str | None) -> dict[str, Any]:
        """重复确认时回放原结项单，而不是生成新结果。"""
        self._check_expected_digest(row, expected_content_sha256)
        return self._serialize_closeout(row)

    @staticmethod
    def _check_expected_digest(row: sqlite3.Row, expected_content_sha256: str | None) -> None:
        if expected_content_sha256 and expected_content_sha256 != row["content_sha256"]:
            raise Conflict("回放摘要与结项单内容不一致，拒绝重复确认")

    def _get_closeout_row(self, closeout_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM closeouts WHERE closeout_id=?", (closeout_id,)).fetchone()
        if row is None:
            raise NotFound("结项单不存在")
        return row

    def _get_stock(self, batch_id: str, stock_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM material_stocks WHERE stock_id=? AND batch_id=?", (stock_id, batch_id)
        ).fetchone()
        if row is None:
            raise NotFound("物料记录不存在")
        return row

    def _stock_payload(self, batch_id: str, stock_id: int) -> dict[str, Any]:
        row = self._get_stock(batch_id, stock_id)
        return {
            "stock_id": row["stock_id"], "batch_id": batch_id,
            "material_type": row["material_type"], "material_ref": row["material_ref"],
            "initial_quantity": row["initial_quantity"], "consumed_quantity": row["consumed_quantity"],
        }

    def _ensure_no_active_closeout(self, batch_id: str) -> None:
        row = self.connection.execute(
            "SELECT closeout_id FROM closeouts WHERE batch_id=? "
            "AND state IN ('submitted','instructor_confirmed','confirmed')",
            (batch_id,),
        ).fetchone()
        if row is not None:
            raise InvalidState("批次已有生效中的结项单，物料记录被冻结")

    def _participants_snapshot(self, participant_ids: Iterable[str]) -> list[dict[str, str]]:
        unique_ids = tuple(dict.fromkeys(item.strip() for item in participant_ids if item.strip()))
        if not unique_ids:
            raise ValidationFailed("实际参与者不能为空")
        participants: list[dict[str, str]] = []
        for user_id in unique_ids:
            user = self.connection.execute(
                "SELECT user_id,display_name,role,active FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise ValidationFailed(f"参与者不存在: {user_id}")
            if not user["active"]:
                raise ValidationFailed(f"参与者已停用: {user_id}")
            participants.append({"user_id": user_id, "display_name": user["display_name"], "role": user["role"]})
        return participants

    def _exclusions_snapshot(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT e.exclusion_id,e.evidence_item_id,o.source_batch,o.source_row,e.status,e.reason,"
            "e.requested_by,e.reviewed_by,e.review_note "
            "FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
            "WHERE o.batch_id=? ORDER BY e.exclusion_id",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _material_ledger(self, batch_id: str) -> list[dict[str, Any]]:
        stocks = self.connection.execute(
            "SELECT * FROM material_stocks WHERE batch_id=? ORDER BY stock_id", (batch_id,)
        ).fetchall()
        dispositions = self.connection.execute(
            "SELECT * FROM material_dispositions WHERE batch_id=? ORDER BY disposition_id", (batch_id,)
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for disposition in dispositions:
            grouped.setdefault(disposition["stock_id"], []).append(disposition)
        ledger: list[dict[str, Any]] = []
        for stock in stocks:
            items = grouped.get(stock["stock_id"], [])
            outcomes = {"accession": 0, "return": 0, "destruction": 0}
            archived_outcomes = {"accession": 0, "return": 0, "destruction": 0}
            payload_items = []
            for disposition in items:
                outcomes[disposition["outcome"]] += disposition["quantity"]
                if disposition["archived_in_closeout"] is not None:
                    archived_outcomes[disposition["outcome"]] += disposition["quantity"]
                payload_items.append(dict(disposition))
            outcome_total = sum(outcomes.values())
            balance = stock["initial_quantity"] - stock["consumed_quantity"] - outcome_total
            ledger.append({
                "stock_id": stock["stock_id"],
                "material_type": stock["material_type"],
                "material_ref": stock["material_ref"],
                "initial_quantity": stock["initial_quantity"],
                "consumed_quantity": stock["consumed_quantity"],
                "outcomes": outcomes,
                "archived_outcomes": archived_outcomes,
                "outcome_total": outcome_total,
                "balance": balance,
                "conserved": balance == 0,
                "dispositions": payload_items,
            })
        return ledger

    @staticmethod
    def _conservation(ledger: list[dict[str, Any]]) -> dict[str, Any]:
        totals = {
            "initial": sum(item["initial_quantity"] for item in ledger),
            "consumed": sum(item["consumed_quantity"] for item in ledger),
            "accession": sum(item["outcomes"]["accession"] for item in ledger),
            "return": sum(item["outcomes"]["return"] for item in ledger),
            "destruction": sum(item["outcomes"]["destruction"] for item in ledger),
        }
        totals["after"] = totals["consumed"] + totals["accession"] + totals["return"] + totals["destruction"]
        return {
            "before": totals["initial"],
            "after": totals["after"],
            "conserved": totals["initial"] == totals["after"] and all(item["balance"] == 0 for item in ledger),
            "totals": totals,
        }

    def _serialize_closeout(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "closeout_id": row["closeout_id"],
            "batch_id": row["batch_id"],
            "serial": row["serial"],
            "state": row["state"],
            "batch_revision": row["batch_revision"],
            "evidence_protocol": {
                "evidence_protocol_id": row["evidence_protocol_id"],
                "version": row["evidence_protocol_version"],
                "sha256": row["evidence_protocol_sha256"],
            },
            "analysis_id": row["analysis_id"],
            "input_sha256": row["input_sha256"],
            "result_digest": row["result_digest"],
            "decision_id": row["decision_id"],
            "decision": row["decision"],
            "participants": json.loads(row["participants_json"]),
            "exclusions": json.loads(row["exclusions_json"]),
            "materials": json.loads(row["material_snapshot_json"]),
            "conservation": json.loads(row["conservation_json"]),
            "request_sha256": row["request_sha256"],
            "content_sha256": row["content_sha256"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "instructor_confirmed_by": row["instructor_confirmed_by"],
            "instructor_confirmed_at": row["instructor_confirmed_at"],
            "museum_confirmed_by": row["museum_confirmed_by"],
            "museum_confirmed_at": row["museum_confirmed_at"],
            "returned_fields": json.loads(row["returned_fields_json"]) if row["returned_fields_json"] else [],
            "partially_returned_fields": (
                json.loads(row["partially_returned_fields_json"]) if row["partially_returned_fields_json"] else []
            ),
            "return_reason": row["return_reason"],
            "superseded_reason": row["superseded_reason"],
            "superseded_at": row["superseded_at"],
            "superseded_by": row["superseded_by"],
        }
