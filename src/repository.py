import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS entity_versions (
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_versions_entity
                    ON entity_versions(entity_id, version);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)
            # Backfill version snapshots for databases created before the
            # history table existed, so every current row is preserved too.
            connection.execute("""
                INSERT OR IGNORE INTO entity_versions
                    (entity_id, version, kind, status, data, created_by, archived_at)
                SELECT id, version, kind, status, data, created_by, updated_at
                FROM entities
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
            # 历史编目按版本保留：创建即归档第 1 版
            connection.execute(
                "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, archived_at) "
                "VALUES (?, 1, ?, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            next_version = current_version + 1
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            # 归档写后的新版本，旧版本行保持不变
            connection.execute(
                "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, archived_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, next_version, row["kind"], status, payload, row["created_by"], now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def apply_revision_order(self, *, order_id, event_id, base_version, event_data, order_data):
        """在单个事务内审批修订单：校验版本未过期后同时更新事件与修订单。

        事件已被其他修订推进到新版本时抛出 ConflictError（版本已过期），
        不会覆盖新数据，修订单也保持待审。
        """
        now = utcnow()
        event_payload = json.dumps(event_data, ensure_ascii=False, sort_keys=True)
        order_payload = json.dumps(order_data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            order_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (order_id,)
            ).fetchone()
            if not order_row:
                raise NotFoundError("entity not found: " + order_id)
            if order_row["kind"] != "revision_order" or order_row["status"] != "pending":
                raise InvalidTransition("修订单当前状态不可审批: " + order_row["status"])
            event_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (event_id,)
            ).fetchone()
            if not event_row:
                raise NotFoundError("entity not found: " + event_id)
            current_event_version = int(event_row["version"])
            if current_event_version != int(base_version):
                raise ConflictError(
                    "修订单版本已过期：事件已被修订到第 %s 版（修订单基于第 %s 版），"
                    "不能覆盖新数据；请基于最新版本重新提交修订单"
                    % (current_event_version, base_version)
                )
            if event_row["status"] not in ("published", "revised"):
                raise InvalidTransition("事件当前状态 %s 不允许修订" % event_row["status"])

            next_event_version = current_event_version + 1
            connection.execute(
                "UPDATE entities SET status = 'revised', version = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (next_event_version, event_payload, now, event_id),
            )
            connection.execute(
                "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, archived_at) "
                "VALUES (?, ?, 'event', 'revised', ?, ?, ?)",
                (event_id, next_event_version, event_payload, event_row["created_by"], now),
            )

            next_order_version = int(order_row["version"]) + 1
            connection.execute(
                "UPDATE entities SET status = 'approved', version = ?, data = ?, updated_at = ? "
                "WHERE id = ?",
                (next_order_version, order_payload, now, order_id),
            )
            connection.execute(
                "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, archived_at) "
                "VALUES (?, ?, 'revision_order', 'approved', ?, ?, ?)",
                (order_id, next_order_version, order_payload, order_row["created_by"], now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id), self.get_entity(event_id)

    def list_versions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_versions WHERE entity_id = ? ORDER BY version",
                (entity_id,),
            ).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "version": int(row["version"]),
                "kind": row["kind"],
                "status": row["status"],
                "data": json.loads(row["data"]),
                "created_by": row["created_by"],
                "archived_at": row["archived_at"],
            }
            for row in rows
        ]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
