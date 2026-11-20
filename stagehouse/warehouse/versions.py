import json

from ..rules import canonical, check, digest, key
from .build import Builder
from .contracts import WarehouseContract


class Versions:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def prepare(self, run_id):
        key(run_id, "运行编号")
        cutoff, version = self.state.watermark(), self.state.catalog_version()
        result = Builder(self.state.dimensions(version)).build(self.state.events(cutoff))
        WarehouseContract().require(result)
        content = {"run": run_id, "cutoff": cutoff, "catalog_version": version,
                   "sources": self.state.source_vector(cutoff), "result": result}
        signature = digest(content)
        old = self.db.execute("SELECT digest FROM runs WHERE id=?", (run_id,)).fetchone()
        if old:
            check(old["digest"] == signature, "run_conflict", "运行编号不能覆盖其他输入版本")
            return self.run(run_id)
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (run_id, cutoff, version, signature, canonical(content)))
        return self.run(run_id)

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        check(row is not None, "run", "候选运行不存在")
        content = json.loads(row["payload"])
        check(digest(content) == row["digest"], "digest", "候选内容摘要不符")
        return {"content": content, "digest": row["digest"],
                "stale": row["cutoff"] != self.state.watermark() or row["catalog_version"] != self.state.catalog_version()}

    def head(self):
        row = self.db.execute("SELECT release_id FROM head WHERE singleton=1").fetchone()
        return row[0] if row else None

    def publish(self, value):
        from ..rules import fields
        fields(value, ("id", "run", "run_digest", "expected_head"))
        release_id, run_id = key(value["id"]), key(value["run"])
        previous = self.db.execute("SELECT run_id,digest,payload FROM releases WHERE id=?", (release_id,)).fetchone()
        if previous:
            content = json.loads(previous["payload"])
            check(previous["run_id"] == run_id and content["run_digest"] == value["run_digest"]
                  and content["parent"] == value["expected_head"], "release_conflict", "发布重试内容不一致")
            return self.release(release_id)
        candidate = self.run(run_id)
        check(candidate["digest"] == value["run_digest"], "digest", "请求的候选摘要不一致")
        check(not candidate["stale"], "stale", "资料或维度已有更新，请建立新候选")
        check(candidate["content"]["result"]["publishable"], "quality", "隔离问题未解决，候选不能切为消费版本",
              issues=candidate["content"]["result"]["issues"])
        check(self.head() == value["expected_head"], "head_conflict", "消费版本已被其他发布推进", actual=self.head())
        check(not self.db.execute("SELECT 1 FROM releases WHERE run_id=?", (run_id,)).fetchone(),
              "released", "同一候选不能重复签发为不同版本")
        content = {"release": release_id, "parent": self.head(), "run_digest": candidate["digest"],
                   "snapshot": candidate["content"]}
        signature = digest(content)
        self.db.execute("INSERT INTO releases VALUES(?,?,?,?,?)", (release_id, run_id, self.head(), signature, canonical(content)))
        self.db.execute("INSERT INTO head VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id", (release_id,))
        return self.release(release_id)

    def release(self, release_id=None):
        release_id = self.head() if release_id is None else release_id
        check(release_id is not None, "release", "尚无消费版本")
        row = self.db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        check(row is not None, "release", "消费版本不存在")
        content = json.loads(row["payload"])
        check(digest(content) == row["digest"], "digest", "消费版本内容被改变")
        snapshot = content["snapshot"]
        return {"content": content, "digest": row["digest"], "is_head": release_id == self.head(),
                "current_inputs": snapshot["cutoff"] == self.state.watermark()
                and snapshot["catalog_version"] == self.state.catalog_version()}

    def list(self):
        return [{"id": row["id"], "run": row["run_id"], "parent": row["parent"], "digest": row["digest"],
                 "is_head": row["id"] == self.head()} for row in self.db.execute("SELECT * FROM releases ORDER BY rowid")]
