"""リマインドの時刻判定のテスト（日本時間・5分おきの実行を想定）。"""

from datetime import datetime, timedelta

import pytest

from app.schedule import ScheduleError, due_occurrence, parse_schedule
from app.timeutil import JST

CATCHUP = timedelta(minutes=30)


def jst(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=JST)


def due(schedule_text, now, last_sent=None, updated_at=None, catchup=CATCHUP):
    return due_occurrence(parse_schedule(schedule_text), now, last_sent, updated_at, catchup)


# ---------------------------------------------------------------- 基本の判定
def test_not_due_before_time():
    assert due("毎日 09:00", jst(2026, 11, 14, 8, 55), last_sent=jst(2026, 11, 13, 9, 0)) is None


def test_due_exactly_at_time():
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 0), last_sent=jst(2026, 11, 13, 9, 0)) == jst(2026, 11, 14, 9, 0)


def test_due_shortly_after_time_when_cron_is_late():
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 4), last_sent=jst(2026, 11, 13, 9, 0)) == jst(2026, 11, 14, 9, 0)


def test_not_sent_twice_for_same_occurrence():
    # 09:00 の回を 09:00 に送った後、09:05 の実行では送らない
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 5), last_sent=jst(2026, 11, 14, 9, 0)) is None
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 25), last_sent=jst(2026, 11, 14, 9, 3)) is None


def test_next_day_is_due_again():
    assert due("毎日 09:00", jst(2026, 11, 15, 9, 0), last_sent=jst(2026, 11, 14, 9, 0)) == jst(2026, 11, 15, 9, 0)


def test_too_old_occurrence_is_skipped_after_long_outage():
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 30), last_sent=None) == jst(2026, 11, 14, 9, 0)
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 31), last_sent=None) is None


def test_registered_after_occurrence_waits_for_next_one():
    # 09:02 に「毎日 09:00」を登録しても、その日の 09:00 の回は送らない
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 5), updated_at=jst(2026, 11, 14, 9, 2)) is None
    assert due("毎日 09:00", jst(2026, 11, 15, 9, 0), updated_at=jst(2026, 11, 14, 9, 2)) == jst(2026, 11, 15, 9, 0)


def test_registered_before_occurrence_is_due():
    assert due("毎日 09:00", jst(2026, 11, 14, 9, 0), updated_at=jst(2026, 11, 14, 8, 59)) == jst(2026, 11, 14, 9, 0)


def test_multiple_times_per_day():
    s = "毎日 08:00,20:00"
    assert due(s, jst(2026, 11, 14, 20, 0), last_sent=jst(2026, 11, 14, 8, 0)) == jst(2026, 11, 14, 20, 0)
    assert due(s, jst(2026, 11, 14, 12, 0), last_sent=jst(2026, 11, 14, 8, 0)) is None


# ---------------------------------------------------------------- 曜日・日付
def test_weekdays_skip_weekend():
    # 2026-11-14 は土曜、11-16 は月曜
    assert due("平日 07:30", jst(2026, 11, 14, 7, 30)) is None
    assert due("平日 07:30", jst(2026, 11, 16, 7, 30)) == jst(2026, 11, 16, 7, 30)


def test_weekly_specific_days():
    assert due("毎週 月,木 09:00", jst(2026, 11, 19, 9, 0)) == jst(2026, 11, 19, 9, 0)  # 木曜
    assert due("毎週 月,木 09:00", jst(2026, 11, 18, 9, 0)) is None  # 水曜


def test_monthly_days_and_last_day():
    assert due("毎月 1,15日 10:00", jst(2026, 11, 15, 10, 0)) == jst(2026, 11, 15, 10, 0)
    assert due("毎月 末日 18:00", jst(2026, 11, 30, 18, 0)) == jst(2026, 11, 30, 18, 0)
    assert due("毎月 末日 18:00", jst(2027, 2, 28, 18, 0)) == jst(2027, 2, 28, 18, 0)
    assert due("毎月 末日 18:00", jst(2028, 2, 28, 18, 0)) is None  # 2028年はうるう年（29日が末日）
    assert due("毎月 末日 18:00", jst(2028, 2, 29, 18, 0)) == jst(2028, 2, 29, 18, 0)


def test_monthly_31st_is_skipped_in_short_months():
    s = parse_schedule("毎月 31日 09:00")
    assert s.next_after(jst(2026, 11, 1, 0, 0)) == jst(2026, 12, 31, 9, 0)


def test_nth_and_last_weekday():
    # 2026年11月：第2火曜=10日、最終金曜=27日
    assert due("毎月 第2火曜 19:00", jst(2026, 11, 10, 19, 0)) == jst(2026, 11, 10, 19, 0)
    assert due("毎月 第2火曜 19:00", jst(2026, 11, 3, 19, 0)) is None
    assert due("毎月 最終金曜 18:00", jst(2026, 11, 27, 18, 0)) == jst(2026, 11, 27, 18, 0)
    assert due("毎月 最終金曜 18:00", jst(2026, 11, 20, 18, 0)) is None


def test_yearly():
    assert due("毎年 12-25 09:00", jst(2026, 12, 25, 9, 0)) == jst(2026, 12, 25, 9, 0)
    assert due("毎年 12-25 09:00", jst(2026, 12, 24, 9, 0)) is None


def test_once_only_once():
    s = "2026-12-01 09:00"
    assert due(s, jst(2026, 11, 30, 9, 0)) is None
    assert due(s, jst(2026, 12, 1, 9, 0)) == jst(2026, 12, 1, 9, 0)
    assert due(s, jst(2026, 12, 1, 9, 5), last_sent=jst(2026, 12, 1, 9, 0)) is None


def test_next_after_crosses_month_and_year():
    s = parse_schedule("毎年 01-01 00:00")
    assert s.next_after(jst(2026, 12, 31, 23, 59)) == jst(2027, 1, 1, 0, 0)


# ---------------------------------------------------------------- 書式
@pytest.mark.parametrize(
    "text, canonical",
    [
        ("毎日 8:00", "毎日 08:00"),
        ("毎日 9時半", "毎日 09:30"),
        ("平日 午後3時", "平日 15:00"),
        ("土日 10:00", "毎週 土,日 10:00"),
        ("毎週月曜9時", "毎週 月 09:00"),
        ("毎週 月・水・金 07:00", "毎週 月,水,金 07:00"),
        ("毎月 1、15日 10:00", "毎月 1,15日 10:00"),
        ("毎月 最終日 18:00", "毎月 末日 18:00"),
        ("毎月 第１，３水曜 ９：００", "毎月 第1,3水曜 09:00"),
        ("毎月 最終日曜 18:00", "毎月 最終日曜 18:00"),
        ("毎年12月25日 9時", "毎年 12-25 09:00"),
        ("2026/12/1 9:00", "2026-12-01 09:00"),
        ("一回 2026年12月1日 午前9時", "2026-12-01 09:00"),
    ],
)
def test_parse_and_canonical(text, canonical):
    s = parse_schedule(text)
    assert s.canonical() == canonical
    assert parse_schedule(s.canonical()) == s


@pytest.mark.parametrize("text", ["毎日", "毎週 9:00", "毎日 25:00", "毎月 32日 9:00", "毎年 2-30 9:00",
                                  "cron: 0 9 * * 1", "隔週 月 9:00", "", "毎月 第2日 10:00"])
def test_invalid_schedules_are_rejected(text):
    with pytest.raises(ScheduleError):
        parse_schedule(text)
