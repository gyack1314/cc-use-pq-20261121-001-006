import copy
import json
import tempfile
import unittest
from pathlib import Path

from stagehouse.intake.formats import encode_scan_csv
from stagehouse.rules import Problem
from stagehouse.service import Service


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Service(Path(self.temp.name) / "warehouse.sqlite3")
        self.addCleanup(self.service.state.close)
        for source in ("box", "partner", "gate"):
            self.service.source({"id": source, "name": "合成来源-" + source})
        self.service.catalog(load("catalog.json"))

    def assert_problem(self, code, call):
        with self.assertRaises(Problem) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_register_source_idempotent_but_not_rename(self):
        first = self.service.source({"id": "box", "name": "合成来源-box"})
        self.assertEqual(first["cursor"], 0)
        self.assert_problem("source", lambda: self.service.source({"id": "box", "name": "另一系统"}))
        self.assertEqual(self.service.status()["sources"][0]["name"], "合成来源-box")

    def test_batch_replay_preserves_watermark_and_cursor(self):
        initial = self.service.ingest(load("box.json"))
        before = self.service.status()
        retry = self.service.ingest(load("box.json"))
        self.assertFalse(initial["replayed"])
        self.assertTrue(retry["replayed"])
        self.assertEqual(initial["seq"], retry["seq"])
        self.assertEqual(before, self.service.status())

    def test_same_batch_new_original_payload_is_rejected(self):
        self.service.ingest(load("box.json"))
        changed = load("box.json")
        changed["events"][0]["data"]["cents"] = 9999
        self.assert_problem("idempotency", lambda: self.service.ingest(changed))
        self.assertEqual(self.service.state.events()[0]["data"]["cents"], 10000)

    def test_gap_and_overlap_do_not_advance_cursor(self):
        gap = load("partner.json")
        gap.update(first_offset=2, last_offset=2)
        gap["events"][0]["offset"] = 2
        self.assert_problem("cursor", lambda: self.service.ingest(gap))
        self.assertEqual(self.service.state.watermark(), 0)
        self.service.ingest(load("partner.json"))
        overlap = load("partner.json")
        overlap["id"] = "another-id"
        self.assert_problem("cursor", lambda: self.service.ingest(overlap))
        self.assertEqual(self.service.state.watermark(), 1)

    def test_missing_offset_and_duplicate_offset_rejected(self):
        batch = load("box.json")
        batch["events"] = batch["events"][:1]
        self.assert_problem("coverage", lambda: self.service.ingest(batch))
        batch = load("box.json")
        batch["events"][1]["offset"] = 1
        self.assert_problem("coverage", lambda: self.service.ingest(batch))
        self.assertEqual(self.service.state.watermark(), 0)

    def test_invalid_last_row_rolls_back_entire_batch(self):
        batch = load("box.json")
        batch["events"][1]["revision"] = 2
        self.assert_problem("revision", lambda: self.service.ingest(batch))
        self.assertEqual(self.service.state.events(), [])
        self.assertEqual(next(r["cursor"] for r in self.service.status()["sources"] if r["id"] == "box"), 0)
        self.assertEqual(self.service.status()["batches"], [])

    def test_revision_keeps_original_and_historical_cutoff(self):
        self.service.ingest(load("partner.json"))
        before = self.service.prepare("before")
        batch = load("partner.json")
        batch.update(id="partner-correction", first_offset=2, last_offset=2)
        batch["events"][0].update(revision=2, offset=2)
        batch["events"][0]["data"]["cents"] = 12000
        self.service.ingest(batch)
        history = self.service.history("partner", "sale-c")
        self.assertEqual([r["revision"] for r in history], [1, 2])
        self.assertEqual(self.service.state.events(1)[0]["data"]["cents"], 10001)
        self.assertEqual(self.service.state.events()[0]["data"]["cents"], 12000)
        self.assertEqual(self.service.run("before")["digest"], before["digest"])
        self.assertTrue(self.service.replay("before")["reproducible"])

    def test_revision_cannot_change_kind(self):
        self.service.ingest(load("partner.json"))
        batch = load("refund.json")
        batch["events"][0].update(id="sale-c", revision=2)
        self.assert_problem("kind", lambda: self.service.ingest(batch))
        self.assertEqual(self.service.state.watermark(), 1)

    def test_unknown_source_rejected_before_landing(self):
        batch = load("box.json")
        batch["source"] = "unknown"
        self.assert_problem("source", lambda: self.service.ingest(batch))
        self.assertEqual(self.service.state.events(), [])

    def test_time_normalizes_offsets_and_rejects_naive(self):
        batch = load("partner.json")
        batch["events"][0]["data"]["at"] = "2026-11-20T03:00:00Z"
        self.service.ingest(batch)
        self.assertEqual(self.service.state.events()[0]["data"]["at"], "2026-11-20T03:00:00+00:00")
        batch = load("box.json")
        batch["events"][0]["data"]["at"] = "2026-11-20T11:00:00"
        self.assert_problem("time", lambda: self.service.ingest(batch))

    def test_money_is_integer_cents(self):
        for amount in (True, 1.5, -1, "1000"):
            batch = load("box.json")
            batch["events"][0]["data"]["cents"] = amount
            self.assert_problem("integer", lambda: self.service.ingest(batch))
        self.assertEqual(self.service.state.watermark(), 0)

    def test_scan_csv_roundtrip_and_line_error(self):
        batch = load("gate.json")
        csv = encode_scan_csv(batch.pop("events"))
        batch["csv"] = "\ufeff" + csv
        result = self.service.ingest(batch, "scan-csv")
        self.assertEqual(result["events"], 2)
        self.assertEqual(self.service.state.events()[0]["kind"], "scan")
        broken = {**batch, "id": "broken", "csv": "event_id,event_id\nx,y\n"}
        self.assert_problem("csv_header", lambda: self.service.ingest(broken, "scan-csv"))

    def test_ndjson_adapter_keeps_source_identity(self):
        batch = load("box.json")
        rows = batch.pop("events")
        batch["ndjson"] = "\n".join(json.dumps(row) for row in rows)
        self.service.ingest(batch, "ndjson")
        self.assertEqual([r["identity"] for r in self.service.state.events()], ["box:sale-a", "box:sale-b"])
        broken = {**batch, "ndjson": "{bad json}"}
        self.assert_problem("ndjson", lambda: self.service.ingest(broken, "ndjson"))

    def test_dimension_alias_collision_is_atomic(self):
        records = load("catalog.json")["records"]
        alias = copy.deepcopy(next(r for r in records if r["kind"] == "alias"))
        alias["data"]["id"] = "collision"
        venue = copy.deepcopy(records[0])
        venue["data"].update(id="new-venue", revision=1)
        self.assert_problem("alias", lambda: self.service.catalog({"records": [venue, alias]}))
        self.assertNotIn("new-venue", self.service.state.dimensions()["venue"])
        self.assertEqual(self.service.state.catalog_version(), 1)

    def test_missing_references_and_bad_timezone_rejected(self):
        row = copy.deepcopy(load("catalog.json")["records"][0])
        row["data"].update(id="bad", timezone="No/Such_Zone")
        self.assert_problem("timezone", lambda: self.service.catalog({"records": [row]}))
        session = copy.deepcopy(load("catalog.json")["records"][1])
        session["data"].update(id="bad", venue="unknown")
        self.assert_problem("reference", lambda: self.service.catalog({"records": [session]}))

    def test_catalog_replay_and_historical_version(self):
        self.assertEqual(self.service.catalog(load("catalog.json"))["changed"], 0)
        row = copy.deepcopy(load("catalog.json")["records"][0])
        row["data"].update(revision=2, capacity=200)
        self.service.catalog({"records": [row]})
        self.assertEqual(self.service.state.dimensions(1)["venue"]["hall"]["capacity"], 100)
        self.assertEqual(self.service.state.dimensions()["venue"]["hall"]["capacity"], 200)
        self.assert_problem("version", lambda: self.service.state.dimensions(99))
