import argparse
import json
import sys
from pathlib import Path

from .operations import Operations
from .rules import Problem, canonical
from .service import Service


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Problem("input", "JSON资料文件不可读") from exc


def main(argv=None):
    root = argparse.ArgumentParser(description="幕间事实仓离线操作")
    root.add_argument("--database", default="stagehouse.sqlite3")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("source", "catalog", "ingest", "query", "publish"):
        item = commands.add_parser(name)
        item.add_argument("input")
        if name == "ingest":
            item.add_argument("--format", default="json", choices=("json", "scan-csv", "ndjson"))
    for name in ("prepare", "run", "profile", "replay"):
        item = commands.add_parser(name)
        item.add_argument("id")
    commands.add_parser("status")
    commands.add_parser("audit")
    item = commands.add_parser("manifest")
    item.add_argument("--release")
    item = commands.add_parser("export")
    item.add_argument("release")
    item.add_argument("destination")
    item = commands.add_parser("export-package")
    item.add_argument("release")
    item.add_argument("destination")
    item = commands.add_parser("verify-package")
    item.add_argument("path")
    for name in ("backup", "verify"):
        item = commands.add_parser(name)
        item.add_argument("path")
    item = commands.add_parser("serve")
    item.add_argument("--host", default="127.0.0.1")
    item.add_argument("--port", type=int, default=8014)
    args = root.parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(args.database), host=args.host, port=args.port)
            return 0
        if args.command == "verify":
            result = Operations.verify_file(args.path)
        elif args.command == "verify-package":
            from .warehouse.package import Package
            result = Package.verify(args.path)
        else:
            with Service(args.database) as service:
                if args.command in ("source", "catalog", "query", "publish"):
                    result = getattr(service, args.command)(load(args.input))
                elif args.command == "ingest":
                    result = service.ingest(load(args.input), args.format)
                elif args.command in ("prepare", "run", "profile", "replay"):
                    result = getattr(service, args.command)(args.id)
                elif args.command in ("status", "audit"):
                    result = getattr(service, args.command)()
                elif args.command == "manifest":
                    result = service.manifest(args.release)
                elif args.command == "export":
                    result = service.export(args.release, args.destination)
                elif args.command == "export-package":
                    result = service.export_package(args.release, args.destination)
                else:
                    result = service.backup(args.path)
        print(canonical(result))
        return 0
    except Problem as exc:
        print(canonical(exc.body()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
