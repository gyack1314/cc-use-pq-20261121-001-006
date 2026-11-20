import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .rules import canonical, check, digest, integer


DDL = """
CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES('schema',1);
INSERT OR IGNORE INTO meta VALUES('catalog',0);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS catalog(kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
 payload TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS catalog_history(version INTEGER NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,
 revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(version,kind,id));
CREATE TABLE IF NOT EXISTS batches(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL REFERENCES sources(id),first_offset INTEGER NOT NULL,last_offset INTEGER NOT NULL,
 digest TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_events(source TEXT NOT NULL,event_id TEXT NOT NULL,revision INTEGER NOT NULL,
 offset INTEGER NOT NULL,seq INTEGER NOT NULL REFERENCES batches(seq),kind TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(source,event_id,revision),UNIQUE(source,offset));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,cutoff INTEGER NOT NULL,catalog_version INTEGER NOT NULL,
 digest TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
 parent TEXT REFERENCES releases(id),digest TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS head(singleton INTEGER PRIMARY KEY CHECK(singleton=1),release_id TEXT NOT NULL REFERENCES releases(id));
"""


class State:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(DDL)
        from .warehouse.consumer import CONSUMER_DDL
        self.db.executescript(CONSUMER_DDL)
        check(self.db.execute("SELECT value FROM meta WHERE name='schema'").fetchone()[0] == 1,
              "schema", "不支持的仓库元数据版本")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, write=False):
        check(not self.db.in_transaction, "transaction", "业务事务不能嵌套")
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def watermark(self):
        return self.db.execute("SELECT COALESCE(MAX(seq),0) FROM batches").fetchone()[0]

    def catalog_version(self):
        return self.db.execute("SELECT value FROM meta WHERE name='catalog'").fetchone()[0]

    def dimensions(self, version=None):
        current = self.catalog_version()
        version = current if version is None else integer(version, "维度版本")
        check(version <= current, "version", "维度版本尚不存在")
        rows = self.db.execute("""
          SELECT h.* FROM catalog_history h JOIN (
           SELECT kind,id,MAX(version) version FROM catalog_history WHERE version<=? GROUP BY kind,id
          ) latest USING(kind,id,version) ORDER BY kind,id
          """, (version,))
        result = {"venue": {}, "session": {}, "alias": {}, "channel": {}}
        for row in rows:
            result[row["kind"]][row["id"]] = json.loads(row["payload"])
        return result

    def events(self, cutoff=None):
        current = self.watermark()
        cutoff = current if cutoff is None else integer(cutoff, "原始批次水位")
        check(cutoff <= current, "version", "原始批次水位尚不存在")
        rows = self.db.execute("""
          SELECT r.* FROM raw_events r JOIN (
           SELECT source,event_id,MAX(revision) revision FROM raw_events WHERE seq<=? GROUP BY source,event_id
          ) latest USING(source,event_id,revision) ORDER BY r.source,r.offset
          """, (cutoff,))
        return [{**json.loads(row["payload"]), "source": row["source"], "seq": row["seq"],
                 "identity": row["source"] + ":" + row["event_id"]} for row in rows]

    def source_vector(self, cutoff):
        return [dict(row) for row in self.db.execute("""
          SELECT source,MAX(last_offset) cursor FROM batches WHERE seq<=? GROUP BY source ORDER BY source
          """, (cutoff,))]

    def raw_history(self, source, event_id):
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.db.execute(
            "SELECT revision,offset,seq,kind,payload FROM raw_events WHERE source=? AND event_id=? ORDER BY revision",
            (source, event_id))]

    def integrity(self):
        check(self.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity", "仓库存储完整性失败")
        check(not self.db.execute("PRAGMA foreign_key_check").fetchall(), "integrity", "仓库引用完整性失败")
        for row in self.db.execute("SELECT digest,payload FROM batches"):
            check(digest(json.loads(row["payload"])) == row["digest"], "integrity", "原始资料包摘要被改变")
        return {"status": "ok", "cutoff": self.watermark(), "catalog_version": self.catalog_version()}
