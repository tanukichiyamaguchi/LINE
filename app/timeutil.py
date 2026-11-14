"""日本時間（JST）の扱いをまとめたモジュール。

日本には夏時間がないため、UTC+9 の固定オフセットで正確に表せる。
（tzdata への依存を避けるため zoneinfo は使わない）
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")

WEEKDAYS_JA = "月火水木金土日"  # datetime.weekday() の 0=月 … 6=日 に対応


def now_jst() -> datetime:
    return datetime.now(JST)


def from_epoch_ms(ms: int | float) -> datetime:
    """LINE の timestamp（ミリ秒）を JST の datetime にする。"""
    return datetime.fromtimestamp(float(ms) / 1000.0, JST)


def to_jst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def fmt_date(d: datetime | date) -> str:
    return d.strftime("%Y-%m-%d")


def fmt_hhmm(dt: datetime) -> str:
    return to_jst(dt).strftime("%H:%M")


def fmt_minute(dt: datetime) -> str:
    """「YYYY-MM-DD HH:MM」形式（リマインドの日時欄で使う）。"""
    return to_jst(dt).strftime("%Y-%m-%d %H:%M")


def weekday_ja(d: datetime | date) -> str:
    return WEEKDAYS_JA[d.weekday()]


def fmt_date_with_weekday(d: datetime | date) -> str:
    return f"{fmt_date(d)}（{weekday_ja(d)}）"


def fmt_short(dt: datetime) -> str:
    """「11/14（土）09:00」のような短い表記（LINEの返信用）。"""
    dt = to_jst(dt)
    return f"{dt.month}/{dt.day}（{weekday_ja(dt)}）{dt:%H:%M}"


_MINUTE_RE = re.compile(
    r"^\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*[ T]?\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$"
)


def parse_minute(text: str | None) -> datetime | None:
    """「YYYY-MM-DD HH:MM」などを JST の datetime として読む。読めなければ None。"""
    if not text:
        return None
    s = unicodedata.normalize("NFKC", text).strip()
    if not s or s in {"-", "ー", "なし", "未送信"}:
        return None
    m = _MINUTE_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=JST)
    except ValueError:
        return None


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)
