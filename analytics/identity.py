"""解析外部实体名称与内部分析主体。"""
class EntityDirectory:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY, label TEXT NOT NULL, group_id TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS aliases (
            source TEXT NOT NULL, external_id TEXT NOT NULL, entity_id TEXT NOT NULL,
            PRIMARY KEY(source, external_id), FOREIGN KEY(entity_id) REFERENCES entities(entity_id))""")

    def register(self, entity_id, label, group_id):
        if not all(isinstance(v, str) and v.strip() for v in (entity_id,label,group_id)):
            raise ValueError("实体信息不能为空")
        prior = self.connection.execute("SELECT label,group_id FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if prior:
            if tuple(prior) != (label, group_id):
                raise ValueError("实体身份已登记为其他内容")
            return False
        self.connection.execute("INSERT INTO entities VALUES(?,?,?)", (entity_id,label,group_id))
        return True

    def link(self, source, external_id, entity_id):
        exists = self.connection.execute("SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if not exists:
            raise ValueError("未知分析主体")
        prior = self.connection.execute("SELECT entity_id FROM aliases WHERE source=? AND external_id=?", (source,external_id)).fetchone()
        if prior:
            if prior[0] != entity_id:
                raise ValueError("外部身份与现有主体冲突")
            return False
        self.connection.execute("INSERT INTO aliases VALUES(?,?,?)", (source,external_id,entity_id))
        return True

    def resolve(self, source, external_id):
        row = self.connection.execute("SELECT entity_id FROM aliases WHERE source=? AND external_id=?", (source,external_id)).fetchone()
        return row[0] if row else None
