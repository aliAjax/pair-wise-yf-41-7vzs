from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
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
    """提交待审修订单时必须写明新增台站、修订震级和修订依据。"""
    event_id = data.get("event_id")
    if not event_id:
        raise ValidationError("修订单必须关联事件 event_id")
    events = lookup("event", "id", event_id) if lookup else None
    if not events:
        raise ValidationError("关联事件不存在: " + str(event_id))
    event = events[0]
    if event["status"] not in ("published", "revised"):
        raise InvalidTransition(
            "仅已发布或已修订的事件可提交修订单，当前事件状态: " + event["status"]
        )

    stations = data.get("new_stations")
    if not isinstance(stations, list) or not stations:
        raise ValidationError("修订单必须写明新增台站 new_stations（至少一个）")
    normalized = []
    for item in stations:
        if isinstance(item, str):
            if not item.strip():
                raise ValidationError("新增台站代码不能为空")
            normalized.append({"station": item.strip()})
        elif isinstance(item, dict) and item.get("station"):
            normalized.append(dict(item))
        else:
            raise ValidationError("新增台站必须包含 station 代码")

    magnitude = data.get("magnitude")
    if magnitude is None:
        raise ValidationError("修订单必须写明修订震级 magnitude")
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("修订震级必须是数字")
    if not 0.0 <= magnitude <= 10.0:
        raise ValidationError("修订震级超出合理范围(0~10)")

    basis = data.get("basis")
    if not basis or not str(basis).strip():
        raise ValidationError("修订单必须写明修订依据 basis")

    data["new_stations"] = normalized
    data["magnitude"] = magnitude
    # 快照提交时事件的版本与震级，审批据此判断版本是否已过期
    data["base_version"] = event["version"]
    data["base_magnitude"] = event["data"].get("magnitude")
    data["base_status"] = event["status"]
    data.setdefault("revision_count", 0)
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


CUSTOM_CREATE = {
    'station': _validate_station,
    'event': _validate_event,
    'revision_order': _validate_revision_order,
}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'revision-orders': 'revision_order', 'revision_orders': 'revision_order'}
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
            'withdraw': (('published', 'revised'), 'withdrawn'),
        },
        'revision_order': {
            'approve': (('pending',), 'approved'),
            'withdraw': (('pending',), 'withdrawn'),
        },
    }
    CREATE_REQUIRED = {
        'station': ('code', 'lat', 'lon'),
        'event': ('title', 'origin_time', 'location', 'reports'),
        'revision_order': ('event_id', 'new_stations', 'magnitude', 'basis'),
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
        'revision_order': ('admin', 'analyst', 'reviewer'),
    }
    ROLE_ACTIONS = {
        'offline': ('admin', 'station'),
        'online': ('admin', 'station'),
        'associate': ('admin', 'analyst'),
        'review': ('admin', 'reviewer'),
        'publish': ('admin', 'reviewer'),
        'withdraw': ('admin', 'reviewer', 'analyst'),
        # 修订单：提交人可撤回；只有复核人员/管理员可审批
        ('revision_order', 'approve'): ('admin', 'reviewer'),
        ('revision_order', 'withdraw'): ('admin', 'analyst', 'reviewer'),
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
