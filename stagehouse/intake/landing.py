import json

from ..rules import canonical, check, digest, fields, key, label
from .protocol import normalize_batch


class Landing:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def register(self, value):
        fields(value, ("id", "name"))
        source, name = key(value["id"]), label(value["name"])
        old = self.db.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone()
        if old:
            check(old["name"] == name, "source", "来源编号已经用于其他名称")
            return dict(old)
        self.db.execute("INSERT INTO sources(id,name) VALUES(?,?)", (source, name))
        return {"id": source, "name": name, "cursor": 0}

    def ingest(self, value):
        normalized = normalize_batch(value)
        source = normalized["source"]
        old = self.db.execute("SELECT seq,digest FROM batches WHERE id=?", (normalized["id"],)).fetchone()
        signature = digest(value)
        if old:
            check(old["digest"] == signature, "idempotency", "同一批次编号的原始内容发生变化")
            return {"seq": old["seq"], "replayed": True}
        cursor = self.db.execute("SELECT cursor FROM sources WHERE id=?", (source,)).fetchone()
        check(cursor is not None, "source", "资料来源尚未登记")
        check(normalized["first_offset"] == cursor[0] + 1, "cursor", "来源位点不连续", expected=cursor[0] + 1)
        inserted = self.db.execute("INSERT INTO batches(id,source,first_offset,last_offset,digest,payload) VALUES(?,?,?,?,?,?)",
                                   (normalized["id"], source, normalized["first_offset"], normalized["last_offset"],
                                    signature, canonical(value)))
        seq = inserted.lastrowid
        for event in normalized["events"]:
            latest = self.db.execute("SELECT revision,kind FROM raw_events WHERE source=? AND event_id=? ORDER BY revision DESC LIMIT 1",
                                     (source, event["id"])).fetchone()
            check(event["revision"] == (latest["revision"] + 1 if latest else 1), "revision", "事件修订必须连续递增")
            check(not latest or latest["kind"] == event["kind"], "kind", "事件修订不能改变事实类型")
            self.db.execute("INSERT INTO raw_events VALUES(?,?,?,?,?,?,?)", (source, event["id"], event["revision"],
                            event["offset"], seq, event["kind"], canonical(event)))
        self.db.execute("UPDATE sources SET cursor=? WHERE id=?", (normalized["last_offset"], source))
        return {"seq": seq, "replayed": False, "events": len(normalized["events"]), "cursor": normalized["last_offset"]}

    def batches(self, source=None):
        rows = self.db.execute("SELECT seq,id,source,first_offset,last_offset,digest FROM batches ORDER BY seq")
        return [dict(row) for row in rows if source is None or row["source"] == source]
