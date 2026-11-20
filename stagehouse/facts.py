from collections import defaultdict

from .rules import issue, ticket_key


class TicketFacts:
    def __init__(self, reference):
        self.reference = reference

    def sales(self, events):
        groups = defaultdict(list)
        for event in events:
            if event["kind"] == "sale":
                groups[ticket_key(event["source"], event["data"]["ticket"])].append(event)
        tickets = {}
        rejected = []
        for identity, candidates in sorted(groups.items()):
            anchors = [event["identity"] for event in candidates]
            if len(candidates) != 1:
                rejected.append(issue("duplicate_sale", "同出票来源票号存在多条销售事实", anchors, ticket=identity))
                continue
            event = candidates[0]
            data = event["data"]
            session = self.reference.resolve(event["source"], data["session_ref"])
            if session is None:
                rejected.append(issue("unmapped_sale", "销售场次尚未映射", anchors, ticket=identity))
                continue
            if data["at"] > session["end"]:
                rejected.append(issue("sale_after_end", "销售发生在场次结束之后", anchors, ticket=identity))
                continue
            tickets[identity] = {"ticket": identity, "source": event["source"], "session": session["id"],
                                 "venue": session["venue"], "day": self.reference.business_day(session),
                                 "issued_at": data["at"], "sale_cents": data["cents"],
                                 "sale_event": event["identity"], "sale_revision": event["revision"],
                                 "session_revision": session["revision"], "cancelled": session["status"] == "cancelled"}
        return tickets, rejected

    def admissions(self, events, tickets, refunds):
        accepted = []
        rejected = []
        seen = set()
        scans = sorted((event for event in events if event["kind"] == "scan"),
                       key=lambda event: (event["data"]["at"], event["identity"]))
        for event in scans:
            data = event["data"]
            identity = ticket_key(data["ticket_source"], data["ticket"])
            ticket = tickets.get(identity)
            target = self.reference.resolve(event["source"], data["session_ref"])
            reason = None
            if ticket is None:
                reason = ("orphan_scan", "验票未找到有效销售")
            elif target is None:
                reason = ("unmapped_scan", "验票场次尚未映射")
            elif target["id"] != ticket["session"]:
                reason = ("wrong_session", "验票场次与购票权益不一致")
            elif ticket["cancelled"]:
                reason = ("cancelled_session", "取消场次不能产生有效入场")
            elif not target["admit_from"] <= data["at"] <= target["end"] or data["at"] < ticket["issued_at"]:
                reason = ("scan_time", "验票时间超出权益允许范围")
            elif ticket["sale_cents"] > 0 and sum(row["cents"] for row in refunds.get(identity, []) if row["at"] <= data["at"]) >= ticket["sale_cents"]:
                reason = ("refunded_scan", "全额退款后不能产生有效入场")
            elif identity in seen:
                reason = ("repeat_scan", "该票已有有效入场")
            if reason:
                rejected.append(issue(*reason, [event["identity"]], ticket=identity))
                continue
            seen.add(identity)
            accepted.append({"ticket": identity, "session": ticket["session"], "venue": ticket["venue"], "day": ticket["day"],
                             "at": data["at"], "gate": data["gate"], "event": event["identity"],
                             "revision": event["revision"], "sale_event": ticket["sale_event"]})
        counts = defaultdict(list)
        for admission in accepted:
            counts[admission["session"]].append(admission)
        for session_id, rows in counts.items():
            session = self.reference.session(session_id)
            capacity = self.reference.dimensions["venue"][session["venue"]]["capacity"]
            if len(rows) > capacity:
                rejected.append(issue("capacity", "有效入场超过场馆容量", [r["event"] for r in rows], session=session_id))
        return accepted, rejected
