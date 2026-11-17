"""保存带来源、实体与修订号的原始业务事件。"""
import json
import sqlite3
from datetime import datetime

def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须为字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.isoformat()

class EventStore:
    def __init__(self, connection, allowed_kinds):
        self.connection = connection
        self.allowed_kinds = frozenset(allowed_kinds)
        connection.execute("""CREATE TABLE IF NOT EXISTS events (
            source TEXT NOT NULL, event_id TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, entity TEXT NOT NULL, occurred_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, payload TEXT NOT NULL, deleted INTEGER NOT NULL,
            PRIMARY KEY (source, event_id, revision))""")
        connection.execute("CREATE INDEX IF NOT EXISTS event_entity ON events(entity,kind)")

    def ingest(self, event):
        if not isinstance(event, dict):
            raise ValueError("事件必须为对象")
        required = {"source", "event_id", "revision", "kind", "entity", "occurred_at", "observed_at", "payload"}
        if not required <= event.keys():
            raise ValueError("事件缺少必要字段")
        source, event_id, entity = (event[key] for key in ("source", "event_id", "entity"))
        if not all(isinstance(v, str) and v.strip() for v in (source, event_id, entity)):
            raise ValueError("事件身份不能为空")
        rev = event["revision"]
        if type(rev) is not int or rev < 1:
            raise ValueError("修订号必须为正整数")
        if event["kind"] not in self.allowed_kinds:
            raise ValueError("未知事件类型")
        if not isinstance(event["payload"], dict):
            raise ValueError("事件内容必须为对象")
        occurred, observed = timestamp(event["occurred_at"]), timestamp(event["observed_at"])
        deleted = event.get("deleted", False)
        if type(deleted) is not bool:
            raise ValueError("删除标记无效")
        payload = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True, allow_nan=False)
        values = (source, event_id, rev, event["kind"], entity, occurred, observed, payload, int(deleted))
        prior = self.connection.execute(
            "SELECT kind,entity,occurred_at,observed_at,payload,deleted FROM events WHERE source=? AND event_id=? AND revision=?",
            (source, event_id, rev)).fetchone()
        if prior is not None:
            if tuple(prior) == values[3:]:
                return False
            raise ValueError("同来源同事件同修订内容冲突")
        self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", values)
        return True

    def current(self, as_of=None):
        where = "" if as_of is None else "WHERE observed_at<=?"
        parameters = () if as_of is None else (timestamp(as_of),)
        rows = self.connection.execute(
            f"SELECT source,event_id,revision,kind,entity,occurred_at,observed_at,payload,deleted FROM events {where} "
            "ORDER BY source,event_id,revision", parameters).fetchall()
        latest = {}
        for row in rows:
            key = (row["source"], row["event_id"])
            if key not in latest or row["revision"] > latest[key]["revision"]:
                latest[key] = row
        return [dict(row) | {"payload": json.loads(row["payload"])}
                for row in latest.values() if not row["deleted"]]
