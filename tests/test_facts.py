import copy
import json
import tempfile
import unittest
from pathlib import Path

from stagehouse.rules import Problem
from stagehouse.service import Service


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class FactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(Path(self.temp.name) / "warehouse.sqlite3")
        self.addCleanup(self.service.state.close)
        for source in ("box", "partner", "gate"):
            self.service.source({"id": source, "name": source})
        self.service.catalog(load("catalog.json"))
        for name in ("box.json", "partner.json", "gate.json"):
            self.service.ingest(load(name))

    def result(self, name="candidate"):
        return self.service.prepare(name)["content"]["result"]

    def test_baseline_sales_admissions_and_money(self):
        result = self.result()
        self.assertTrue(result["publishable"])
        self.assertEqual(result["statistics"]["raw_events"], 5)
        self.assertEqual(result["statistics"]["sales"], 3)
        self.assertEqual(result["statistics"]["admissions"], 2)
        self.assertEqual(result["statistics"]["net_cents"], 28001)
        self.assertEqual(result["statistics"]["commission_cents"], 1250)
        self.assertEqual(result["statistics"]["venue_due_cents"], 26751)
        self.assertEqual(result["partitions"][0]["key"], "2026-11-21/evening")

    def test_late_refund_preserves_earlier_admission_and_balances(self):
        self.service.ingest(load("refund.json"))
        result = self.result()
        ticket = next(row for row in result["settlements"] if row["ticket"] == "partner:C")
        self.assertEqual(ticket["net_cents"], 6667)
        self.assertEqual(ticket["commission_cents"], 833)
        self.assertEqual(ticket["venue_due_cents"], 5834)
        self.assertEqual(result["statistics"]["admissions"], 2)
        entries = [row for row in result["journal"] if row["ticket"] == "partner:C"]
        self.assertEqual(sum(row["cash_cents"] for row in entries), 6667)
        self.assertEqual(sum(row["commission_cents"] for row in entries), 833)
        self.assertEqual(sum(row["venue_cents"] for row in entries), 5834)
        self.assertTrue(all(row["day"] == "2026-11-21" for row in entries))

    def test_full_refund_before_scan_invalidates_entitlement(self):
        batch = load("refund.json")
        batch["events"][0]["data"].update(cents=10001, at="2026-11-21T18:00:00+08:00")
        self.service.ingest(batch)
        result = self.result()
        self.assertFalse(result["publishable"])
        self.assertEqual(result["statistics"]["admissions"], 1)
        self.assertIn("refunded_scan", [row["code"] for row in result["issues"]])
        ticket = next(row for row in result["settlements"] if row["ticket"] == "partner:C")
        self.assertEqual(ticket["net_cents"], 0)
        self.assertEqual(ticket["commission_cents"], 0)

    def test_over_refund_quarantines_group_without_picking_lucky_order(self):
        batch = load("refund.json")
        batch["events"][0]["data"]["cents"] = 10002
        self.service.ingest(batch)
        result = self.result()
        self.assertIn("over_refund", [row["code"] for row in result["issues"]])
        ticket = next(row for row in result["settlements"] if row["ticket"] == "partner:C")
        self.assertEqual(ticket["refund_cents"], 0)
        self.assertFalse(result["publishable"])

    def test_missing_sale_refund_is_retained_then_resolved(self):
        with Service(Path(self.temp.name) / "other.sqlite3") as other:
            for source in ("box", "partner", "gate"):
                other.source({"id": source, "name": source})
            other.catalog(load("catalog.json"))
            refund = load("refund.json")
            refund.update(id="refund-first", source="box", first_offset=1, last_offset=1)
            refund["events"][0]["offset"] = 1
            other.ingest(refund)
            first = other.prepare("first")
            self.assertEqual(first["content"]["result"]["issues"][0]["code"], "orphan_refund")
            other.ingest(load("partner.json"))
            second = other.prepare("second")
            self.assertTrue(second["content"]["result"]["publishable"])
            comparison = other.compare_runs("first", "second")
            self.assertEqual(len(comparison["resolved_issues"]), 1)
            self.assertTrue(other.replay("first")["reproducible"])

    def test_repeat_scan_does_not_add_admission(self):
        batch = load("gate.json")
        batch.update(id="repeat", first_offset=3, last_offset=3)
        batch["events"] = [batch["events"][0]]
        batch["events"][0].update(id="repeat-a", offset=3)
        self.service.ingest(batch)
        result = self.result()
        self.assertEqual(result["statistics"]["admissions"], 2)
        self.assertIn("repeat_scan", [row["code"] for row in result["issues"]])

    def test_unmapped_scan_is_not_dropped(self):
        batch = load("gate.json")
        batch.update(id="unmapped", first_offset=3, last_offset=3)
        batch["events"] = [batch["events"][0]]
        batch["events"][0].update(id="unmapped-a", offset=3)
        batch["events"][0]["data"]["session_ref"] = "unknown"
        self.service.ingest(batch)
        result = self.result()
        self.assertIn("unmapped_scan", [row["code"] for row in result["issues"]])
        lineage = next(row for row in result["lineage"] if row["event"] == "gate:unmapped-a")
        self.assertEqual(lineage["status"], "quarantined")

    def test_cancellation_changes_rights_and_scan_not_money(self):
        session = copy.deepcopy(load("catalog.json")["records"][1])
        session["data"].update(revision=2, status="cancelled")
        self.service.catalog({"records": [session]})
        result = self.result()
        self.assertEqual(result["statistics"]["admissions"], 0)
        self.assertEqual(result["statistics"]["net_cents"], 28001)
        self.assertEqual(len(result["availability"][0]["cancelled_unrefunded"]), 3)
        self.assertEqual(result["availability"][0]["active_rights"], 0)
        self.assertEqual(len([r for r in result["issues"] if r["code"] == "cancelled_session"]), 2)

    def test_capacity_check_blocks_snapshot(self):
        venue = copy.deepcopy(load("catalog.json")["records"][0])
        venue["data"].update(revision=2, capacity=1)
        self.service.catalog({"records": [venue]})
        result = self.result()
        self.assertIn("capacity", [row["code"] for row in result["issues"]])
        self.assertTrue(result["availability"][0]["oversold"])
        self.assertFalse(result["publishable"])

    def test_free_ticket_has_right_but_no_money(self):
        batch = load("partner.json")
        batch.update(id="free", first_offset=2, last_offset=2)
        batch["events"][0].update(id="sale-free", offset=2)
        batch["events"][0]["data"].update(ticket="FREE", cents=0)
        self.service.ingest(batch)
        result = self.result()
        row = next(row for row in result["settlements"] if row["ticket"] == "partner:FREE")
        self.assertEqual(row["net_cents"], 0)
        self.assertEqual(row["commission_cents"], 0)
        self.assertEqual(result["availability"][0]["active_rights"], 4)

    def test_scan_at_admission_boundary_is_valid(self):
        batch = load("gate.json")
        batch.update(id="scan-correction", first_offset=3, last_offset=3)
        batch["events"] = [batch["events"][0]]
        batch["events"][0].update(revision=2, offset=3)
        batch["events"][0]["data"]["at"] = "2026-11-21T18:30:00+08:00"
        self.service.ingest(batch)
        self.assertEqual(self.result()["statistics"]["admissions"], 2)

    def test_same_ticket_label_different_source_is_distinct(self):
        batch = load("partner.json")
        batch.update(id="same-label", first_offset=2, last_offset=2)
        batch["events"][0].update(id="sale-other", offset=2)
        batch["events"][0]["data"].update(ticket="A", cents=100)
        self.service.ingest(batch)
        result = self.result()
        self.assertEqual(result["statistics"]["sales"], 4)
        self.assertIn("box:A", [r["ticket"] for r in result["tickets"]])
        self.assertIn("partner:A", [r["ticket"] for r in result["tickets"]])
