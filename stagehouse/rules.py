import hashlib
import json
from datetime import date, datetime, timezone


class Problem(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def body(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def check(condition, code, message, **details):
    if not condition:
        raise Problem(code, message, **details)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fields(value, required, optional=()):
    check(isinstance(value, dict), "shape", "资料需要JSON对象")
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    check(not missing and not extra, "shape", "资料字段不符合契约", missing=sorted(missing), extra=sorted(extra))


def label(value, name="名称", maximum=160):
    check(isinstance(value, str) and value.strip() == value and 0 < len(value) <= maximum
          and "\x00" not in value, "text", f"{name}需要有效文本")
    return value


def key(value, name="编号"):
    value = label(value, name, 100)
    check(all(c.isascii() and (c.isalnum() or c in "_-.") for c in value), "identity", f"{name}格式无效")
    return value


def integer(value, name, minimum=0, maximum=10**12):
    check(type(value) is int and minimum <= value <= maximum, "integer", f"{name}需要范围内整数")
    return value


def instant(value):
    check(isinstance(value, str), "time", "时间需要带偏移的ISO文本")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Problem("time", "时间格式无效") from None
    check(parsed.tzinfo is not None and parsed.utcoffset() is not None, "time", "时间缺少UTC偏移")
    check(parsed.microsecond == 0, "time", "时间精度限定到秒")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def date_text(value):
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise Problem("date", "日期需要YYYY-MM-DD") from None
    check(result.isoformat() == value, "date", "日期需要YYYY-MM-DD")
    return value


def ticket_key(source, ticket):
    return key(source, "出票来源") + ":" + key(ticket, "票号")


def round_share(cents, bps):
    sign = -1 if cents < 0 else 1
    return sign * ((abs(cents) * bps + 5000) // 10000)


def issue(code, message, rows, **details):
    return {"code": code, "message": message, "events": sorted(set(rows)), "details": details}
