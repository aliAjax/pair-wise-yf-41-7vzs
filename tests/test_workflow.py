import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'station', 'kind': 'station', 'data': {'code': 'STA-1', 'lat': 35.0, 'lon': 110.0}}, {'op': 'create', 'as': 'event', 'kind': 'event', 'data': {'title': 'Event-A', 'origin_time': '2026-01-01T00:00:00Z', 'location': 'Region-A', 'reports': [{'station': 'STA-1', 'time_offset': 2, 'distance_km': 1.0}, {'station': 'STA-2', 'time_offset': -1, 'distance_km': 1.5}]}}, {'op': 'transition', 'target': 'event', 'action': 'associate', 'data': {}, 'expect': 'associated'}, {'op': 'transition', 'target': 'event', 'action': 'review', 'data': {'reviewer': 'R-1', 'magnitude': 4.2}, 'expect': 'reviewed'}, {'op': 'transition', 'target': 'event', 'action': 'publish', 'data': {'communication_id': 'C-1'}, 'expect': 'published'}, {'op': 'create', 'as': 'order', 'kind': 'revision_order', 'data': {'event_id': '{event}', 'added_stations': [{'station': 'STA-3', 'time_offset': 1, 'distance_km': 2.0}], 'magnitude': 4.3, 'basis': 'late station report'}}, {'op': 'transition', 'target': 'order', 'action': 'approve', 'data': {}, 'expect': 'approved'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

        event = self.service.get(created["event"])
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["data"]["magnitude"], 4.3)
        self.assertEqual(event["data"]["revision_count"], 1)
        self.assertEqual(len(event["data"]["reports"]), 3)
        versions = self.service.versions(created["event"])
        self.assertEqual([v["version"] for v in versions], [1, 2, 3, 4, 5])
        self.assertEqual(versions[-1]["data"]["magnitude"], 4.3)


if __name__ == "__main__":
    unittest.main()
