"""协调来源事件、主体目录、现有摘要、衔接风险分析和报告发布。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import JourneyDirectory
from .connections import ConnectionRisk, notification_batches

class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = JourneyDirectory(self.connection)
        analysis = self.config.get("analysis", {})
        self.risk = ConnectionRisk(self.store, self.entities, self.domain, analysis)
        self.handling_list_kind = analysis.get("handling_list_kind", "handling-list")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source,external_id,entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def add_leg(self, journey, leg_no, train, station):
        with self.connection:
            return self.domain.add_leg(journey,leg_no,train,station)

    def set_interchange(self, station, minutes):
        with self.connection:
            return self.domain.set_interchange(station,minutes)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def assess_connections(self, as_of=None):
        """评估当前联程衔接风险：断点、触发时刻变化、仍需处置旅客与席位覆盖。"""
        return self.risk.assess(as_of)

    def publish_handling_list(self, list_id, created_at, as_of=None):
        """下发处置清单：固化当前评估与当时依据，同编号不接受不同内容。"""
        body = self.risk.assess(as_of)
        with self.connection:
            return self.decisions.publish(list_id, created_at, "衔接风险处置清单", body,
                                          kind=self.handling_list_kind)

    def handling_delta(self, as_of=None):
        """用最新运行消息更新当前清单，并给出需要重新通知的旅客批次。"""
        current = self.risk.assess(as_of)
        published = self.decisions.latest(self.handling_list_kind)
        batches = notification_batches(published["body"] if published else None, current)
        basis_list = None
        if published:
            basis_list = {"list_id": published["report_id"],
                          "created_at": published["created_at"],
                          "as_of": published["body"]["as_of"]}
        return {"as_of": current["as_of"], "basis_list": basis_list,
                "batches": batches, "current": current}

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)
