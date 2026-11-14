"""ログの書式のテスト：「- HH:MM 表示名：本文 ^m<メッセージID>」"""

from datetime import date

from app.logformat import (
    EDITED_MARK,
    apply_edit,
    attachment_links,
    daily_header,
    find_entry,
    format_entry,
    one_line,
    parse_entry,
    split_name_body,
    upsert_entry,
)


def test_format_basic_line():
    assert format_entry("09:05", "山田", "おはようございます", "123456") == "- 09:05 山田：おはようございます ^m123456"


def test_multiline_text_is_kept_on_one_line():
    line = format_entry("21:30", "佐藤", "1行目\n2行目\r\n3行目", "42")
    assert line == "- 21:30 佐藤：1行目<br>2行目<br>3行目 ^m42"
    assert "\n" not in line


def test_obsidian_comment_marker_in_text_is_neutralized():
    line = format_entry("10:00", "鈴木", "進捗100%%です", "7")
    assert "%%" not in line.split(" ^m")[0]
    assert parse_entry(line).msg_id == "7"


def test_name_with_newline_and_empty_name():
    assert format_entry("10:00", "名\n前", "本文", "1").startswith("- 10:00 名 前：")
    assert format_entry("10:00", "", "本文", "1").startswith("- 10:00 名前不明：")


def test_parse_roundtrip_and_fields():
    line = format_entry("08:00", "田中", "テスト ^m999 という文字列", "555")
    e = parse_entry(line)
    assert e is not None
    assert (e.time, e.msg_id, e.edited_ts) == ("08:00", "555", None)
    assert e.rest == "田中：テスト ^m999 という文字列"
    assert not e.is_failure and not e.is_unsent


def test_parse_ignores_non_entry_lines():
    assert parse_entry("# 2026-11-14（土） 家族") is None
    assert parse_entry("- メモ：手で書いた行") is None
    assert parse_entry("") is None


def test_edited_entry_carries_hidden_timestamp():
    line = format_entry("08:00", "田中", "修正後" + EDITED_MARK, "555", edited_ts=1763100000000)
    assert line == "- 08:00 田中：修正後（編集済み） %%edited:1763100000000%% ^m555"
    e = parse_entry(line)
    assert e.edited_ts == 1763100000000 and e.msg_id == "555"


def test_daily_header_has_date_weekday_and_title():
    assert daily_header("家族グループ", date(2026, 11, 14)) == "# 2026-11-14（土） 家族グループ\n\n"


def test_upsert_creates_file_with_header():
    header = daily_header("家族", date(2026, 11, 14))
    text, status = upsert_entry(None, header=header, line="- 09:00 A：x ^m10", time="09:00", msg_id="10")
    assert status == "inserted"
    assert text == "# 2026-11-14（土） 家族\n\n- 09:00 A：x ^m10\n"


def test_upsert_keeps_chronological_order_even_if_processed_late():
    text = "# h\n\n- 09:00 A：一番目 ^m100\n- 09:30 B：三番目 ^m300\n"
    text, _ = upsert_entry(text, header="", line="- 09:10 C：二番目 ^m200", time="09:10", msg_id="200")
    text, _ = upsert_entry(text, header="", line="- 08:00 D：最初 ^m50", time="08:00", msg_id="50")
    text, _ = upsert_entry(text, header="", line="- 23:59 E：最後 ^m900", time="23:59", msg_id="900")
    ids = [parse_entry(x).msg_id for x in text.splitlines() if parse_entry(x)]
    assert ids == ["50", "100", "200", "300", "900"]
    assert text.endswith("\n")


def test_upsert_same_minute_orders_by_message_id():
    text = "- 09:00 A：a ^m300\n"
    text, _ = upsert_entry(text, header="", line="- 09:00 B：b ^m200", time="09:00", msg_id="200")
    assert [parse_entry(x).msg_id for x in text.splitlines() if x] == ["200", "300"]


def test_upsert_preserves_hand_written_notes():
    text = "# h\n\nメモ：ここは手で書いた\n\n- 09:00 A：a ^m1\n"
    new, _ = upsert_entry(text, header="", line="- 10:00 B：b ^m2", time="10:00", msg_id="2")
    assert "メモ：ここは手で書いた" in new
    assert new.index("^m1") < new.index("^m2")


def test_split_name_body_prefers_known_name_containing_separator():
    assert split_name_body("A：B：本文", known_name="A：B") == ("A：B", "本文")
    assert split_name_body("山田：本文：続き") == ("山田", "本文：続き")


def test_attachment_links_extracted():
    rest = "山田：[PDF] [[LINEログ/添付/2026-11-14_0930_見積書.pdf.md|見積書.pdf]]<br>要約"
    assert attachment_links(rest) == ["LINEログ/添付/2026-11-14_0930_見積書.pdf.md"]
    assert attachment_links("山田：[[LINEログ/添付/2026-11-14_0930_画像|画像]]") == ["LINEログ/添付/2026-11-14_0930_画像.md"]


def test_apply_edit_replaces_body_and_marks_edited():
    text = "# h\n\n- 09:00 山田：明日は9時集合 ^m77\n"
    new, status = apply_edit(text, "77", "明日は10時集合", 2000, known_name="山田")
    assert status == "edited"
    assert "- 09:00 山田：明日は10時集合（編集済み） %%edited:2000%% ^m77" in new


def test_apply_edit_ignores_older_out_of_order_event():
    text = "- 09:00 山田：最新（編集済み） %%edited:3000%% ^m77\n"
    new, status = apply_edit(text, "77", "古い編集", 2000)
    assert (new, status) == (None, "stale")
    new, status = apply_edit(text, "77", "さらに新しい", 4000)
    assert status == "edited" and "さらに新しい（編集済み） %%edited:4000%%" in new


def test_apply_edit_does_not_resurrect_unsent_message():
    text = "- 09:00 山田：（送信取消） ^m77\n"
    assert apply_edit(text, "77", "復活？", 5000) == (None, "unsent")


def test_find_entry():
    text = "- 09:00 A：a ^m1\n- 09:01 B：b ^m2\n"
    assert find_entry(text, "2").rest == "B：b"
    assert find_entry(text, "3") is None
    assert find_entry(None, "1") is None


def test_one_line_handles_unicode_line_separators():
    assert one_line("a b c\x0bd") == "a<br>b<br>c<br>d"
