from ..rules import fields, instant, integer, key, ticket_key, check


def normalize_event(row):
    fields(row, ("id", "revision", "offset", "kind", "data"))
    kind = row["kind"]
    check(kind in ("sale", "refund", "scan"), "kind", "不支持的票务事实类型")
    data = row["data"]
    if kind == "sale":
        fields(data, ("ticket", "session_ref", "at", "cents"))
        value = {"ticket": key(data["ticket"]), "session_ref": key(data["session_ref"]),
                 "at": instant(data["at"]), "cents": integer(data["cents"], "出票金额", 0, 10**9)}
    elif kind == "refund":
        fields(data, ("ticket_source", "ticket", "at", "cents"))
        ticket_key(data["ticket_source"], data["ticket"])
        value = {"ticket_source": key(data["ticket_source"]), "ticket": key(data["ticket"]),
                 "at": instant(data["at"]), "cents": integer(data["cents"], "退款金额", 1, 10**9)}
    else:
        fields(data, ("ticket_source", "ticket", "session_ref", "at", "gate"))
        ticket_key(data["ticket_source"], data["ticket"])
        value = {"ticket_source": key(data["ticket_source"]), "ticket": key(data["ticket"]),
                 "session_ref": key(data["session_ref"]), "at": instant(data["at"]), "gate": key(data["gate"])}
    return {"id": key(row["id"], "来源事件号"), "revision": integer(row["revision"], "修订", 1),
            "offset": integer(row["offset"], "来源位点", 1), "kind": kind, "data": value}


def normalize_batch(value):
    fields(value, ("id", "source", "first_offset", "last_offset", "events"))
    first = integer(value["first_offset"], "起始位点", 1)
    last = integer(value["last_offset"], "结束位点", first)
    check(last - first < 50000, "batch", "单批资料过大")
    check(isinstance(value["events"], list) and len(value["events"]) == last - first + 1,
          "coverage", "批次未完整覆盖声明位点")
    events = [normalize_event(row) for row in value["events"]]
    offsets = sorted(row["offset"] for row in events)
    check(offsets == list(range(first, last + 1)), "coverage", "批次存在重复或缺失位点")
    identities = [(row["id"], row["revision"]) for row in events]
    check(len(set(identities)) == len(identities), "duplicate", "同批事件修订重复")
    return {"id": key(value["id"], "批次编号"), "source": key(value["source"], "来源"),
            "first_offset": first, "last_offset": last, "events": sorted(events, key=lambda row: row["offset"])}
