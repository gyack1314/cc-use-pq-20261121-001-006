from collections import Counter

from ..rules import check, digest


class WarehouseContract:
    def inspect(self, result):
        errors = []
        tickets = {row["ticket"]: row for row in result["tickets"]}
        settlements = {row["ticket"]: row for row in result["settlements"]}
        counts = Counter(row["ticket"] for row in result["admissions"])
        if any(count != 1 for count in counts.values()):
            errors.append("有效入场粒度不是每票一次")
        for row in result["settlements"]:
            if row["net_cents"] != row["sale_cents"] - row["refund_cents"]:
                errors.append("票级销售与退款净额不守恒")
            if row["net_cents"] != row["commission_cents"] + row["venue_due_cents"]:
                errors.append("票级渠道与场馆分成不守恒")
            if row["ticket"] not in tickets:
                errors.append("结算缺少出票权益依据")
        for row in result["admissions"]:
            if row["ticket"] not in tickets:
                errors.append("有效入场缺少出票权益依据")
        for kind in ("settlements", "admissions", "journal"):
            flat = [row for partition in result["partitions"] for row in partition["body"][kind]]
            if sorted(map(digest, flat)) != sorted(map(digest, result[kind])):
                errors.append(f"{kind}分区覆盖与事实不一致")
        for partition in result["partitions"]:
            if digest(partition["body"]) != partition["digest"]:
                errors.append("分区摘要与内容不一致")
            body = partition["body"]
            if partition["key"] != body["day"] + "/" + body["session"]:
                errors.append("分区键与业务日期场次不一致")
            for kind in ("settlements", "admissions", "journal"):
                if len(body[kind]) != partition["rows"][kind]:
                    errors.append("分区行数证明不一致")
                if any(row["session"] != body["session"] or row["day"] != body["day"] for row in body[kind]):
                    errors.append("事实落入错误分区")
        for identity, row in settlements.items():
            entries = [item for item in result["journal"] if item["ticket"] == identity]
            for total_field, journal_field in (("net_cents", "cash_cents"), ("commission_cents", "commission_cents"),
                                                ("venue_due_cents", "venue_cents")):
                if sum(item[journal_field] for item in entries) != row[total_field]:
                    errors.append("分录与票级结算不守恒")
        return {"valid": not errors, "errors": sorted(set(errors))}

    def require(self, result):
        report = self.inspect(result)
        check(report["valid"], "warehouse_contract", "事实仓内部契约失败", errors=report["errors"])
        return report
