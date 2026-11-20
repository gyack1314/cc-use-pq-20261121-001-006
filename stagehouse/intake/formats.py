import csv
import io
import json

from ..rules import Problem, canonical, check, fields, integer, key
from .protocol import normalize_batch


def decode_bundle(value):
    fields(value, ("id", "source", "first_offset", "last_offset", "events"))
    normalize_batch(value)
    return value


def decode_scan_csv(value):
    fields(value, ("id", "source", "first_offset", "last_offset", "csv"))
    check(isinstance(value["csv"], str) and len(value["csv"]) <= 10_000_000, "csv", "验票CSV需要有效文本")
    stream = io.StringIO(value["csv"].lstrip("\ufeff"), newline="")
    reader = csv.DictReader(stream, strict=True)
    expected = {"event_id", "revision", "offset", "ticket_source", "ticket", "session_ref", "at", "gate"}
    try:
        header = reader.fieldnames
        check(header and len(header) == len(set(header)) and set(header) == expected, "csv_header", "验票CSV表头不匹配")
        events = []
        for row in reader:
            check(None not in row and all(item is not None for item in row.values()), "csv", "验票CSV列数不一致", line=reader.line_num)
            converted = {}
            for field in ("revision", "offset"):
                item = row[field]
                check(item.isascii() and item.isdecimal(), "csv", "验票CSV整数列格式无效", line=reader.line_num, field=field)
                converted[field] = int(item)
            events.append({"id": row["event_id"], "kind": "scan", **converted,
                           "data": {field: row[field] for field in ("ticket_source", "ticket", "session_ref", "at", "gate")}})
    except csv.Error as exc:
        raise Problem("csv", "验票CSV引号或转义不正确", line=reader.line_num) from exc
    bundle = {field: value[field] for field in ("id", "source", "first_offset", "last_offset")}
    bundle["events"] = events
    normalize_batch(bundle)
    return bundle


def encode_scan_csv(events):
    output = io.StringIO(newline="")
    columns = ["event_id", "revision", "offset", "ticket_source", "ticket", "session_ref", "at", "gate"]
    writer = csv.DictWriter(output, columns, lineterminator="\n")
    writer.writeheader()
    for event in events:
        check(event["kind"] == "scan", "kind", "只能输出验票CSV")
        writer.writerow({"event_id": event["id"], "revision": event["revision"], "offset": event["offset"], **event["data"]})
    return output.getvalue()


def decode_ndjson(value):
    fields(value, ("id", "source", "first_offset", "last_offset", "ndjson"))
    check(isinstance(value["ndjson"], str) and len(value["ndjson"]) <= 10_000_000, "ndjson", "逐行JSON资料无效")
    events = []
    for number, line in enumerate(value["ndjson"].splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise Problem("ndjson", "逐行JSON解析失败", line=number) from None
        events.append(event)
    result = {field: value[field] for field in ("id", "source", "first_offset", "last_offset")}
    result["events"] = events
    normalize_batch(result)
    return result
