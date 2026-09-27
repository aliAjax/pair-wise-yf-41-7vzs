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


class RevisionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.reviewer = Actor("rev-1", "reviewer")
        self.other_reviewer = Actor("rev-2", "reviewer")
        self.event_id = self._publish_event()

    def tearDown(self):
        self.tmp.cleanup()

    def _publish_event(self, magnitude=4.2):
        actor = Actor("admin", "admin")
        self.service.create(
            actor,
            "station",
            {"code": "STA-1", "lat": 35.0, "lon": 110.0},
        )
        event = self.service.create(
            actor,
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
        event_id = event["id"]
        self.service.transition(Actor("admin", "admin"), event_id, "associate", {})
        self.service.transition(
            Actor("admin", "admin"),
            event_id,
            "review",
            {"reviewer": "R-1", "magnitude": magnitude},
        )
        self.service.transition(
            Actor("admin", "admin"),
            event_id,
            "publish",
            {"communication_id": "C-1"},
        )
        return event_id

    def _open_order(self, magnitude=4.3, station="STA-3", basis="补报台站震相重算震级"):
        return self.service.create(
            self.reviewer,
            "revision_order",
            {
                "event_id": self.event_id,
                "added_stations": [
                    {"station": station, "time_offset": 1, "distance_km": 2.0}
                ],
                "magnitude": magnitude,
                "basis": basis,
            },
        )

    def test_order_requires_station_magnitude_and_basis(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.reviewer,
                "revision_order",
                {
                    "event_id": self.event_id,
                    "added_stations": [],
                    "magnitude": 4.3,
                    "basis": "x",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.reviewer,
                "revision_order",
                {
                    "event_id": self.event_id,
                    "added_stations": [{"station": "STA-3"}],
                    "magnitude": 4.3,
                    "basis": "",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.reviewer,
                "revision_order",
                {
                    "event_id": self.event_id,
                    "added_stations": [{"station": "STA-3"}],
                    "magnitude": "bad",
                    "basis": "x",
                },
            )

    def test_analyst_cannot_open_or_approve_order(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("ana-1", "analyst"),
                "revision_order",
                {
                    "event_id": self.event_id,
                    "added_stations": [{"station": "STA-3"}],
                    "magnitude": 4.3,
                    "basis": "x",
                },
            )

    def test_approve_applies_magnitude_station_count_and_versions(self):
        order = self._open_order()
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["data"]["event_base_version"], 4)

        approved = self.service.transition(
            self.other_reviewer, order["id"], "approve", {}
        )
        self.assertEqual(approved["status"], "approved")

        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["data"]["magnitude"], 4.3)
        self.assertEqual(event["data"]["revision_count"], 1)
        self.assertEqual(len(event["data"]["reports"]), 3)
        self.assertIn("STA-3", [r["station"] for r in event["data"]["reports"]])
        self.assertEqual(event["data"]["last_revision"]["order_id"], order["id"])
        self.assertEqual(event["data"]["last_revision"]["basis"], "补报台站震相重算震级")
        self.assertEqual(event["data"]["last_revision"]["approved_by"], "rev-2")

        versions = self.service.versions(self.event_id)
        self.assertEqual(versions[-1]["version"], event["version"])
        old = versions[-2]
        self.assertEqual(old["status"], "published")
        self.assertEqual(old["data"]["magnitude"], 4.2)
        self.assertNotIn("revision_count", old["data"])

    def test_second_approval_increments_revision_count_and_keeps_history(self):
        first = self._open_order(magnitude=4.3, station="STA-3")
        self.service.transition(self.reviewer, first["id"], "approve", {})

        second = self._open_order(magnitude=4.5, station="STA-4", basis="第二批补报")
        self.service.transition(self.reviewer, second["id"], "approve", {})

        event = self.service.get(self.event_id)
        self.assertEqual(event["data"]["magnitude"], 4.5)
        self.assertEqual(event["data"]["revision_count"], 2)
        self.assertEqual(len(event["data"]["reports"]), 4)

        versions = self.service.versions(self.event_id)
        magnitudes = [v["data"].get("magnitude") for v in versions]
        self.assertIn(4.2, magnitudes)
        self.assertIn(4.3, magnitudes)
        self.assertEqual(magnitudes[-1], 4.5)

    def test_approve_stale_order_after_event_revised_is_conflict(self):
        stale_order = self._open_order(magnitude=4.3, station="STA-3")

        # 修订单提交后，事件被另一个修订单再次修订。
        newer = self._open_order(magnitude=4.4, station="STA-4", basis="先审批的修订")
        self.service.transition(self.reviewer, newer["id"], "approve", {})

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.reviewer, stale_order["id"], "approve", {})
        self.assertIn("stale", str(ctx.exception))

        # 过期审批未覆盖新数据。
        event = self.service.get(self.event_id)
        self.assertEqual(event["data"]["magnitude"], 4.4)
        self.assertEqual(event["data"]["revision_count"], 1)
        self.assertEqual(stale_order and self.service.get(stale_order["id"])["status"], "pending")

    def test_withdraw_order_leaves_event_untouched(self):
        order = self._open_order()
        withdrawn = self.service.transition(
            self.reviewer, order["id"], "withdraw", {"note": "依据待补充"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["data"].get("note"), "依据待补充")

        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["data"]["magnitude"], 4.2)
        self.assertEqual(len(event["data"]["reports"]), 2)
        self.assertNotIn("revision_count", event["data"])

        versions = self.service.versions(self.event_id)
        self.assertEqual(len(versions), 4)

        with self.assertRaises(InvalidTransition):
            self.service.transition(self.reviewer, order["id"], "approve", {})

    def test_reject_order_leaves_event_untouched(self):
        order = self._open_order()
        rejected = self.service.transition(
            self.other_reviewer, order["id"], "reject", {"reason": "台站数据不可信"}
        )
        self.assertEqual(rejected["status"], "rejected")
        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["data"]["magnitude"], 4.2)

    def test_cannot_open_order_for_unpublished_event(self):
        actor = Actor("admin", "admin")
        fresh = self.service.create(
            actor,
            "event",
            {
                "title": "Event-B",
                "origin_time": "2026-01-02T00:00:00Z",
                "location": "Region-B",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 2, "distance_km": 1.2},
                ],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.create(
                self.reviewer,
                "revision_order",
                {
                    "event_id": fresh["id"],
                    "added_stations": [{"station": "STA-3"}],
                    "magnitude": 3.0,
                    "basis": "x",
                },
            )

    def test_duplicate_station_in_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._open_order(station="STA-1")

    def test_event_has_no_direct_revise_action(self):
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer,
                self.event_id,
                "revise",
                {"reason": "x", "magnitude": 9.0},
            )

    def test_list_orders_filtered_by_event(self):
        order = self._open_order()
        items = self.service.list("revision_orders", event_id=self.event_id)
        self.assertEqual([item["id"] for item in items], [order["id"]])
        self.assertEqual(
            self.service.list("revision_orders", event_id="no-such-event"), []
        )

    def test_order_version_conflict_on_duplicated_approve(self):
        order = self._open_order()
        self.service.transition(self.reviewer, order["id"], "approve", {})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.reviewer, order["id"], "approve", {})


if __name__ == "__main__":
    unittest.main()
