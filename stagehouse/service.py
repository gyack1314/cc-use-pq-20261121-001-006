from .intake.formats import decode_bundle, decode_ndjson, decode_scan_csv
from .intake.landing import Landing
from .operations import Operations
from .reference import Catalog
from .rules import check
from .state import State
from .warehouse.reader import Reader
from .warehouse.versions import Versions


class Service:
    def __init__(self, path):
        self.state = State(path)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.state.close()

    def source(self, value):
        with self.state.transaction(write=True):
            return Landing(self.state).register(value)

    def catalog(self, value):
        with self.state.transaction(write=True):
            return Catalog(self.state).apply(value)

    def ingest(self, value, format="json"):
        decoders = {"json": decode_bundle, "ndjson": decode_ndjson, "scan-csv": decode_scan_csv}
        check(format in decoders, "format", "不支持的来源格式")
        bundle = decoders[format](value)
        with self.state.transaction(write=True):
            return Landing(self.state).ingest(bundle)

    def prepare(self, run_id):
        with self.state.transaction(write=True):
            return Versions(self.state).prepare(run_id)

    def run(self, run_id):
        with self.state.transaction():
            return Versions(self.state).run(run_id)

    def publish(self, value):
        with self.state.transaction(write=True):
            return Versions(self.state).publish(value)

    def query(self, value):
        with self.state.transaction():
            return Reader(Versions(self.state)).query(value)

    def manifest(self, release_id=None):
        with self.state.transaction():
            return Reader(Versions(self.state)).manifest(release_id)

    def trace(self, ticket, release_id=None):
        with self.state.transaction():
            return Reader(Versions(self.state)).trace(ticket, release_id)

    def reconcile(self, left, right):
        with self.state.transaction():
            return Reader(Versions(self.state)).reconcile(left, right)

    def status(self):
        with self.state.transaction():
            return {**self.state.integrity(), "head": Versions(self.state).head(),
                    "sources": [dict(row) for row in self.state.db.execute("SELECT * FROM sources ORDER BY id")],
                    "releases": Versions(self.state).list(), "batches": Landing(self.state).batches()}

    def audit(self):
        with self.state.transaction():
            return Operations(self.state).audit()

    def export(self, release_id, destination):
        with self.state.transaction():
            return Operations(self.state).export(release_id, destination)

    def backup(self, destination):
        return Operations(self.state).backup(destination)

    def history(self, source, event):
        with self.state.transaction():
            return self.state.raw_history(source, event)

    def profile(self, run_id):
        from .warehouse.health import Health
        with self.state.transaction():
            return Health(self.state).profile(Versions(self.state).run(run_id))

    def issues(self, run_id, filters):
        from .warehouse.health import Health
        with self.state.transaction():
            return Health(self.state).issues(Versions(self.state).run(run_id), filters)

    def dimension_impact(self, left, right):
        from .warehouse.health import Health
        with self.state.transaction():
            return Health(self.state).dimension_impact(Versions(self.state).run(left), Versions(self.state).run(right))

    def replay(self, run_id):
        from .warehouse.replay import Replay
        with self.state.transaction():
            return Replay(self.state).verify(run_id)

    def compare_runs(self, left, right):
        from .warehouse.replay import Replay
        with self.state.transaction():
            return Replay(self.state).compare(left, right)

    def export_package(self, release_id, directory):
        from .warehouse.package import Package
        with self.state.transaction():
            return Package(self.state).export(release_id, directory)

    def subscribe(self, identity):
        from .warehouse.consumer import Consumer
        with self.state.transaction(write=True):
            return Consumer(self.state).register(identity)

    def delivery(self, value):
        from .warehouse.consumer import Consumer
        with self.state.transaction(write=True):
            return Consumer(self.state).prepare(value)

    def acknowledge(self, value):
        from .warehouse.consumer import Consumer
        with self.state.transaction(write=True):
            return Consumer(self.state).acknowledge(value)

    def check_partitions(self, release_id):
        from .warehouse.partition_checks import PartitionChecks
        with self.state.transaction():
            return PartitionChecks(self.state).inspect(release_id)
