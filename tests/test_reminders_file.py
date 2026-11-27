"""リマインド.md の読み書きのテスト（人が直接編集しても壊れにくいこと）。"""

from datetime import datetime

from app.reminders import HEADER, add_reminder, find, new_fields, parse_reminders, set_fields
from app.schedule import parse_schedule
from app.timeutil import JST

HUMAN_EDITED = """# リマインド

> [!info] 説明文（見出しの下に - 項目: のような行があってもリマインドとは見なさない）

## R1 ゴミ出し
- 送り先： 家族グループ
- スケジュール：毎週 月,木 7時
- 本文: 明日は燃えるゴミの日です
  前の晩に出さないでください
- 状態: 有効
- 最終送信日時: 2026-11-12 07:00
メモ：これは人が書き足したメモ

## R2
- 送信先: 町内会
- 日時: 毎月 第2火曜 19:00
- 内容: 定例会です
- 状態: 停止

## R3
- 送り先: 家族グループ
- スケジュール: 毎日 25:00
- 本文: 書式エラーの例

## 手書きの見出し（リマインドではない）
ただのメモ
"""


def test_parse_human_edited_file():
    rs = parse_reminders(HUMAN_EDITED)
    assert [r.rid for r in rs] == ["R1", "R2", "R3"]
    r1, r2, r3 = rs
    assert r1.title == "ゴミ出し"
    assert r1.target == "家族グループ"
    assert r1.schedule.canonical() == "毎週 月,木 07:00"
    assert r1.body == "明日は燃えるゴミの日です\n前の晩に出さないでください"
    assert r1.enabled and r1.is_valid
    assert r1.last_sent == datetime(2026, 11, 12, 7, 0, tzinfo=JST)
    assert r2.target == "町内会" and r2.schedule_text == "毎月 第2火曜 19:00" and r2.body == "定例会です"
    assert r2.enabled is False and r2.is_valid
    assert not r3.is_valid and any("25:00" in e for e in r3.errors)


def test_missing_state_means_enabled_and_unknown_state_is_error():
    text = "## R9\n- 送り先: A\n- スケジュール: 毎日 09:00\n- 本文: x\n"
    assert parse_reminders(text)[0].enabled is True
    bad = text + "- 状態: たぶん\n"
    r = parse_reminders(bad)[0]
    assert not r.is_valid and r.enabled is False


def test_set_fields_changes_only_target_lines():
    new = set_fields(HUMAN_EDITED, "R1", {"最終送信日時": "2026-11-16 07:00"})
    assert "- 最終送信日時: 2026-11-16 07:00" in new
    # 他の行（人のメモ・他の件・全角コロンの行）はそのまま
    assert "メモ：これは人が書き足したメモ" in new
    assert "- 送り先： 家族グループ" in new
    assert "- 送信先: 町内会" in new
    assert new.replace("- 最終送信日時: 2026-11-16 07:00", "- 最終送信日時: 2026-11-12 07:00") == HUMAN_EDITED


def test_set_fields_adds_missing_field_and_multiline_body():
    new = set_fields(HUMAN_EDITED, "R2", {"更新日時": "2026-11-14 10:00", "本文": "定例会です\n場所：公民館"})
    r2 = find(parse_reminders(new), "R2")
    assert r2.body == "定例会です\n場所：公民館"
    assert r2.updated_at == datetime(2026, 11, 14, 10, 0, tzinfo=JST)
    assert "- 内容: 定例会です" not in new and "- 本文: 定例会です\n  場所：公民館" in new
    assert find(parse_reminders(new), "R1").body.startswith("明日は燃えるゴミの日です")


def test_add_reminder_to_new_file_and_next_id():
    now = datetime(2026, 11, 14, 9, 0, tzinfo=JST)
    text, rid = add_reminder(None, new_fields("家族グループ", parse_schedule("毎日 08:00"), "おはよう", now))
    assert rid == "R1" and text.startswith(HEADER)
    text, rid = add_reminder(text, new_fields("町内会", parse_schedule("平日 07:30"), "1行目\n2行目", now))
    assert rid == "R2"
    rs = parse_reminders(text)
    assert [r.rid for r in rs] == ["R1", "R2"]
    assert rs[1].body == "1行目\n2行目" and rs[1].last_sent is None and rs[1].enabled
    assert "- 最終送信日時: 未送信" in text


def test_next_id_skips_existing_numbers():
    now = datetime(2026, 11, 14, 9, 0, tzinfo=JST)
    _text, rid = add_reminder(HUMAN_EDITED, new_fields("A", parse_schedule("毎日 08:00"), "x", now))
    assert rid == "R4"


def test_find_is_case_and_width_insensitive():
    rs = parse_reminders(HUMAN_EDITED)
    assert find(rs, "r1").rid == "R1"
    assert find(rs, "Ｒ２").rid == "R2"
    assert find(rs, "R99") is None
