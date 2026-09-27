from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        detail = {"kind": kind}
        if kind == "revision_order":
            detail.update(
                {
                    "event_id": payload.get("event_id"),
                    "base_version": payload.get("base_version"),
                    "new_stations": payload.get("new_stations"),
                    "magnitude": payload.get("magnitude"),
                    "basis": payload.get("basis"),
                }
            )
        self.audit.record(entity_id, actor, "create", None, status, detail)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "revision_order":
            if action == "approve":
                return self._approve_revision(actor, entity)
            if action == "withdraw":
                return self._withdraw_revision(actor, entity)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    @staticmethod
    def _station_key(report):
        return report.get("station") if isinstance(report, dict) else str(report)

    def _merge_revision(self, event, order_data):
        """把修订单中的新增台站并入事件报告，并同步震级与修订次数。"""
        merged_data = dict(event["data"])
        reports = list(merged_data.get("reports") or [])
        known = {self._station_key(report) for report in reports}
        added = []
        for station in order_data.get("new_stations") or []:
            if self._station_key(station) in known:
                continue
            report = dict(station) if isinstance(station, dict) else {"station": station}
            report.setdefault("source", "revision_order")
            reports.append(report)
            known.add(self._station_key(report))
            added.append(report)
        merged_data["reports"] = reports
        merged_data["magnitude"] = order_data["magnitude"]
        previous_magnitude = event["data"].get("magnitude")
        merged_data["revision_count"] = int(event["data"].get("revision_count") or 0) + 1
        merged_data["last_revision_basis"] = order_data["basis"]
        return merged_data, added, previous_magnitude

    def _approve_revision(self, actor, order):
        # 状态机与角色校验（pending -> approved）；不接受事件已变化等数据改写
        self.rules.validate_transition(actor, order, "approve", {}, self._lookup)
        order_data = order["data"]
        event_id = order_data.get("event_id")
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("关联事件不存在: " + str(event_id))

        # 提交后事件又被修订（版本推进）→ 版本已过期，拒绝覆盖
        base_version = int(order_data.get("base_version"))
        if event["version"] != base_version:
            raise InvalidTransition(
                "修订单版本已过期：事件已从第 %s 版修订到第 %s 版，"
                "不能覆盖新数据；请撤回该修订单并基于最新版本重新提交"
                % (base_version, event["version"])
            )
        if event["status"] not in ("published", "revised"):
            raise InvalidTransition("事件当前状态 %s 不允许修订" % event["status"])

        event_data, added, previous_magnitude = self._merge_revision(event, order_data)
        approved_data = dict(order_data)
        approved_data["approver"] = actor.user_id
        approved_data["approved_at"] = _utcnow()
        approved_data["applied_event_version"] = base_version + 1
        approved_data["added_stations"] = added

        # 事务内再做一次乐观版本检查，防止并发覆盖
        updated_order, updated_event = self.repository.apply_revision_order(
            order_id=order["id"],
            event_id=event_id,
            base_version=base_version,
            event_data=event_data,
            order_data=approved_data,
        )
        self.audit.record(
            event_id,
            actor,
            "revise",
            event["status"],
            updated_event["status"],
            {
                "order_id": order["id"],
                "base_version": base_version,
                "added_stations": added,
                "previous_magnitude": previous_magnitude,
                "magnitude": approved_data["magnitude"],
                "revision_count": event_data["revision_count"],
                "basis": approved_data["basis"],
            },
        )
        self.audit.record(
            order["id"],
            actor,
            "approve",
            order["status"],
            updated_order["status"],
            {"event_id": event_id, "applied_event_version": base_version + 1},
        )
        return updated_order

    def _withdraw_revision(self, actor, order):
        # 撤回修订单：只改修订单本身，事件内容保持原样
        next_status, patch = self.rules.validate_transition(
            actor, order, "withdraw", {}, self._lookup
        )
        merged = dict(order["data"])
        merged.update(patch)
        merged["withdrawn_by"] = actor.user_id
        merged["withdrawn_at"] = _utcnow()
        updated = self.repository.update_entity(order["id"], order["version"], next_status, merged)
        self.audit.record(
            order["id"],
            actor,
            "withdraw",
            order["status"],
            updated["status"],
            {"event_id": order["data"].get("event_id"), "event_unchanged": True},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def versions(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_versions(entity_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
