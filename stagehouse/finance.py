from collections import defaultdict

from .rules import issue, round_share, ticket_key


class Settlement:
    def __init__(self, reference):
        self.reference = reference

    def refunds(self, events, tickets):
        grouped = defaultdict(list)
        rejected = []
        for event in events:
            if event["kind"] != "refund":
                continue
            data = event["data"]
            identity = ticket_key(data["ticket_source"], data["ticket"])
            grouped[identity].append(event)
        valid = {}
        for identity, rows in sorted(grouped.items()):
            ticket = tickets.get(identity)
            anchors = [row["identity"] for row in rows]
            if ticket is None:
                rejected.append(issue("orphan_refund", "退款未找到有效销售", anchors, ticket=identity))
                continue
            if any(row["data"]["at"] < ticket["issued_at"] for row in rows):
                rejected.append(issue("refund_time", "退款早于销售", anchors, ticket=identity))
                continue
            if sum(row["data"]["cents"] for row in rows) > ticket["sale_cents"]:
                rejected.append(issue("over_refund", "累计退款超过原始售价，整组退款暂不入事实", anchors, ticket=identity))
                continue
            valid[identity] = [{"event": row["identity"], "revision": row["revision"], **row["data"]}
                               for row in sorted(rows, key=lambda row: (row["data"]["at"], row["identity"]))]
        return valid, rejected

    def settle(self, tickets, refunds):
        facts = []
        journal = []
        rejected = []
        for identity, ticket in sorted(tickets.items()):
            channel = self.reference.channel(ticket["source"])
            if channel is None:
                rejected.append(issue("missing_channel", "出票来源缺少结算渠道", [ticket["sale_event"]], ticket=identity))
                continue
            sale = ticket["sale_cents"]
            refunded = sum(row["cents"] for row in refunds.get(identity, []))
            net = sale - refunded
            commission = round_share(net, channel["commission_bps"])
            venue_due = net - commission
            facts.append({**ticket, "refund_cents": refunded, "net_cents": net, "commission_cents": commission,
                          "venue_due_cents": venue_due, "account": channel["account"],
                          "channel_revision": channel["revision"], "refund_events": [row["event"] for row in refunds.get(identity, [])]})
            sale_commission = round_share(sale, channel["commission_bps"])
            entries = [{"event": ticket["sale_event"], "kind": "sale", "at": ticket["issued_at"],
                        "cash_cents": sale, "commission_cents": sale_commission, "venue_cents": sale - sale_commission}]
            cumulative = 0
            previous_commission = sale_commission
            for refund in refunds.get(identity, []):
                cumulative += refund["cents"]
                remaining_commission = round_share(sale - cumulative, channel["commission_bps"])
                reversed_commission = previous_commission - remaining_commission
                entries.append({"event": refund["event"], "kind": "refund", "at": refund["at"],
                                "cash_cents": -refund["cents"], "commission_cents": -reversed_commission,
                                "venue_cents": -refund["cents"] + reversed_commission})
                previous_commission = remaining_commission
            for entry in entries:
                journal.append({"ticket": identity, "session": ticket["session"], "day": ticket["day"],
                                "account": channel["account"], "channel_revision": channel["revision"], **entry})
            assert sum(row["cash_cents"] for row in entries) == net
            assert sum(row["commission_cents"] for row in entries) == commission
            assert sum(row["venue_cents"] for row in entries) == venue_due
        return facts, journal, rejected
