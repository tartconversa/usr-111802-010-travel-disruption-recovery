"""保存已发布分析的原始内容与来源摘要。"""
import hashlib
import json

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

class DecisionBook:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, digest TEXT NOT NULL)""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
        if "kind" not in columns:
            connection.execute("ALTER TABLE reports ADD COLUMN kind TEXT NOT NULL DEFAULT 'report'")

    def publish(self, report_id, created_at, title, body, kind="report"):
        if not all(isinstance(v, str) and v.strip() for v in (report_id,created_at,title,kind)):
            raise ValueError("报告信息不能为空")
        encoded = encode(body)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        prior = self.get(report_id)
        if prior:
            if prior["title"] == title and prior["body"] == body and prior["kind"] == kind:
                return prior
            raise ValueError("报告编号已存在且内容不同")
        self.connection.execute("INSERT INTO reports VALUES(?,?,?,?,?,?)",
                                (report_id,created_at,title,encoded,digest,kind))
        return self.get(report_id)

    def get(self, report_id):
        row = self.connection.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
        return (dict(row) | {"body": json.loads(row["body"])}) if row else None

    def latest(self, kind):
        row = self.connection.execute(
            "SELECT * FROM reports WHERE kind=? ORDER BY created_at DESC, report_id DESC LIMIT 1",
            (kind,)).fetchone()
        return (dict(row) | {"body": json.loads(row["body"])}) if row else None
