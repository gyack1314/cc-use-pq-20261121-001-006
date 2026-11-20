from collections import defaultdict

from ..rules import check, key


class Availability:
    def __init__(self, reference):
        self.reference = reference

    def inspect(self, tickets, admissions, settlements):
        by_session = defaultdict(list)
        for ticket in tickets:
            by_session[ticket["session"]].append(ticket)
        admitted = {row["ticket"] for row in admissions}
        money = {row["ticket"]: row for row in settlements}
        result = []
        for session_id, session in sorted(self.reference.dimensions["session"].items()):
            rows = by_session[session_id]
            venue = self.reference.dimensions["venue"][session["venue"]]
            cancelled = session["status"] == "cancelled"
            active_rights = [row for row in rows if not cancelled and (row["sale_cents"] == 0
                              or money.get(row["ticket"], {}).get("net_cents", row["sale_cents"]) > 0)]
            unsettled = [row["ticket"] for row in rows if row["ticket"] not in money]
            charged_cancelled = [row["ticket"] for row in rows if cancelled
                                 and money.get(row["ticket"], {}).get("net_cents", 0) > 0]
            result.append({"session": session_id, "venue": session["venue"], "day": self.reference.business_day(session),
                           "capacity": venue["capacity"], "issued": len(rows), "active_rights": len(active_rights),
                           "admitted": sum(row["ticket"] in admitted for row in rows),
                           "remaining_capacity": max(0, venue["capacity"] - len(active_rights)),
                           "oversold": len(active_rights) > venue["capacity"], "cancelled": cancelled,
                           "unsettled_tickets": sorted(unsettled), "cancelled_unrefunded": sorted(charged_cancelled)})
        return result
