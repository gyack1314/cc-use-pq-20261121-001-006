import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from stagehouse.api import create_app
from stagehouse.operations import Operations
from stagehouse.service import Service
from stagehouse.warehouse.package import Package


def run():
    examples = Path(__file__).parent / "examples"
    load = lambda name: json.loads((examples / name).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "warehouse.sqlite3"
        with TestClient(create_app(database)) as client:
            for source in ("box", "partner", "gate"):
                response = client.post("/sources", json={"id": source, "name": "合成来源-" + source})
                assert response.status_code == 200, response.text
            assert client.post("/catalog", json=load("catalog.json")).status_code == 200
            for name in ("box.json", "partner.json", "gate.json"):
                response = client.post("/batches", json=load(name))
                assert response.status_code == 200, response.text
            first = client.post("/runs", json={"id": "first"}).json()
            assert first["content"]["result"]["publishable"]
            assert first["content"]["result"]["statistics"]["admissions"] == 2
            payload = {"id": "first", "run": "first", "run_digest": first["digest"], "expected_head": None}
            response = client.post("/releases", json=payload)
            assert response.status_code == 200, response.text
            assert client.post("/batches", json=load("refund.json")).status_code == 200
            second = client.post("/runs", json={"id": "second"}).json()
            assert second["content"]["result"]["statistics"]["net_cents"] == 24667
            response = client.post("/releases", json={"id": "second", "run": "second", "run_digest": second["digest"], "expected_head": "first"})
            assert response.status_code == 200, response.text
            trace = client.get("/tickets/partner/C/trace", params={"release": "second"}).json()
            assert trace["settlements"][0]["commission_cents"] == 833
            assert len(trace["admissions"]) == 1
            assert client.post("/runs/first/replay").json()["reproducible"]
            assert client.post("/releases/second/check-partitions").json()["net_cents"] == 24667
            assert client.post("/subscriptions", json={"id": "finance"}).status_code == 200
            delivery = client.post("/deliveries", json={"id": "delivery", "subscription": "finance", "target": "second"}).json()
            ack = {"id": "delivery", "digest": delivery["digest"], "generation": 0}
            assert client.post("/acknowledgements", json=ack).json()["replayed"] is False
            assert client.post("/acknowledgements", json=ack).json()["replayed"] is True
        with Service(database) as service:
            service.export("second", Path(directory) / "release.json")
            assert Operations.verify_file(Path(directory) / "release.json")["valid"]
            service.export_package("second", Path(directory) / "parts")
            assert Package.verify(Path(directory) / "parts")["valid"]
            service.backup(Path(directory) / "backup.sqlite3")
            assert service.audit()["releases"] == 2
        with Service(Path(directory) / "backup.sqlite3") as restored:
            assert restored.manifest("first")["statistics"]["net_cents"] == 28001
            assert restored.manifest("second")["statistics"]["net_cents"] == 24667
        print(json.dumps({"status": "通过", "synthetic": True, "http_flow": True, "admissions": 2,
                          "net_cents": 24667, "commission_cents": 833, "historical_replay": True,
                          "partition_package": True, "backup_restored": True}, ensure_ascii=False))


if __name__ == "__main__":
    run()
