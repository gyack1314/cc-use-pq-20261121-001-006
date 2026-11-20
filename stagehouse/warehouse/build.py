from collections import defaultdict

from ..facts import TicketFacts
from ..finance import Settlement
from ..reference import Reference
from ..rules import digest, issue


class Builder:
    def __init__(self, dimensions):
        self.reference = Reference(dimensions)

    def build(self, events):
        tickets, sale_issues = TicketFacts(self.reference).sales(events)
        refunds, refund_issues = Settlement(self.reference).refunds(events, tickets)
        admissions, scan_issues = TicketFacts(self.reference).admissions(events, tickets, refunds)
        settlements, journal, money_issues = Settlement(self.reference).settle(tickets, refunds)
        problems = sale_issues + refund_issues + scan_issues + money_issues
        accepted_scan_ids = {row["event"] for row in admissions}
        excluded_events = {identity for problem in problems for identity in problem["events"]}
        lineage = []
        for event in events:
            identity = event["identity"]
            status = "quarantined" if identity in excluded_events else "accepted"
            if event["kind"] == "scan" and identity not in accepted_scan_ids:
                status = "quarantined"
            lineage.append({"event": identity, "revision": event["revision"], "seq": event["seq"],
                            "kind": event["kind"], "status": status})
        partitions = self.partitions(settlements, admissions, journal)
        statistics = {"raw_events": len(events), "sales": len(tickets), "admissions": len(admissions),
                      "settled_tickets": len(settlements), "refunds": sum(len(rows) for rows in refunds.values()),
                      "quarantined_events": len(excluded_events), "issues": len(problems),
                      "gross_cents": sum(row["sale_cents"] for row in settlements),
                      "refund_cents": sum(row["refund_cents"] for row in settlements),
                      "net_cents": sum(row["net_cents"] for row in settlements),
                      "commission_cents": sum(row["commission_cents"] for row in settlements),
                      "venue_due_cents": sum(row["venue_due_cents"] for row in settlements)}
        from .availability import Availability
        availability = Availability(self.reference).inspect(list(tickets.values()), admissions, settlements)
        return {"schema_version": 1, "tickets": list(tickets.values()), "admissions": admissions,
                "settlements": settlements, "journal": journal, "partitions": partitions,
                "issues": sorted(problems, key=lambda row: (row["code"], row["events"])),
                "lineage": sorted(lineage, key=lambda row: row["event"]), "statistics": statistics,
                "availability": availability, "publishable": bool(events) and not problems}

    def partitions(self, settlements, admissions, journal):
        grouped = defaultdict(lambda: {"settlements": [], "admissions": [], "journal": []})
        for kind, rows in (("settlements", settlements), ("admissions", admissions), ("journal", journal)):
            for row in rows:
                identity = row["day"] + "/" + row["session"]
                grouped[identity][kind].append(row)
        result = []
        for identity, contents in sorted(grouped.items()):
            day, session_id = identity.split("/")
            session = self.reference.session(session_id)
            body = {"day": day, "session": session_id, "venue": session["venue"],
                    "production": session["production"], "session_revision": session["revision"], **contents}
            result.append({"key": identity, "digest": digest(body), "body": body,
                           "rows": {kind: len(rows) for kind, rows in contents.items()}})
        return result
