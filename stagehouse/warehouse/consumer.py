import json

from ..rules import canonical, check, digest, fields, integer, key
from .versions import Versions


CONSUMER_DDL = """
CREATE TABLE IF NOT EXISTS subscriptions(
 id TEXT PRIMARY KEY,release_id TEXT REFERENCES releases(id),generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY,subscription TEXT NOT NULL REFERENCES subscriptions(id),from_release TEXT,
 to_release TEXT NOT NULL REFERENCES releases(id),generation INTEGER NOT NULL,digest TEXT NOT NULL,
 payload TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0);
"""


class Consumer:
    def __init__(self, state):
        self.state = state
        self.db = state.db

    def register(self, identity):
        key(identity, "订阅编号")
        row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (identity,)).fetchone()
        if row:
            return dict(row)
        self.db.execute("INSERT INTO subscriptions(id) VALUES(?)", (identity,))
        return {"id": identity, "release_id": None, "generation": 0}

    def prepare(self, value):
        fields(value, ("id", "subscription", "target"))
        identity, subscription = key(value["id"]), key(value["subscription"])
        target_id = key(value["target"])
        previous = self.db.execute("SELECT * FROM deliveries WHERE id=?", (identity,)).fetchone()
        if previous:
            check(previous["subscription"] == subscription and previous["to_release"] == target_id,
                  "delivery_conflict", "投递编号已绑定不同订阅或目标")
            return self.delivery(identity)
        current = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription,)).fetchone()
        check(current is not None, "subscription", "订阅不存在")
        check(current["release_id"] != target_id, "delivery", "订阅已消费该版本")
        target = Versions(self.state).release(target_id)
        before = Versions(self.state).release(current["release_id"]) if current["release_id"] else None
        check(self.is_ancestor(current["release_id"], target_id), "delivery", "消费不能倒退或跳到其他历史链")
        old_parts = self.parts(before)
        new_parts = self.parts(target)
        replacements = []
        removed = []
        for partition_key in sorted(set(old_parts) | set(new_parts)):
            old, new = old_parts.get(partition_key), new_parts.get(partition_key)
            if old == new:
                continue
            if new is None:
                removed.append({"key": partition_key, "previous_digest": old["digest"]})
            else:
                replacements.append({"key": partition_key, "previous_digest": old["digest"] if old else None,
                                     "digest": new["digest"], "body": new["body"], "rows": new["rows"]})
        body = {"delivery": identity, "subscription": subscription, "from_release": current["release_id"],
                "to_release": target_id, "generation": current["generation"], "target_digest": target["digest"],
                "replacements": replacements, "removed": removed}
        signature = digest(body)
        self.db.execute("INSERT INTO deliveries(id,subscription,from_release,to_release,generation,digest,payload) VALUES(?,?,?,?,?,?,?)",
                        (identity, subscription, current["release_id"], target_id, current["generation"], signature, canonical(body)))
        return self.delivery(identity)

    def delivery(self, identity):
        row = self.db.execute("SELECT * FROM deliveries WHERE id=?", (identity,)).fetchone()
        check(row is not None, "delivery", "投递包不存在")
        content = json.loads(row["payload"])
        check(digest(content) == row["digest"], "digest", "投递包摘要不符")
        return {"content": content, "digest": row["digest"], "acknowledged": bool(row["acknowledged"])}

    def acknowledge(self, value):
        fields(value, ("id", "digest", "generation"))
        identity = key(value["id"])
        integer(value["generation"], "消费代次")
        delivery = self.delivery(identity)
        content = delivery["content"]
        check(delivery["digest"] == value["digest"] and content["generation"] == value["generation"],
              "digest", "确认的投递内容或消费代次不符")
        if delivery["acknowledged"]:
            return {"id": identity, "replayed": True, "release": content["to_release"]}
        current = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (content["subscription"],)).fetchone()
        check(current["generation"] == content["generation"] and current["release_id"] == content["from_release"],
              "consumer_conflict", "该订阅已确认其他投递，不能覆盖其消费位置")
        self.db.execute("UPDATE subscriptions SET release_id=?,generation=generation+1 WHERE id=?",
                        (content["to_release"], content["subscription"]))
        self.db.execute("UPDATE deliveries SET acknowledged=1 WHERE id=?", (identity,))
        return {"id": identity, "replayed": False, "release": content["to_release"], "generation": content["generation"] + 1}

    def is_ancestor(self, before, after):
        current = after
        while current is not None:
            if current == before:
                return True
            row = self.db.execute("SELECT parent FROM releases WHERE id=?", (current,)).fetchone()
            check(row is not None, "release", "消费目标的前序版本不存在")
            current = row[0]
        return before is None

    @staticmethod
    def parts(release):
        if release is None:
            return {}
        return {row["key"]: row for row in release["content"]["snapshot"]["result"]["partitions"]}
