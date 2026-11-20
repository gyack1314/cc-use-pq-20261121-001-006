from collections import defaultdict
from datetime import datetime

from ..rules import date_text, fields, integer, key


class Health:
    def __init__(self, state):
        self.state = state

    def profile(self, run):
        content = run["content"]
        result = content["result"]
        events = self.state.events(content["cutoff"])
        identities = {row["identity"]: row for row in events}
        by_source = defaultdict(lambda: {"sale": 0, "refund": 0, "scan": 0, "quarantined": set(), "issue_codes": set()})
        for event in events:
            by_source[event["source"]][event["kind"]] += 1
        unresolved_tickets = set()
        for problem in result["issues"]:
            for identity in problem["events"]:
                source = identities[identity]["source"]
                by_source[source]["quarantined"].add(identity)
                by_source[source]["issue_codes"].add(problem["code"])
            ticket = problem["details"].get("ticket")
            if ticket:
                unresolved_tickets.add(ticket)
        sources = []
        for source, values in sorted(by_source.items()):
            total = sum(values[kind] for kind in ("sale", "refund", "scan"))
            sources.append({"source": source, "events": total, "kinds": {kind: values[kind] for kind in ("sale", "refund", "scan")},
                            "quarantined": len(values["quarantined"]), "issue_codes": sorted(values["issue_codes"]),
                            "accepted_events": total - len(values["quarantined"])})
        windows = []
        for partition in result["partitions"]:
            body = partition["body"]
            tickets = body["settlements"]
            journal = body["journal"]
            late_refunds = [row for row in journal if row["kind"] == "refund" and row["at"][:10] > body["day"]]
            windows.append({"partition": partition["key"], "tickets": len(tickets), "admitted": len(body["admissions"]),
                            "gross_cents": sum(row["sale_cents"] for row in tickets),
                            "refund_cents": sum(row["refund_cents"] for row in tickets),
                            "net_cents": sum(row["net_cents"] for row in tickets),
                            "late_refund_events": len(late_refunds),
                            "cancelled_tickets": sum(row["cancelled"] for row in tickets)})
        return {"run": content["run"], "cutoff": content["cutoff"], "catalog_version": content["catalog_version"],
                "publishable": result["publishable"], "sources": sources, "partitions": windows,
                "unresolved_tickets": sorted(unresolved_tickets), "issue_count": len(result["issues"]),
                "interpretation": "隔离资料不等同业务金额为零，候选统计不能替代已发布消费事实"}

    def issues(self, run, value):
        fields(value, (), ("code", "source", "offset", "limit"))
        offset = integer(value.get("offset", 0), "问题偏移")
        limit = integer(value.get("limit", 100), "问题页大小", 1, 1000)
        if value.get("code") is not None:
            key(value["code"])
        if value.get("source") is not None:
            key(value["source"])
        rows = run["content"]["result"]["issues"]
        filtered = [row for row in rows if (value.get("code") is None or row["code"] == value["code"])
                    and (value.get("source") is None or any(identity.startswith(value["source"] + ":") for identity in row["events"]))]
        return {"run": run["content"]["run"], "total": len(filtered), "offset": offset,
                "issues": filtered[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(filtered) else None}

    def dimension_impact(self, left, right):
        first, second = left["content"], right["content"]
        before = self.state.dimensions(first["catalog_version"])
        after = self.state.dimensions(second["catalog_version"])
        changed = []
        for kind in sorted(before):
            for identity in sorted(set(before[kind]) | set(after[kind])):
                a, b = before[kind].get(identity), after[kind].get(identity)
                if a != b:
                    changed.append({"kind": kind, "id": identity, "before": a, "after": b})
        a = {row["ticket"]: row for row in first["result"]["settlements"]}
        b = {row["ticket"]: row for row in second["result"]["settlements"]}
        affected = []
        for identity in sorted(set(a) | set(b)):
            if a.get(identity) != b.get(identity):
                affected.append({"ticket": identity, "before": a.get(identity), "after": b.get(identity)})
        return {"left_run": first["run"], "right_run": second["run"], "same_raw_cutoff": first["cutoff"] == second["cutoff"],
                "dimension_changes": changed, "affected_settlements": affected,
                "interpretation": "原始水位不同的候选差异同时包含新事实，不能全部归因于维度修订"}
