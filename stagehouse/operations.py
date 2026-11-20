import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .rules import canonical, check, digest
from .warehouse.contracts import WarehouseContract
from .warehouse.versions import Versions


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".stagehouse-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(canonical(value) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Operations:
    def __init__(self, state):
        self.state = state

    def audit(self):
        self.state.integrity()
        sources = self.state.db.execute("SELECT id,cursor FROM sources ORDER BY id").fetchall()
        for source in sources:
            previous = 0
            batches = self.state.db.execute("SELECT first_offset,last_offset FROM batches WHERE source=? ORDER BY seq", (source["id"],))
            for batch in batches:
                check(batch["first_offset"] == previous + 1, "integrity", "来源批次位点存在断层")
                previous = batch["last_offset"]
            check(previous == source["cursor"], "integrity", "来源已确认游标与原始资料不符")
        contract = WarehouseContract()
        for row in self.state.db.execute("SELECT id FROM runs"):
            run = Versions(self.state).run(row[0])
            contract.require(run["content"]["result"])
        prior = None
        count = 0
        for row in self.state.db.execute("SELECT id,parent,run_id FROM releases ORDER BY rowid"):
            release = Versions(self.state).release(row["id"])
            check(row["parent"] == prior, "integrity", "消费版本前序链断裂")
            run = Versions(self.state).run(row["run_id"])
            check(release["content"]["snapshot"] == run["content"], "integrity", "消费版本与候选不一致")
            check(release["content"]["run_digest"] == run["digest"], "integrity", "消费版本与候选摘要不一致")
            contract.require(release["content"]["snapshot"]["result"])
            prior = row["id"]
            count += 1
        check(prior == Versions(self.state).head(), "integrity", "当前消费版本指针不符")
        return {"status": "verified", "cutoff": self.state.watermark(), "catalog_version": self.state.catalog_version(),
                "sources": len(sources), "releases": count}

    def export(self, release_id, destination):
        release = Versions(self.state).release(release_id)
        envelope = {"content": release["content"], "digest": release["digest"]}
        atomic_json(destination, envelope)
        return {"path": str(destination), "digest": release["digest"]}

    @staticmethod
    def verify_file(path):
        from .rules import Problem
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Problem("archive", "消费归档文件不可读") from exc
        check(isinstance(value, dict) and set(value) == {"content", "digest"}, "archive", "归档结构无效")
        check(digest(value["content"]) == value["digest"], "digest", "归档摘要校验失败")
        WarehouseContract().require(value["content"]["snapshot"]["result"])
        return {"valid": True, "release": value["content"]["release"], "digest": value["digest"]}

    def backup(self, destination):
        target = Path(destination).resolve()
        check(self.state.path == ":memory:" or target != Path(self.state.path).resolve(), "backup", "不能备份到当前数据账")
        check(not target.exists(), "backup", "备份目标已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".warehouse-", dir=target.parent)
        os.close(fd)
        try:
            target_db = sqlite3.connect(temporary)
            try:
                self.state.db.backup(target_db)
                check(target_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup", "备份完整性失败")
                target_db.commit()
            finally:
                target_db.close()
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": str(target), "bytes": target.stat().st_size}
