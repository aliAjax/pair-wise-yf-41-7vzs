import math
from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_revision_order(actor, data, lookup):
    event_id = data.get("event_id")
    if not event_id:
        raise ValidationError("revision order requires event_id")
    rows = lookup("event", "id", event_id) if lookup else []
    event = rows[0] if rows else None
    if event is None:
        raise NotFoundError("event not found: " + str(event_id))
    if event["status"] not in ("published", "revised"):
        raise InvalidTransition(
            "revision order can only be opened for published or revised events"
        )

    added_stations = data.get("added_stations") or []
    if not added_stations:
        raise ValidationError("revision order requires at least one added station")

    existing_codes = set()
    for report in event["data"].get("reports") or []:
        if report.get("station"):
            existing_codes.add(report["station"])
    for item in added_stations:
        if not isinstance(item, dict) or not item.get("station"):
            raise ValidationError("each added station requires a station code")
        if item["station"] in existing_codes:
            raise ValidationError(
                "station already associated with event: " + str(item["station"])
            )

    magnitude = data.get("magnitude")
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("revision magnitude must be a number")
    if not math.isfinite(magnitude):
        raise ValidationError("revision magnitude must be a finite number")

    basis = data.get("basis")
    if not basis or not str(basis).strip():
        raise ValidationError("revision basis is required")

    # 服务端快照：订单提交时事件所处版本，审批时据此判断版本是否过期。
    data["event_base_version"] = event["version"]
    data["base_magnitude"] = event["data"].get("magnitude")
    data["base_revision_count"] = event["data"].get("revision_count", 0)
    return data


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'revision_order': _validate_revision_order}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {
        'stations': 'station',
        'events': 'event',
        'revision_orders': 'revision_order',
        'orders': 'revision_order',
    }
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'revision_order': 'pending'}
    TRANSITIONS = {
        'station': {
            'offline': (('online',), 'offline'),
            'online': (('offline',), 'online'),
        },
        'event': {
            'associate': (('candidate',), 'associated'),
            'review': (('associated',), 'reviewed'),
            'publish': (('reviewed',), 'published'),
            # 发布后的震级修订只能通过独立的修订单审批完成，
            # 事件本身不再提供直接覆盖震级的 revise 动作。
            'withdraw': (('published', 'revised'), 'withdrawn'),
        },
        'revision_order': {
            'approve': (('pending',), 'approved'),
            'reject': (('pending',), 'rejected'),
            'withdraw': (('pending',), 'withdrawn'),
        },
    }
    CREATE_REQUIRED = {
        'station': ('code', 'lat', 'lon'),
        'event': ('title', 'origin_time', 'location', 'reports'),
        'revision_order': ('event_id', 'added_stations', 'magnitude', 'basis'),
    }
    ACTION_REQUIRED = {
        ('station', 'offline'): ('reason',),
        ('event', 'review'): ('reviewer', 'magnitude'),
        ('event', 'publish'): ('communication_id',),
        ('event', 'withdraw'): ('reason',),
    }
    CREATE_ROLES = {
        'station': ('admin', 'station'),
        'event': ('admin', 'analyst'),
        'revision_order': ('admin', 'reviewer'),
    }
    ROLE_ACTIONS = {
        'offline': ('admin', 'station'),
        'online': ('admin', 'station'),
        'associate': ('admin', 'analyst'),
        'review': ('admin', 'reviewer'),
        'publish': ('admin', 'reviewer'),
        'withdraw': ('admin', 'reviewer'),
        ('revision_order', 'approve'): ('admin', 'reviewer'),
        ('revision_order', 'reject'): ('admin', 'reviewer'),
        ('revision_order', 'withdraw'): ('admin', 'reviewer'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
