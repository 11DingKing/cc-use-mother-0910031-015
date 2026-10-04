"""时间、序列化与哈希等基础工具。

时间统一存储为带时区的 ISO-8601 字符串（UTC），字典序即可排序，
定时任务以显式 ``as_of`` 时间点求值，保证结果可复现。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_iso(value: str | datetime | None, *, default_now: bool = False) -> str | None:
    """把入参规范化为 ISO 字符串，并校验可解析。"""
    if value is None:
        return now_iso() if default_now else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    # 解析一次以拒绝非法时间。
    parse_iso(value)
    return value


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_days(value: str, days: int) -> str:
    return (parse_iso(value) + timedelta(days=days)).isoformat()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value, default):
    if value is None:
        return default
    return json.loads(value)
