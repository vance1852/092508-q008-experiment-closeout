"""结项流程的领域规则：材料台账、数量守恒与结项单快照。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .contracts import ValidationError
from .jsonio import content_digest

MATERIAL_CATEGORIES = (
    "live_observation",
    "temporary_slide",
    "residual_reagent",
    "accession_candidate",
)

DISPOSITION_ACCESSION = "accession"
DISPOSITION_RETURN = "return"
DISPOSITION_DESTROY = "destroy"
DISPOSITION_MIXED = "mixed"
DISPOSITION_CONSUMED = "consumed"

INVALIDATION_CATEGORIES = ("protocol_correction", "observation_correction")


def quantity(value: object, path: str) -> Decimal:
    """解析非负十进制数量。"""

    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是十进制数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    if result < 0:
        raise ValidationError(f"{path} 不能为负数")
    return result


def require_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


@dataclass(frozen=True, slots=True)
class Participant:
    """实际参与联合实验的人员。"""

    user_id: str
    role: str
    note: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Participant":
        data = require_mapping(raw, path)
        return cls(
            user_id=require_text(data.get("user_id"), f"{path}.user_id"),
            role=require_text(data.get("role"), f"{path}.role"),
            note=(
                None
                if data.get("note") is None
                else require_text(data.get("note"), f"{path}.note")
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "role": self.role, "note": self.note}


@dataclass(frozen=True, slots=True)
class DispositionLine:
    """结项单中一份材料的去向申报与守恒基数。"""

    material_id: int
    initial: Decimal
    consumed_before: Decimal
    accessioned_prior: Decimal
    returned_prior: Decimal
    destroyed_prior: Decimal
    accession: Decimal
    returned: Decimal
    destroyed: Decimal
    reason: str

    @classmethod
    def from_dict(
        cls,
        raw: object,
        path: str,
        balances: Mapping[int, Mapping[str, Decimal]],
    ) -> "DispositionLine":
        data = require_mapping(raw, path)
        material_id = data.get("material_id")
        if isinstance(material_id, bool) or not isinstance(material_id, int):
            raise ValidationError(f"{path}.material_id 必须是整数")
        if material_id not in balances:
            raise ValidationError(f"{path}.material_id 未在本批次登记")
        base = balances[material_id]
        accession = quantity(data.get("accession_quantity", 0), f"{path}.accession_quantity")
        returned = quantity(data.get("returned_quantity", 0), f"{path}.returned_quantity")
        destroyed = quantity(data.get("destroyed_quantity", 0), f"{path}.destroyed_quantity")
        reason = require_text(data.get("reason"), f"{path}.reason")
        line = cls(
            material_id=material_id,
            initial=base["initial"],
            consumed_before=base["consumed"],
            accessioned_prior=base["accessioned_prior"],
            returned_prior=base["returned_prior"],
            destroyed_prior=base["destroyed_prior"],
            accession=accession,
            returned=returned,
            destroyed=destroyed,
            reason=reason,
        )
        line.verify_conservation(path)
        return line

    @property
    def settled_prior(self) -> Decimal:
        return self.accessioned_prior + self.returned_prior + self.destroyed_prior

    @property
    def remaining(self) -> Decimal:
        return self.initial - self.consumed_before - self.settled_prior

    @property
    def disposed(self) -> Decimal:
        return self.accession + self.returned + self.destroyed

    def verify_conservation(self, path: str) -> None:
        if self.disposed > self.remaining:
            raise ValidationError(
                f"{path} 数量不守恒：初始 {self.initial} - 已消耗 {self.consumed_before} - "
                f"前期已闭环去向 {self.settled_prior}（入藏 {self.accessioned_prior}/归还 {self.returned_prior}/"
                f"销毁 {self.destroyed_prior}）= 剩余 {self.remaining}，本次申报合计 {self.disposed} 超出剩余"
            )
        if self.disposed < self.remaining:
            raise ValidationError(
                f"{path} 数量不守恒：初始 {self.initial} - 已消耗 {self.consumed_before} - "
                f"前期已闭环去向 {self.settled_prior} = 剩余 {self.remaining}，本次仅申报 {self.disposed}，"
                f"尚有 {self.remaining - self.disposed} 未交代去向"
            )

    @property
    def disposition(self) -> str:
        positive = [
            (self.accession, DISPOSITION_ACCESSION),
            (self.returned, DISPOSITION_RETURN),
            (self.destroyed, DISPOSITION_DESTROY),
        ]
        kinds = [label for value, label in positive if value > 0]
        if not kinds:
            return DISPOSITION_CONSUMED
        if len(kinds) == 1:
            return kinds[0]
        return DISPOSITION_MIXED

    def as_snapshot(self, category: str, material_code: str, unit: str) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "material_code": material_code,
            "category": category,
            "initial_quantity": format(self.initial, "f"),
            "consumed_quantity": format(self.consumed_before, "f"),
            "accessioned_prior_quantity": format(self.accessioned_prior, "f"),
            "returned_prior_quantity": format(self.returned_prior, "f"),
            "destroyed_prior_quantity": format(self.destroyed_prior, "f"),
            "accession_quantity": format(self.accession, "f"),
            "returned_quantity": format(self.returned, "f"),
            "destroyed_quantity": format(self.destroyed, "f"),
            "unit": unit,
            "disposition": self.disposition,
            "disposition_reason": self.reason,
        }


def parse_participants(raw: object) -> tuple[Participant, ...]:
    rows = require_sequence(raw, "participants")
    if not rows:
        raise ValidationError("participants 不能为空，结项必须登记实际参与者")
    participants = tuple(
        Participant.from_dict(item, f"participants[{index}]") for index, item in enumerate(rows)
    )
    identities = [item.user_id for item in participants]
    if len(set(identities)) != len(identities):
        raise ValidationError("participants.user_id 不能重复")
    return participants


def parse_lines(
    raw: object,
    balances: Mapping[int, Mapping[str, Decimal]],
    materials: Mapping[int, Mapping[str, Any]],
) -> tuple[DispositionLine, ...]:
    rows = require_sequence(raw, "materials")
    if not rows:
        raise ValidationError("materials 不能为空，结项必须交代每份材料去向")
    lines = tuple(
        DispositionLine.from_dict(item, f"materials[{index}]", balances)
        for index, item in enumerate(rows)
    )
    if {line.material_id for line in lines} != set(materials):
        missing = sorted(set(materials) - {line.material_id for line in lines})
        extra = sorted({line.material_id for line in lines} - set(materials))
        raise ValidationError(f"材料去向必须完整覆盖批次材料：缺少 {missing}，多出 {extra}")
    return lines


def build_snapshot(
    *,
    batch: Mapping[str, Any],
    evidence_protocol: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    decision: Mapping[str, Any] | None,
    participants: Sequence[Participant],
    lines: Sequence[DispositionLine],
    materials: Mapping[int, Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """组装结项时刻的不可变事实快照。"""

    return {
        "batch": {
            "batch_id": batch["batch_id"],
            "revision": batch["revision"],
            "state": batch["state"],
            "evidence_protocol_id": batch["evidence_protocol_id"],
            "evidence_protocol_version": batch["evidence_protocol_version"],
            "build_id": batch["build_id"],
        },
        "evidence_protocol": {
            "evidence_protocol_id": evidence_protocol["evidence_protocol_id"],
            "version": evidence_protocol["version"],
            "content_sha256": evidence_protocol["content_sha256"],
        },
        "analysis": None
        if analysis is None
        else {
            "analysis_id": analysis["analysis_id"],
            "batch_revision": analysis["batch_revision"],
            "input_sha256": analysis["input_sha256"],
            "evidence_protocol_sha256": analysis["evidence_protocol_sha256"],
            "algorithm_version": analysis["algorithm_version"],
        },
        "decision": None
        if decision is None
        else {
            "decision_id": decision["decision_id"],
            "decision": decision["decision"],
            "analysis_id": decision["analysis_id"],
        },
        "exclusions": [dict(item) for item in exclusions],
        "participants": [item.as_dict() for item in participants],
        "materials": [
            line.as_snapshot(
                materials[line.material_id]["category"],
                materials[line.material_id]["material_code"],
                materials[line.material_id]["unit"],
            )
            for line in lines
        ],
    }


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    return content_digest([snapshot])
