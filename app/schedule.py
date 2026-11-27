"""リマインドのスケジュール（日本語の簡易表記）の解析と、送信時刻の計算。

対応する書き方（時刻は日本時間。複数の時刻は「,」区切り。祝日は考慮しない）：

    毎日 08:00            毎日 08:00,20:00
    平日 07:30            （月〜金）
    土日 10:00            （= 毎週 土,日）
    毎週 月,木 09:00      （「毎週月曜」「毎週 月・水・金」も可）
    毎月 1,15日 10:00
    毎月 末日 18:00
    毎月 第2火曜 19:00    （「毎月 第1,3水曜」「毎月 最終金曜」も可）
    毎年 12-25 09:00      （「毎年 12/25」「毎年 12月25日」も可）
    2026-12-01 09:00      （1回のみ。「2026/12/1 9時」なども可）

時刻は「9:00」「9時」「9時30分」「9時半」「午後3時」も受け付け、保存時は
上の正規形（例：毎週 月,木 09:00）に整える。
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .timeutil import JST, WEEKDAYS_JA, to_jst

SCAN_DAYS = 400  # 毎年の予定も見つけられるよう1年強を走査する

SUPPORTED_HELP = (
    "使える書き方の例：毎日 08:00／平日 07:30／毎週 月,木 09:00／毎月 1,15日 10:00／"
    "毎月 末日 18:00／毎月 第2火曜 19:00／毎年 12-25 09:00／2026-12-01 09:00（1回のみ）"
)


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    kind: str  # daily / weekdays / weekly / monthly / yearly / once
    times: tuple[time, ...] = ()
    weekdays: frozenset[int] = frozenset()  # 0=月 … 6=日
    month_days: frozenset[int] = frozenset()  # 1〜31
    last_day: bool = False
    nth: frozenset[tuple[int, int]] = frozenset()  # (第n, 曜日)。n=-1 は最終
    month: int = 0
    day: int = 0
    once_at: datetime | None = None

    @property
    def is_once(self) -> bool:
        return self.kind == "once"

    # ---------------------------------------------------------------- 表記
    def canonical(self) -> str:
        t = ",".join(f"{x.hour:02d}:{x.minute:02d}" for x in sorted(self.times))
        if self.kind == "once":
            assert self.once_at is not None
            return self.once_at.strftime("%Y-%m-%d %H:%M")
        if self.kind == "daily":
            return f"毎日 {t}"
        if self.kind == "weekdays":
            return f"平日 {t}"
        if self.kind == "weekly":
            return f"毎週 {','.join(WEEKDAYS_JA[w] for w in sorted(self.weekdays))} {t}"
        if self.kind == "yearly":
            return f"毎年 {self.month:02d}-{self.day:02d} {t}"
        parts: list[str] = []
        if self.month_days:
            parts.append(",".join(str(d) for d in sorted(self.month_days)) + "日")
        if self.last_day:
            parts.append("末日")
        for wd in range(7):
            ns = sorted(n for n, w in self.nth if w == wd and n > 0)
            if ns:
                parts.append(f"第{','.join(map(str, ns))}{WEEKDAYS_JA[wd]}曜")
            if (-1, wd) in self.nth:
                parts.append(f"最終{WEEKDAYS_JA[wd]}曜")
        return f"毎月 {','.join(parts)} {t}"

    # ---------------------------------------------------------------- 判定
    def matches(self, d: date) -> bool:
        if self.kind == "daily":
            return True
        if self.kind == "weekdays":
            return d.weekday() < 5
        if self.kind == "weekly":
            return d.weekday() in self.weekdays
        if self.kind == "yearly":
            return (d.month, d.day) == (self.month, self.day)
        if self.kind == "once":
            return self.once_at is not None and d == self.once_at.date()
        # monthly
        if d.day in self.month_days:
            return True
        last = calendar.monthrange(d.year, d.month)[1]
        if self.last_day and d.day == last:
            return True
        for n, wd in self.nth:
            if d.weekday() != wd:
                continue
            if n == -1 and d.day + 7 > last:
                return True
            if n > 0 and (d.day - 1) // 7 + 1 == n:
                return True
        return False

    def latest_at_or_before(self, now: datetime) -> datetime | None:
        """now 以前で直近の予定時刻。"""
        now = to_jst(now)
        if self.kind == "once":
            return self.once_at if self.once_at and self.once_at <= now else None
        times = sorted(self.times, reverse=True)
        for k in range(SCAN_DAYS):
            d = now.date() - timedelta(days=k)
            if not self.matches(d):
                continue
            for t in times:
                dt = datetime.combine(d, t, tzinfo=JST)
                if dt <= now:
                    return dt
        return None

    def next_after(self, now: datetime) -> datetime | None:
        """now より後で直近の予定時刻。"""
        now = to_jst(now)
        if self.kind == "once":
            return self.once_at if self.once_at and self.once_at > now else None
        times = sorted(self.times)
        for k in range(SCAN_DAYS):
            d = now.date() + timedelta(days=k)
            if not self.matches(d):
                continue
            for t in times:
                dt = datetime.combine(d, t, tzinfo=JST)
                if dt > now:
                    return dt
        return None


# ==================================================================== 解析
_TIME_RE = re.compile(
    r"(?P<ampm>午前|午後)?\s*(?P<h>\d{1,2})\s*(?:(?::)\s*(?P<m>\d{2})|時\s*(?:(?P<m2>\d{1,2})\s*分|(?P<half>半))?)"
)
_ONCE_RE = re.compile(r"^(?:一回|1回|一度|1度|1回のみ|一回のみ)?\s*(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?$")
_YEARLY_RE = re.compile(r"^毎年\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?$")
# 「第2日」のように日曜を「日」1文字で書くと日付と紛らわしいため、日曜は「日曜」と書く必要がある
_NTH_RE = re.compile(r"第\s*([1-5](?:\s*,\s*第?\s*[1-5])*)\s*(?:([月火水木金土])(?:曜日?)?|(日)曜日?)")
_LAST_WD_RE = re.compile(r"最終\s*([月火水木金土日])曜日?")
_LAST_DAY_RE = re.compile(r"末日|月末|最終日(?!曜)")
_DAYS_RE = re.compile(r"(\d{1,2})\s*日?")


def _normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = s.replace("、", ",").replace("・", ",").replace("，", ",")
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_times(s: str) -> tuple[list[time], str]:
    times: list[time] = []
    for m in _TIME_RE.finditer(s):
        h = int(m.group("h"))
        minute = int(m.group("m") or m.group("m2") or (30 if m.group("half") else 0))
        if m.group("ampm") == "午後" and h < 12:
            h += 12
        if m.group("ampm") == "午前" and h == 12:
            h = 0
        if not (0 <= h <= 23 and 0 <= minute <= 59):
            raise ScheduleError(f"時刻「{m.group(0).strip()}」が正しくありません。")
        times.append(time(h, minute))
    rest = _TIME_RE.sub(" ", s)
    rest = re.sub(r"\s*,\s*(?=,|$)", "", rest)  # 時刻を取り除いた後に残る「,」
    rest = re.sub(r"^\s*,|,\s*$", "", rest.strip())
    rest = re.sub(r"\s+", " ", rest).strip(" ,")
    return sorted(set(times)), rest


def _weekday_set(s: str) -> frozenset[int]:
    s = s.replace("曜日", "").replace("曜", "")
    days = {WEEKDAYS_JA.index(ch) for ch in s if ch in WEEKDAYS_JA}
    leftover = re.sub(rf"[{WEEKDAYS_JA},\s]", "", s)
    if leftover:
        raise ScheduleError(f"曜日の指定「{s}」を読めませんでした。")
    return frozenset(days)


def parse_schedule(text: str) -> Schedule:
    """日本語の簡易表記を解析する。読めない場合は ScheduleError。"""
    s = _normalize(text)
    if not s:
        raise ScheduleError("スケジュールが空です。" + SUPPORTED_HELP)
    if s.lower().startswith("cron"):
        raise ScheduleError("cron式には対応していません。" + SUPPORTED_HELP)
    times, rule = _parse_times(s)
    rule_nospace = rule.replace(" ", "")

    m = _ONCE_RE.match(rule)
    if m:
        if len(times) != 1:
            raise ScheduleError("1回のみのリマインドは時刻を1つだけ指定してください（例：2026-12-01 09:00）。")
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), times[0].hour, times[0].minute, tzinfo=JST)
        except ValueError as exc:
            raise ScheduleError(f"日付「{rule}」が正しくありません。") from exc
        return Schedule(kind="once", times=(times[0],), once_at=dt)

    if not times:
        raise ScheduleError("時刻（例：09:00）を指定してください。" + SUPPORTED_HELP)
    t = tuple(times)

    if rule_nospace in {"毎日", "毎朝", "毎晩", "毎夕"}:
        return Schedule(kind="daily", times=t)
    if rule_nospace in {"平日", "毎週平日", "平日毎日"}:
        return Schedule(kind="weekdays", times=t)
    if rule_nospace in {"土日", "毎週土日", "週末", "毎週末", "土日曜", "毎週土日曜"}:
        return Schedule(kind="weekly", times=t, weekdays=frozenset({5, 6}))

    m = _YEARLY_RE.match(rule)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            date(2024, month, day)  # 2024年はうるう年なので 2/29 も許可
        except ValueError as exc:
            raise ScheduleError(f"日付「{month}/{day}」が正しくありません。") from exc
        return Schedule(kind="yearly", times=t, month=month, day=day)

    if rule_nospace.startswith("毎週") or (rule_nospace and all(c in WEEKDAYS_JA + ",曜日" for c in rule_nospace)):
        wd = _weekday_set(rule_nospace.removeprefix("毎週"))
        if not wd:
            raise ScheduleError("曜日を指定してください（例：毎週 月,木 09:00）。")
        return Schedule(kind="weekly", times=t, weekdays=wd)

    if rule_nospace.startswith("毎月"):
        body = rule_nospace.removeprefix("毎月")
        nth: set[tuple[int, int]] = set()
        for m in _NTH_RE.finditer(body):
            wd = WEEKDAYS_JA.index(m.group(2) or m.group(3))
            for n in re.findall(r"[1-5]", m.group(1)):
                nth.add((int(n), wd))
        body = _NTH_RE.sub(",", body)
        last_day = bool(_LAST_DAY_RE.search(body))
        body = _LAST_DAY_RE.sub(",", body)
        for m in _LAST_WD_RE.finditer(body):
            nth.add((-1, WEEKDAYS_JA.index(m.group(1))))
        body = _LAST_WD_RE.sub(",", body)
        days: set[int] = set()
        for token in [x for x in body.split(",") if x.strip()]:
            dm = _DAYS_RE.fullmatch(token.strip())
            if not dm:
                raise ScheduleError(f"「毎月」の指定「{token}」を読めませんでした。" + SUPPORTED_HELP)
            d = int(dm.group(1))
            if not 1 <= d <= 31:
                raise ScheduleError(f"日付「{d}日」が正しくありません。")
            days.add(d)
        if not (days or last_day or nth):
            raise ScheduleError("「毎月」の後に日付（例：1,15日／末日／第2火曜）を指定してください。")
        return Schedule(kind="monthly", times=t, month_days=frozenset(days), last_day=last_day, nth=frozenset(nth))

    raise ScheduleError(f"スケジュール「{text}」を読めませんでした。" + SUPPORTED_HELP)


def due_occurrence(
    schedule: Schedule,
    now: datetime,
    last_sent: datetime | None,
    updated_at: datetime | None,
    catchup: timedelta,
) -> datetime | None:
    """今送るべき回（予定時刻）を返す。送る必要がなければ None。

    * 直近の予定時刻 T が now から catchup を超えて過去なら送らない（長時間止まっていた場合）。
    * 最終送信日時が T 以降なら送信済み（同じ回を二重に送らない）。
    * 更新日時が T 以降なら、その回は登録・変更より前の予定なので送らない。
    """
    occurrence = schedule.latest_at_or_before(now)
    if occurrence is None:
        return None
    if to_jst(now) - occurrence > catchup:
        return None
    if last_sent is not None and last_sent >= occurrence:
        return None
    if updated_at is not None and updated_at >= occurrence:
        return None
    return occurrence
