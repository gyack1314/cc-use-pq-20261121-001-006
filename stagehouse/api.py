import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .rules import Problem, fields, key
from .service import Service


def create_app(database=None):
    path = str(database or os.environ.get("STAGEHOUSE_DATABASE", "stagehouse.sqlite3"))
    with Service(path):
        pass
    app = FastAPI(title="幕间事实仓", description="演出票务多渠道事实入仓与一致消费服务", version="1.0.0")
    app.state.database = path

    @contextmanager
    def service():
        with Service(app.state.database) as instance:
            yield instance

    @app.exception_handler(Problem)
    async def problem_handler(request: Request, exc: Problem):
        conflicts = {"cursor", "revision", "idempotency", "stale", "head_conflict", "release_conflict", "run_conflict", "released"}
        return JSONResponse(status_code=409 if exc.code in conflicts else 422, content=exc.body())

    @app.exception_handler(sqlite3.OperationalError)
    async def database_handler(request: Request, exc: sqlite3.OperationalError):
        if "locked" in str(exc).lower():
            return JSONResponse(status_code=503, content={"code": "busy", "message": "仓库正在处理其他写入，请稍后重试"})
        raise exc

    @app.get("/status")
    def status():
        with service() as instance:
            return instance.status()

    @app.post("/sources")
    def register_source(payload: dict = Body(...)):
        with service() as instance:
            return instance.source(payload)

    @app.post("/catalog")
    def catalog(payload: dict = Body(...)):
        with service() as instance:
            return instance.catalog(payload)

    @app.post("/batches")
    def batch(payload: dict = Body(...), format: str = Query("json")):
        with service() as instance:
            return instance.ingest(payload, format)

    @app.post("/runs")
    def prepare(payload: dict = Body(...)):
        fields(payload, ("id",))
        with service() as instance:
            return instance.prepare(payload["id"])

    @app.get("/runs/{run_id}")
    def run(run_id: str):
        with service() as instance:
            return instance.run(key(run_id))

    @app.post("/releases")
    def publish(payload: dict = Body(...)):
        with service() as instance:
            return instance.publish(payload)

    @app.post("/query")
    def query(payload: dict = Body(...)):
        with service() as instance:
            return instance.query(payload)

    @app.get("/manifest")
    def manifest(release: str | None = None):
        with service() as instance:
            return instance.manifest(release)

    @app.get("/tickets/{source}/{ticket}/trace")
    def trace(source: str, ticket: str, release: str | None = None):
        with service() as instance:
            return instance.trace(key(source) + ":" + key(ticket), release)

    @app.get("/history/{source}/{event}")
    def history(source: str, event: str):
        with service() as instance:
            return instance.history(key(source), key(event))

    @app.get("/compare")
    def compare(left: str, right: str):
        with service() as instance:
            return instance.reconcile(key(left), key(right))

    @app.post("/audit")
    def audit():
        with service() as instance:
            return instance.audit()

    @app.get("/runs/{run_id}/profile")
    def profile(run_id: str):
        with service() as instance:
            return instance.profile(key(run_id))

    @app.post("/runs/{run_id}/issues")
    def issues(run_id: str, filters: dict = Body(...)):
        with service() as instance:
            return instance.issues(key(run_id), filters)

    @app.post("/runs/{run_id}/replay")
    def replay(run_id: str):
        with service() as instance:
            return instance.replay(key(run_id))

    @app.get("/run-comparison")
    def compare_runs(left: str, right: str):
        with service() as instance:
            return instance.compare_runs(key(left), key(right))

    @app.get("/dimension-impact")
    def dimension_impact(left: str, right: str):
        with service() as instance:
            return instance.dimension_impact(key(left), key(right))

    @app.post("/subscriptions")
    def subscribe(payload: dict = Body(...)):
        fields(payload, ("id",))
        with service() as instance:
            return instance.subscribe(payload["id"])

    @app.post("/deliveries")
    def delivery(payload: dict = Body(...)):
        with service() as instance:
            return instance.delivery(payload)

    @app.post("/acknowledgements")
    def acknowledge(payload: dict = Body(...)):
        with service() as instance:
            return instance.acknowledge(payload)

    @app.post("/releases/{release_id}/check-partitions")
    def check_partitions(release_id: str):
        with service() as instance:
            return instance.check_partitions(key(release_id))

    return app
