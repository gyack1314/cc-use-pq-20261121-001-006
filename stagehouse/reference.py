from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .rules import canonical, check, date_text, fields, instant, integer, key, label


def normalize_dimension(kind, row):
    if kind == "venue":
        fields(row, ("id", "revision", "name", "timezone", "capacity"))
        try:
            ZoneInfo(row["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            from .rules import Problem
            raise Problem("timezone", "场馆时区无效") from None
        value = {"name": label(row["name"]), "timezone": row["timezone"],
                 "capacity": integer(row["capacity"], "场馆容量", 1, 100000)}
    elif kind == "session":
        fields(row, ("id", "revision", "production", "venue", "start", "end", "admit_from", "status"))
        start, end, admit = instant(row["start"]), instant(row["end"]), instant(row["admit_from"])
        check(admit <= start < end, "period", "场次时间或入场时间不合法")
        check(row["status"] in ("scheduled", "cancelled"), "status", "场次状态无效")
        value = {"production": label(row["production"]), "venue": key(row["venue"]), "start": start,
                 "end": end, "admit_from": admit, "status": row["status"]}
    elif kind == "alias":
        fields(row, ("id", "revision", "source", "external", "session"))
        value = {"source": key(row["source"]), "external": key(row["external"]), "session": key(row["session"])}
    elif kind == "channel":
        fields(row, ("id", "revision", "source", "account", "commission_bps"))
        value = {"source": key(row["source"]), "account": key(row["account"]),
                 "commission_bps": integer(row["commission_bps"], "渠道佣金基点", 0, 10000)}
    else:
        from .rules import Problem
        raise Problem("dimension", "不支持的维度类型")
    return {"id": key(row["id"]), "revision": integer(row["revision"], "维度修订", 1), **value}


class Reference:
    def __init__(self, dimensions):
        self.dimensions = dimensions

    def validate(self, sources):
        aliases = set()
        channels = set()
        for session in self.dimensions["session"].values():
            check(session["venue"] in self.dimensions["venue"], "reference", "场次引用的场馆不存在")
        for alias in self.dimensions["alias"].values():
            check(alias["source"] in sources and alias["session"] in self.dimensions["session"],
                  "reference", "场次别名的来源或目标不存在")
            identity = (alias["source"], alias["external"])
            check(identity not in aliases, "alias", "同来源外部场次只能对应一个场次")
            aliases.add(identity)
        for channel in self.dimensions["channel"].values():
            check(channel["source"] in sources, "reference", "渠道来源不存在")
            check(channel["source"] not in channels, "channel", "同来源只能有一个结算渠道")
            channels.add(channel["source"])

    def resolve(self, source, external):
        matches = [row for row in self.dimensions["alias"].values()
                   if row["source"] == source and row["external"] == external]
        return self.dimensions["session"].get(matches[0]["session"]) if matches else None

    def channel(self, source):
        return next((row for row in self.dimensions["channel"].values() if row["source"] == source), None)

    def business_day(self, session):
        venue = self.dimensions["venue"][session["venue"]]
        return datetime.fromisoformat(session["start"]).astimezone(ZoneInfo(venue["timezone"])).date().isoformat()

    def session(self, identity):
        return self.dimensions["session"].get(identity)


class Catalog:
    def __init__(self, state):
        self.state = state

    def apply(self, value):
        fields(value, ("records",))
        check(isinstance(value["records"], list) and 0 < len(value["records"]) <= 10000, "catalog", "维度资料数量无效")
        candidate = self.state.dimensions()
        seen = set()
        changed = []
        for item in value["records"]:
            fields(item, ("kind", "data"))
            kind = item["kind"]
            row = normalize_dimension(kind, item["data"])
            identity = (kind, row["id"])
            check(identity not in seen, "duplicate", "维度资料包中对象重复")
            seen.add(identity)
            previous = candidate[kind].get(row["id"])
            if previous == row:
                continue
            check(row["revision"] == (previous["revision"] + 1 if previous else 1), "revision", "维度修订必须连续递增")
            candidate[kind][row["id"]] = row
            changed.append((kind, row))
        sources = {r[0] for r in self.state.db.execute("SELECT id FROM sources")}
        Reference(candidate).validate(sources)
        if not changed:
            return {"version": self.state.catalog_version(), "changed": 0}
        version = self.state.catalog_version() + 1
        for kind, row in changed:
            self.state.db.execute("INSERT INTO catalog VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                                  (kind, row["id"], row["revision"], canonical(row)))
            self.state.db.execute("INSERT INTO catalog_history VALUES(?,?,?,?,?)",
                                  (version, kind, row["id"], row["revision"], canonical(row)))
        self.state.db.execute("UPDATE meta SET value=? WHERE name='catalog'", (version,))
        return {"version": version, "changed": len(changed)}
