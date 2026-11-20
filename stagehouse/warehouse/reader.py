from ..rules import check, date_text, fields, integer, key


class Reader:
    def __init__(self, versions):
        self.versions = versions

    def query(self, value):
        fields(value, ("release", "table"), ("day", "session", "account", "offset", "limit"))
        check(value["table"] in ("tickets", "admissions", "settlements", "journal", "lineage"), "table", "不支持的事实表")
        if value["release"] is not None:
            key(value["release"], "消费版本")
        if value.get("day") is not None:
            date_text(value["day"])
        if value.get("session") is not None:
            key(value["session"])
        if value.get("account") is not None:
            key(value["account"])
        offset = integer(value.get("offset", 0), "偏移", 0)
        limit = integer(value.get("limit", 100), "页大小", 1, 1000)
        release = self.versions.release(value["release"])
        snapshot = release["content"]["snapshot"]
        table = value["table"]
        rows = snapshot["result"][table]
        filters = {field: value[field] for field in ("day", "session", "account") if value.get(field) is not None}
        check(not filters or table != "lineage", "filter", "来源血缘表不接受业务维度筛选")
        check("account" not in filters or table in ("settlements", "journal"), "filter", "该表没有结算账号维度")
        selected = [row for row in rows if all(row.get(field) == target for field, target in filters.items())]
        page = selected[offset:offset + limit]
        return {"release": release["content"]["release"], "digest": release["digest"],
                "table": table, "cutoff": snapshot["cutoff"], "catalog_version": snapshot["catalog_version"],
                "total": len(selected), "offset": offset, "rows": page,
                "next_offset": offset + len(page) if offset + len(page) < len(selected) else None}

    def manifest(self, release_id=None):
        release = self.versions.release(release_id)
        snapshot = release["content"]["snapshot"]
        return {"release": release["content"]["release"], "digest": release["digest"],
                "parent": release["content"]["parent"], "sources": snapshot["sources"],
                "cutoff": snapshot["cutoff"], "catalog_version": snapshot["catalog_version"],
                "partitions": [{name: part[name] for name in ("key", "digest", "rows")}
                               for part in snapshot["result"]["partitions"]],
                "statistics": snapshot["result"]["statistics"], "is_head": release["is_head"],
                "current_inputs": release["current_inputs"]}

    def trace(self, ticket, release_id=None):
        release = self.versions.release(release_id)
        snapshot = release["content"]["snapshot"]
        result = snapshot["result"]
        sales = [row for row in result["tickets"] if row["ticket"] == ticket]
        check(bool(sales), "ticket", "该消费版本中未找到票号")
        settlements = [row for row in result["settlements"] if row["ticket"] == ticket]
        admissions = [row for row in result["admissions"] if row["ticket"] == ticket]
        journal = [row for row in result["journal"] if row["ticket"] == ticket]
        identities = {sales[0]["sale_event"]}
        identities.update(row["event"] for row in admissions + journal)
        return {"release": release["content"]["release"], "ticket": ticket, "sale": sales[0],
                "settlements": settlements, "admissions": admissions, "journal": journal,
                "lineage": [row for row in result["lineage"] if row["event"] in identities]}

    def reconcile(self, left_id, right_id):
        left = self.versions.release(left_id)["content"]["snapshot"]["result"]
        right = self.versions.release(right_id)["content"]["snapshot"]["result"]
        before = {row["ticket"]: row for row in left["settlements"]}
        after = {row["ticket"]: row for row in right["settlements"]}
        changes = []
        for ticket in sorted(set(before) | set(after)):
            old, new = before.get(ticket), after.get(ticket)
            if old == new:
                continue
            changes.append({"ticket": ticket, "before": old, "after": new,
                            "net_delta_cents": (new["net_cents"] if new else 0) - (old["net_cents"] if old else 0)})
        left_parts = {row["key"]: row["digest"] for row in left["partitions"]}
        right_parts = {row["key"]: row["digest"] for row in right["partitions"]}
        return {"left": left_id, "right": right_id, "tickets": changes,
                "changed_partitions": [key for key in sorted(set(left_parts) | set(right_parts))
                                       if left_parts.get(key) != right_parts.get(key)]}
