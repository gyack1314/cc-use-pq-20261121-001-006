from ..rules import check, digest
from .versions import Versions


class PartitionChecks:
    def __init__(self, state):
        self.state = state

    def inspect(self, release_id):
        release = Versions(self.state).release(release_id)
        snapshot = release["content"]["snapshot"]
        result = snapshot["result"]
        reports = []
        for partition in result["partitions"]:
            body = partition["body"]
            settlements = body["settlements"]
            journal = body["journal"]
            report = {"key": partition["key"], "digest_valid": digest(body) == partition["digest"],
                      "ticket_count": len(settlements), "admission_count": len(body["admissions"]),
                      "fact_net_cents": sum(row["net_cents"] for row in settlements),
                      "journal_cash_cents": sum(row["cash_cents"] for row in journal),
                      "fact_commission_cents": sum(row["commission_cents"] for row in settlements),
                      "journal_commission_cents": sum(row["commission_cents"] for row in journal)}
            report["balanced"] = (report["fact_net_cents"] == report["journal_cash_cents"]
                                  and report["fact_commission_cents"] == report["journal_commission_cents"])
            reports.append(report)
        check(all(row["digest_valid"] and row["balanced"] for row in reports), "partition", "分区事实与资金分录核对失败")
        return {"release": release_id, "valid": True, "partitions": reports,
                "net_cents": sum(row["fact_net_cents"] for row in reports)}
