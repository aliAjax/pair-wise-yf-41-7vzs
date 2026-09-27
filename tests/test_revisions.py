import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class RevisionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("ana-1", "analyst")
        self.reviewer = Actor("rev-1", "reviewer")
        self._publish_event(magnitude=4.2)

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_event(self, magnitude=4.2):
        self.service.create(
            self.admin,
            "station",
            {"code": "STA-1", "lat": 35.0, "lon": 110.0},
        )
        self.event = self.service.create(
            self.analyst,
            "event",
            {
                "title": "Event-A",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-A",
                "reports": [
                    {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
                ],
            },
        )
        self.service.transition(self.admin, self.event["id"], "associate", {})
        self.service.transition(
            self.admin, self.event["id"], "review",
            {"reviewer": "rev-1", "magnitude": magnitude},
        )
        self.service.transition(
            self.admin, self.event["id"], "publish",
            {"communication_id": "C-1"},
        )
        self.event = self.service.get(self.event["id"])
        return self.event

    def _submit_order(self, actor=None, magnitude=4.4, basis="STA-3 补报，重新标定"):
        return self.service.create(
            actor or self.analyst,
            "revision_order",
            {
                "event_id": self.event["id"],
                "new_stations": [{"station": "STA-3", "time_offset": 3, "distance_km": 2.1}],
                "magnitude": magnitude,
                "basis": basis,
            },
        )

    def test_submit_requires_stations_magnitude_and_basis(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst, "revision_order",
                {"event_id": self.event["id"], "new_stations": [], "magnitude": 4.4, "basis": "x"},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst, "revision_order",
                {"event_id": self.event["id"],
                 "new_stations": [{"station": "STA-3"}], "magnitude": None, "basis": "x"},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst, "revision_order",
                {"event_id": self.event["id"],
                 "new_stations": [{"station": "STA-3"}], "magnitude": 4.4, "basis": ""},
            )

    def test_order_snapshots_base_version_on_submit(self):
        order = self._submit_order()
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["data"]["base_version"], self.event["version"])
        self.assertEqual(order["data"]["base_magnitude"], 4.2)

    def test_approve_updates_event_magnitude_count_and_versions_kept(self):
        order = self._submit_order(magnitude=4.4)
        approved = self.service.transition(self.reviewer, order["id"], "approve", {})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["data"]["approver"], "rev-1")

        event = self.service.get(self.event["id"])
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["data"]["magnitude"], 4.4)
        self.assertEqual(event["data"]["revision_count"], 1)
        self.assertEqual(event["data"]["last_revision_basis"], "STA-3 补报，重新标定")
        stations = [r["station"] for r in event["data"]["reports"]]
        self.assertEqual(stations, ["STA-1", "STA-2", "STA-3"])

        versions = self.service.versions(self.event["id"])
        self.assertEqual(versions[-1]["version"], event["version"])
        self.assertEqual(versions[-1]["data"]["magnitude"], 4.4)
        # 旧版编目仍可查到发布时的震级与依据之外的原始内容
        published = versions[-2]
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["data"]["magnitude"], 4.2)
        self.assertNotIn("STA-3", [r["station"] for r in published["data"]["reports"]])

    def test_second_revision_increments_count_and_keeps_all_versions(self):
        first = self._submit_order(magnitude=4.4)
        self.service.transition(self.reviewer, first["id"], "approve", {})
        event = self.service.get(self.event["id"])
        second = self.service.create(
            self.analyst, "revision_order",
            {
                "event_id": self.event["id"],
                "new_stations": ["STA-4"],
                "magnitude": 4.6,
                "basis": "STA-4 补报后再次标定",
            },
        )
        self.service.transition(self.reviewer, second["id"], "approve", {})
        event = self.service.get(self.event["id"])
        self.assertEqual(event["data"]["magnitude"], 4.6)
        self.assertEqual(event["data"]["revision_count"], 2)
        self.assertIn("STA-4", [r["station"] for r in event["data"]["reports"]])
        versions = self.service.versions(self.event["id"])
        self.assertEqual(len(versions), event["version"])

    def test_stale_order_approval_rejected_and_event_preserved(self):
        stale = self._submit_order(magnitude=4.4)

        # 事件在旧修订单等待期间又被另一张修订单修订
        newer = self.service.create(
            self.analyst, "revision_order",
            {
                "event_id": self.event["id"],
                "new_stations": ["STA-9"],
                "magnitude": 4.5,
                "basis": "STA-9 先到的补报",
            },
        )
        self.service.transition(self.reviewer, newer["id"], "approve", {})

        event_before = self.service.get(self.event["id"])
        with self.assertRaises(InvalidTransition) as ctx:
            self.service.transition(self.reviewer, stale["id"], "approve", {})
        self.assertIn("版本已过期", str(ctx.exception))

        # 过期审批不能覆盖新数据
        event_after = self.service.get(self.event["id"])
        self.assertEqual(event_after["version"], event_before["version"])
        self.assertEqual(event_after["data"]["magnitude"], 4.5)
        stale_order = self.service.get(stale["id"])
        self.assertEqual(stale_order["status"], "pending")

        # 乐观锁同样拦截过期基线的事务提交（并发兜底）
        guard = self.service.create(
            self.analyst, "revision_order",
            {
                "event_id": self.event["id"],
                "new_stations": ["STA-8"],
                "magnitude": 4.7,
                "basis": "并发兜底检查",
            },
        )
        with self.assertRaises(ConflictError):
            self.repo.apply_revision_order(
                order_id=guard["id"],
                event_id=self.event["id"],
                base_version=1,
                event_data=event_after["data"],
                order_data=dict(guard["data"]),
            )
        # 兜底回滚后修订单仍待审、事件仍为新版本
        self.assertEqual(self.service.get(guard["id"])["status"], "pending")
        self.assertEqual(self.service.get(self.event["id"])["version"], event_after["version"])

    def test_withdraw_keeps_event_untouched(self):
        order = self._submit_order(magnitude=4.4)
        event_before = self.service.get(self.event["id"])
        withdrawn = self.service.transition(self.analyst, order["id"], "withdraw", {})
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["data"]["withdrawn_by"], "ana-1")

        event_after = self.service.get(self.event["id"])
        self.assertEqual(event_after["version"], event_before["version"])
        self.assertEqual(event_after["status"], "published")
        self.assertEqual(event_after["data"]["magnitude"], 4.2)
        self.assertNotIn("STA-3", [r["station"] for r in event_after["data"]["reports"]])

        with self.assertRaises(InvalidTransition):
            self.service.transition(self.reviewer, order["id"], "approve", {})

    def test_analyst_cannot_approve(self):
        order = self._submit_order()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, order["id"], "approve", {})

    def test_cannot_submit_order_before_publish(self):
        fresh = self.service.create(
            self.analyst,
            "event",
            {
                "title": "Event-B",
                "origin_time": "2026-01-02T00:00:00Z",
                "location": "Region-B",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 0, "distance_km": 1.2},
                ],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.create(
                self.analyst, "revision_order",
                {"event_id": fresh["id"], "new_stations": ["STA-5"],
                 "magnitude": 3.0, "basis": "补报"},
            )


if __name__ == "__main__":
    unittest.main()
