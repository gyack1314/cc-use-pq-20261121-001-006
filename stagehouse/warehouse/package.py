import json
import os
import shutil
import tempfile
from pathlib import Path

from ..operations import atomic_json
from ..rules import canonical, check, digest
from .contracts import WarehouseContract
from .versions import Versions


class Package:
    def __init__(self, state):
        self.state = state

    def export(self, release_id, directory):
        release = Versions(self.state).release(release_id)
        target = Path(directory).resolve()
        check(not target.exists(), "package", "分区包目标已存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".partitions-", dir=target.parent))
        try:
            snapshot = release["content"]["snapshot"]
            partitions = []
            for index, partition in enumerate(snapshot["result"]["partitions"]):
                name = f"part-{index:06d}.json"
                atomic_json(temporary / name, partition["body"])
                partitions.append({"file": name, "key": partition["key"], "digest": partition["digest"], "rows": partition["rows"]})
            manifest = {"schema_version": 1, "release": release["content"]["release"], "release_digest": release["digest"],
                        "cutoff": snapshot["cutoff"], "catalog_version": snapshot["catalog_version"],
                        "partitions": partitions, "sources": snapshot["sources"]}
            atomic_json(temporary / "manifest.json", {"content": manifest, "digest": digest(manifest)})
            atomic_json(temporary / "release.json", {"content": release["content"], "digest": release["digest"]})
            self.verify(temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"directory": str(target), "release": release_id, "partitions": len(partitions), "digest": release["digest"]}

    @staticmethod
    def verify(directory):
        from ..rules import Problem
        root = Path(directory).resolve()
        check(root.is_dir() and not Path(directory).is_symlink(), "package", "分区包目录无效")
        try:
            envelope = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            release = json.loads((root / "release.json").read_text(encoding="utf-8"))
            manifest = envelope["content"]
            check(digest(manifest) == envelope["digest"], "digest", "分区清单摘要不符")
            check(digest(release["content"]) == release["digest"] == manifest["release_digest"], "digest", "发布文件摘要不符")
            check(release["content"]["release"] == manifest["release"], "package", "发布编号不一致")
            result = release["content"]["snapshot"]["result"]
            WarehouseContract().require(result)
            expected = {row["key"]: row for row in result["partitions"]}
            seen = set()
            files = {"manifest.json", "release.json"}
            for partition in manifest["partitions"]:
                name = partition["file"]
                check(isinstance(name, str) and Path(name).name == name and name.startswith("part-") and name.endswith(".json"),
                      "package", "分区文件名不安全")
                check(partition["key"] not in seen and name not in files, "package", "分区或文件重复")
                seen.add(partition["key"])
                files.add(name)
                path = root / name
                check(not path.is_symlink() and path.is_file(), "package", "分区文件缺失或是链接")
                body = json.loads(path.read_text(encoding="utf-8"))
                original = expected.get(partition["key"])
                check(original is not None and body == original["body"], "package", "分区内容与发布不一致")
                check(digest(body) == partition["digest"] == original["digest"], "digest", "分区内容摘要不符")
                check(partition["rows"] == original["rows"], "package", "分区行数清单不符")
            check(seen == set(expected), "package", "分区覆盖不完整")
            check({path.name for path in root.iterdir()} == files, "package", "包内包含未声明文件")
            return {"valid": True, "release": manifest["release"], "partitions": len(seen), "digest": release["digest"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, Problem):
                raise
            raise Problem("package", "分区包不可解析") from exc
