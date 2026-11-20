from ..rules import check, digest
from .build import Builder
from .contracts import WarehouseContract
from .versions import Versions


class Replay:
    def __init__(self, state):
        self.state = state

    def verify(self, run_id):
        run = Versions(self.state).run(run_id)
        content = run["content"]
        dimensions = self.state.dimensions(content["catalog_version"])
        events = self.state.events(content["cutoff"])
        rebuilt = Builder(dimensions).build(events)
        WarehouseContract().require(rebuilt)
        check(rebuilt == content["result"], "replay", "固定水位与维度重放不能复现候选")
        check(content["sources"] == self.state.source_vector(content["cutoff"]), "replay", "来源向量与当时输入不一致")
        return {"run": run_id, "reproducible": True, "result_digest": digest(rebuilt),
                "cutoff": content["cutoff"], "catalog_version": content["catalog_version"]}

    def compare(self, left_id, right_id):
        first = Versions(self.state).run(left_id)["content"]
        second = Versions(self.state).run(right_id)["content"]
        a = {row["event"]: row for row in first["result"]["lineage"]}
        b = {row["event"]: row for row in second["result"]["lineage"]}
        changes = []
        for identity in sorted(set(a) | set(b)):
            left, right = a.get(identity), b.get(identity)
            if left == right:
                continue
            changes.append({"event": identity, "before": left, "after": right})
        old_issues = {(row["code"], tuple(row["events"])): row for row in first["result"]["issues"]}
        new_issues = {(row["code"], tuple(row["events"])): row for row in second["result"]["issues"]}
        before_parts = {row["key"]: row["digest"] for row in first["result"]["partitions"]}
        after_parts = {row["key"]: row["digest"] for row in second["result"]["partitions"]}
        return {"left": left_id, "right": right_id, "events": changes,
                "resolved_issues": [old_issues[key] for key in sorted(set(old_issues) - set(new_issues))],
                "new_issues": [new_issues[key] for key in sorted(set(new_issues) - set(old_issues))],
                "changed_partitions": [key for key in sorted(set(before_parts) | set(after_parts))
                                       if before_parts.get(key) != after_parts.get(key)],
                "left_publishable": first["result"]["publishable"], "right_publishable": second["result"]["publishable"]}
