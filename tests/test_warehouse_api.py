import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from stagehouse.api import create_app
from stagehouse.operations import Operations
from stagehouse.rules import Problem
from stagehouse.service import Service
from stagehouse.warehouse.package import Package


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class WarehouseApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "warehouse.sqlite3"
        self.service = Service(self.database)
        self.addCleanup(self.service.state.close)
        for source in ("box", "partner", "gate"):
            self.service.source({"id": source, "name": source})
        self.service.catalog(load("catalog.json"))
        for name in ("box.json", "partner.json", "gate.json"):
            self.service.ingest(load(name))

    def assert_problem(self, code, call):
        with self.assertRaises(Problem) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def publish(self, name="one", parent=None):
        run = self.service.prepare(name)
        return self.service.publish({"id": name, "run": name, "run_digest": run["digest"], "expected_head": parent})

    def test_candidate_is_not_visible_before_publish(self):
        self.service.prepare("candidate")
        self.assert_problem("release", lambda: self.service.manifest())
        self.assertEqual(self.service.status()["releases"], [])

    def test_publish_retry_and_snapshot_query(self):
        run = self.service.prepare("one")
        request = {"id": "one", "run": "one", "run_digest": run["digest"], "expected_head": None}
        first = self.service.publish(request)
        self.assertEqual(self.service.publish(request), first)
        page = self.service.query({"release": "one", "table": "settlements", "limit": 2})
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(page["next_offset"], 2)
        last = self.service.query({"release": page["release"], "table": "settlements", "offset": page["next_offset"], "limit": 2})
        self.assertEqual(len(last["rows"]), 1)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(page["digest"], last["digest"])

    def test_original_update_between_candidate_and_publish_is_stale(self):
        run = self.service.prepare("one")
        self.service.ingest(load("refund.json"))
        self.assert_problem("stale", lambda: self.service.publish({"id": "one", "run": "one", "run_digest": run["digest"], "expected_head": None}))
        self.assertIsNone(self.service.status()["head"])
        self.assertTrue(self.service.replay("one")["reproducible"])

    def test_catalog_update_invalidates_candidate(self):
        run = self.service.prepare("one")
        venue = copy.deepcopy(load("catalog.json")["records"][0])
        venue["data"].update(revision=2, capacity=120)
        self.service.catalog({"records": [venue]})
        self.assertTrue(self.service.run("one")["stale"])
        self.assert_problem("stale", lambda: self.service.publish({"id": "one", "run": "one", "run_digest": run["digest"], "expected_head": None}))
        second = self.service.prepare("two")
        self.assertFalse(second["stale"])
        self.assertEqual(self.service.dimension_impact("one", "two")["dimension_changes"][0]["kind"], "venue")

    def test_two_candidates_require_matching_predecessor(self):
        first = self.service.prepare("one")
        second = self.service.prepare("two")
        self.service.publish({"id": "one", "run": "one", "run_digest": first["digest"], "expected_head": None})
        self.assert_problem("head_conflict", lambda: self.service.publish({"id": "two", "run": "two", "run_digest": second["digest"], "expected_head": None}))
        published = self.service.publish({"id": "two", "run": "two", "run_digest": second["digest"], "expected_head": "one"})
        self.assertEqual(published["content"]["parent"], "one")
        self.assertEqual(self.service.status()["head"], "two")

    def test_refund_changes_partition_and_settlement_but_not_old_release(self):
        first = self.publish()
        old_digest = first["digest"]
        self.service.ingest(load("refund.json"))
        self.publish("two", "one")
        self.assertEqual(self.service.manifest("one")["statistics"]["net_cents"], 28001)
        self.assertEqual(self.service.manifest("two")["statistics"]["net_cents"], 24667)
        comparison = self.service.reconcile("one", "two")
        self.assertEqual(len(comparison["tickets"]), 1)
        self.assertEqual(comparison["tickets"][0]["net_delta_cents"], -3334)
        self.assertEqual(comparison["changed_partitions"], ["2026-11-21/evening"])
        self.assertEqual(self.service.export("one", Path(self.temp.name) / "old.json")["digest"], old_digest)

    def test_full_refund_before_scan_propagates_to_release_gate(self):
        self.publish()
        batch = load("refund.json")
        batch["events"][0]["data"].update(cents=10001, at="2026-11-21T18:00:00+08:00")
        self.service.ingest(batch)
        blocked = self.service.prepare("blocked")
        result = blocked["content"]["result"]
        self.assertEqual(result["statistics"]["net_cents"], 18000)
        self.assertEqual(result["statistics"]["admissions"], 1)
        self.assertFalse(result["publishable"])
        self.assert_problem("quality", lambda: self.service.publish({"id": "blocked", "run": "blocked", "run_digest": blocked["digest"], "expected_head": "one"}))
        self.assertEqual(self.service.manifest()["release"], "one")

    def test_zero_issues_empty_input_not_publishable(self):
        with Service(Path(self.temp.name) / "empty.sqlite3") as empty:
            result = empty.prepare("empty")
            self.assertEqual(result["content"]["result"]["issues"], [])
            self.assertFalse(result["content"]["result"]["publishable"])

    def test_query_cannot_mix_missing_or_wrong_dimensions(self):
        self.publish()
        self.assert_problem("table", lambda: self.service.query({"release": "one", "table": "arbitrary"}))
        self.assert_problem("filter", lambda: self.service.query({"release": "one", "table": "admissions", "account": "box-account"}))
        self.assert_problem("date", lambda: self.service.query({"release": "one", "table": "settlements", "day": "2026-11-99"}))
        self.assert_problem("integer", lambda: self.service.query({"release": "one", "table": "settlements", "limit": 0}))
        trace = self.service.trace("partner:C", "one")
        self.assertEqual(trace["sale"]["session"], "evening")
        self.assertEqual(len(trace["admissions"]), 1)
        self.assertEqual(len(trace["lineage"]), 2)

    def test_archive_tamper_and_failed_atomic_write(self):
        self.publish()
        destination = Path(self.temp.name) / "release.json"
        self.service.export("one", destination)
        self.assertTrue(Operations.verify_file(destination)["valid"])
        original = destination.read_bytes()
        with patch("stagehouse.operations.os.replace", side_effect=OSError("合成磁盘故障")):
            with self.assertRaises(OSError):
                self.service.export("one", destination)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(list(destination.parent.glob(".stagehouse-*")), [])
        value = json.loads(original)
        value["content"]["release"] = "changed"
        destination.write_text(json.dumps(value), encoding="utf-8")
        self.assert_problem("digest", lambda: Operations.verify_file(destination))

    def test_partition_package_requires_full_coverage(self):
        self.publish()
        directory = Path(self.temp.name) / "partitions"
        self.service.export_package("one", directory)
        self.assertTrue(Package.verify(directory)["valid"])
        self.assertEqual(Package.verify(directory)["partitions"], 1)
        (directory / "part-000000.json").unlink()
        self.assert_problem("package", lambda: Package.verify(directory))
        self.assert_problem("package", lambda: self.service.export_package("one", directory))

    def test_consumer_acknowledgement_is_retryable_and_not_preview(self):
        self.publish()
        self.service.subscribe("finance")
        delivery = self.service.delivery({"id": "delivery", "subscription": "finance", "target": "one"})
        self.assertFalse(delivery["acknowledged"])
        self.assertEqual(self.service.subscribe("finance")["generation"], 0)
        self.assertEqual(len(delivery["content"]["replacements"]), 1)
        ack = {"id": "delivery", "digest": delivery["digest"], "generation": 0}
        self.assertFalse(self.service.acknowledge(ack)["replayed"])
        self.assertTrue(self.service.acknowledge(ack)["replayed"])
        self.assertEqual(self.service.subscribe("finance")["release_id"], "one")

    def test_two_delivery_previews_cannot_overwrite_consumed_position(self):
        self.publish()
        self.service.subscribe("finance")
        first = self.service.delivery({"id": "first", "subscription": "finance", "target": "one"})
        self.service.ingest(load("refund.json"))
        self.publish("two", "one")
        second = self.service.delivery({"id": "second", "subscription": "finance", "target": "two"})
        self.service.acknowledge({"id": "first", "digest": first["digest"], "generation": 0})
        self.assert_problem("consumer_conflict", lambda: self.service.acknowledge({"id": "second", "digest": second["digest"], "generation": 0}))
        actual = self.service.delivery({"id": "third", "subscription": "finance", "target": "two"})
        self.assertEqual(actual["content"]["from_release"], "one")
        self.assertEqual(actual["content"]["generation"], 1)
        self.assertEqual(len(actual["content"]["replacements"]), 1)

    def test_backup_restores_history_and_consumer_position(self):
        self.publish()
        self.service.subscribe("finance")
        delivery = self.service.delivery({"id": "delivery", "subscription": "finance", "target": "one"})
        self.service.acknowledge({"id": "delivery", "digest": delivery["digest"], "generation": 0})
        destination = Path(self.temp.name) / "backup.sqlite3"
        self.service.backup(destination)
        with Service(destination) as restored:
            self.assertEqual(restored.audit(), self.service.audit())
            self.assertEqual(restored.subscribe("finance")["release_id"], "one")
            self.assertTrue(restored.replay("one")["reproducible"])
            self.assertEqual(restored.query({"release": "one", "table": "settlements"}), self.service.query({"release": "one", "table": "settlements"}))
        self.assert_problem("backup", lambda: self.service.backup(destination))
        self.assert_problem("backup", lambda: self.service.backup(self.database))

    def test_api_status_codes_real_landing_and_query(self):
        with TestClient(create_app(self.database)) as client:
            self.assertEqual(client.get("/status").status_code, 200)
            self.assertEqual(client.post("/batches", json=load("box.json")).status_code, 200)
            run_response = client.post("/runs", json={"id": "http"})
            self.assertEqual(run_response.status_code, 200)
            run = run_response.json()
            response = client.post("/releases", json={"id": "http", "run": "http", "run_digest": run["digest"], "expected_head": None})
            self.assertEqual(response.status_code, 200)
            rows = client.post("/query", json={"release": "http", "table": "settlements"})
            self.assertEqual(rows.json()["total"], 3)
            self.assertEqual(client.get("/manifest").json()["release"], "http")
            self.assertEqual(client.post("/runs/http/replay").json()["reproducible"], True)
            bad = client.post("/query", json={"release": "http", "table": "unknown"})
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(bad.json()["code"], "table")

    def test_api_rejects_revision_gap_with_conflict(self):
        with TestClient(create_app(self.database)) as client:
            batch = load("partner.json")
            batch.update(id="gap", first_offset=9, last_offset=9)
            batch["events"][0]["offset"] = 9
            response = client.post("/batches", json=batch)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "cursor")
            self.assertEqual(client.get("/status").json()["cutoff"], 3)
