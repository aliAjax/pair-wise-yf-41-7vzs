from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
)
from .rules import RuleEngine


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
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]

        if entity["kind"] == "revision_order" and action == "approve":
            return self._approve_revision_order(actor, entity, dict(data or {}), expected)

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

    def _approve_revision_order(self, actor, order, payload, expected_order_version):
        next_status, patch = self.rules.validate_transition(
            actor, order, "approve", payload, self._lookup
        )
        if order["status"] != "pending":
            raise InvalidTransition(
                "cannot approve revision order in status %s" % order["status"]
            )

        event_id = order["data"].get("event_id")
        event = self.repository.get_entity(event_id)
        if not event:
            raise NotFoundError("event not found: " + str(event_id))

        base_version = order["data"].get("event_base_version")
        if base_version is not None and event["version"] != int(base_version):
            raise ConflictError(
                "revision order is stale: 事件自修订单提交后已被修订（当前版本 %s，修订单基于版本 %s），"
                "旧审批不能覆盖新数据，请基于新版本重新提交修订单"
                % (event["version"], base_version)
            )
        if event["status"] not in ("published", "revised"):
            raise InvalidTransition(
                "cannot apply revision to event in status %s" % event["status"]
            )

        # 更新事件：合并新增台站报告、写入新震级与依据、修订次数加一。
        event_data = dict(event["data"])
        reports = list(event_data.get("reports") or [])
        existing_codes = {item.get("station") for item in reports}
        for station in order["data"].get("added_stations") or []:
            if station.get("station") not in existing_codes:
                reports.append(station)
                existing_codes.add(station.get("station"))
        event_data["reports"] = reports
        event_data["magnitude"] = float(order["data"]["magnitude"])
        event_data["last_revision"] = {
            "order_id": order["id"],
            "added_stations": order["data"].get("added_stations") or [],
            "basis": order["data"].get("basis"),
            "approved_by": actor.user_id,
        }
        revision_count = int(event_data.get("revision_count") or 0) + 1
        event_data["revision_count"] = revision_count

        order_data = dict(order["data"])
        order_data.update(patch)
        order_data["approved_event_version"] = event["version"] + 1
        order_data["revision_no"] = revision_count

        updated_event, updated_order = self.repository.apply_revision(
            event_id=event_id,
            expected_event_version=event["version"],
            event_status="revised",
            event_data=event_data,
            order_id=order["id"],
            expected_order_version=expected_order_version,
            order_status=next_status,
            order_data=order_data,
        )
        self.audit.record(
            event_id,
            actor,
            "revise",
            event["status"],
            updated_event["status"],
            {
                "order_id": order["id"],
                "revision_no": revision_count,
                "magnitude": event_data["magnitude"],
                "added_stations": order["data"].get("added_stations") or [],
                "basis": order["data"].get("basis"),
                "from_version": event["version"],
                "to_version": updated_event["version"],
            },
        )
        self.audit.record(
            order["id"],
            actor,
            "approve",
            order["status"],
            updated_order["status"],
            {"event_id": event_id, "revision_no": revision_count},
        )
        return updated_order

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, event_id=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        if event_id:
            items = [item for item in items if item["data"].get("event_id") == event_id]
        return items

    def versions(self, entity_id):
        if not self.repository.get_entity(entity_id):
            raise NotFoundError("entity not found: " + entity_id)
        return self.repository.list_versions(entity_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
