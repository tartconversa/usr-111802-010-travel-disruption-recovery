"""核验当前事件修订、主体识别与发布快照行为。"""
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService

BASE = {"source":"system", "event_id":"e1", "revision":1,
        "kind":None, "entity":"external-1", "occurred_at":"2026-11-01T10:00:00+08:00",
        "observed_at":"2026-11-01T11:00:00+08:00", "payload":{"amount":3}}

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name)/"data.sqlite3")
        self.service.register("entity-1", "主体一", "group-1")
        self.service.link("system", "external-1", "entity-1")
        self.event = dict(BASE, kind=self.service.config["event_kinds"][0])

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_revision_and_asof(self):
        self.assertTrue(self.service.ingest(self.event))
        self.assertFalse(self.service.ingest(self.event))
        changed = dict(self.event, revision=2, observed_at="2026-11-02T11:00:00+08:00", payload={"amount":5})
        self.service.ingest(changed)
        self.assertEqual(self.service.summary()["subjects"]["entity-1"]["amount"], "5")
        self.assertEqual(self.service.summary("2026-11-01T12:00:00+08:00")["subjects"]["entity-1"]["amount"], "3")
        with self.assertRaises(ValueError):
            self.service.ingest(dict(changed, payload={"amount":8}))

    def test_unmatched_and_report(self):
        unknown = dict(self.event, entity="missing")
        self.service.ingest(unknown)
        self.assertEqual(len(self.service.summary()["unmatched"]), 1)
        first = self.service.publish("report-1", "2026-11-03", "月度观察")
        self.service.link("system", "missing", "entity-1")
        self.assertEqual(first, self.service.decisions.get("report-1"))
        with self.assertRaises(ValueError):
            self.service.publish("report-1", "2026-11-03", "新的观察")

    def test_invalid_event(self):
        with self.assertRaises(ValueError):
            self.service.ingest(dict(self.event, kind="unknown"))
        with self.assertRaises(ValueError):
            self.service.ingest(dict(self.event, occurred_at="yesterday"))

if __name__ == "__main__":
    unittest.main()
